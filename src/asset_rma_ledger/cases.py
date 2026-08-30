"""RMA case opening and immutable opening-event operations."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from .assets import AssetNotFoundError, get_asset
from .events import ZERO_EVENT_HASH, calculate_event_hash, canonical_json
from .models import CaseEvent, RmaCase
from .vendors import (
    VendorInactiveError,
    VendorNotFoundError,
    VendorValidationError,
    require_active_vendor,
)

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}", flags=re.ASCII)
_OUTCOMES = frozenset(
    {"repaired", "replaced", "refund", "no_fault_found", "repair_declined", "written_off", "other"}
)
_OUTCOME_STATUSES = frozenset({"open", "authorised", "with_vendor", "returned"})
_ACTIVE_CASE_STATUSES = frozenset(
    {"open", "authorised", "outbound", "with_vendor", "returning", "returned"}
)
_EXCEPTIONAL_OUTCOMES = frozenset({"refund", "repair_declined", "written_off", "other"})
_EXCEPTIONAL_CLOSE_STATUSES = frozenset({"open", "authorised", "with_vendor"})


class CaseError(RuntimeError):
    """Base error for RMA case domain operations."""


class CaseValidationError(CaseError):
    """Raised when supplied case fields violate the command contract."""


class CaseConflictError(CaseError):
    """Raised when a case reference or active asset case already exists."""


class CaseNotFoundError(CaseError):
    """Raised when a requested case does not exist."""


class CaseStateError(CaseError):
    """Raised when a selected asset or vendor cannot open a case."""


def open_case(
    connection: sqlite3.Connection,
    *,
    reference: str,
    asset_tag: str,
    vendor_key: str,
    opened_at: str,
    operator_alias: str,
    response_due_at: str | None = None,
    resolution_due_at: str | None = None,
) -> RmaCase:
    """Open one RMA case and append its first immutable event atomically."""
    case_reference = _validate_identifier(reference, "case reference")
    alias = _validate_identifier(operator_alias, "operator alias")
    occurred_at = _parse_utc_timestamp(opened_at, "opened-at")
    explicit_response_due = _parse_optional_utc_timestamp(response_due_at, "response due-at")
    explicit_resolution_due = _parse_optional_utc_timestamp(resolution_due_at, "resolution due-at")
    asset = _require_case_asset(connection, asset_tag)
    vendor = _require_case_vendor(connection, vendor_key)
    effective_response_due = explicit_response_due or _deadline_from_default(
        occurred_at, vendor.response_sla_minutes
    )
    effective_resolution_due = explicit_resolution_due or _deadline_from_default(
        occurred_at, vendor.resolution_sla_minutes
    )
    payload = {
        "asset_tag": asset.tag,
        "resolution_due_at": effective_resolution_due,
        "response_due_at": effective_response_due,
        "vendor_key": vendor.key,
    }
    payload_json = canonical_json(payload)
    event_id = str(uuid.uuid4())
    recorded_at = _utc_now()
    event_hash = calculate_event_hash(
        case_reference=case_reference,
        sequence=1,
        event_id=event_id,
        event_type="case_opened",
        occurred_at=occurred_at,
        recorded_at=recorded_at,
        operator_alias=alias,
        payload_json=payload_json,
        previous_hash=ZERO_EVENT_HASH,
    )

    try:
        with _write_transaction(connection):
            _ensure_case_reference_available(connection, case_reference)
            _ensure_asset_has_no_active_case(connection, asset.id)
            cursor = connection.execute(
                """
                INSERT INTO rma_cases (
                    case_reference, case_reference_folded, asset_id, vendor_id, opened_at,
                    current_status, response_due_at, resolution_due_at,
                    last_event_sequence, last_event_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_reference,
                    case_reference.casefold(),
                    asset.id,
                    vendor.id,
                    occurred_at,
                    "open",
                    effective_response_due,
                    effective_resolution_due,
                    1,
                    event_hash,
                    recorded_at,
                    recorded_at,
                ),
            )
            case_id = cursor.lastrowid
            connection.execute(
                """
                INSERT INTO case_events (
                    event_id, case_id, sequence, event_type, occurred_at, recorded_at,
                    operator_alias, payload_json, previous_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    case_id,
                    1,
                    "case_opened",
                    occurred_at,
                    recorded_at,
                    alias,
                    payload_json,
                    ZERO_EVENT_HASH,
                    event_hash,
                ),
            )
            row = _select_case_by_id(connection, case_id)
    except sqlite3.IntegrityError as error:
        raise CaseConflictError("case reference or active RMA case already exists") from error

    return _case_from_row(row)


def get_case(connection: sqlite3.Connection, reference: str) -> RmaCase:
    """Return an RMA case by its case-insensitive business reference."""
    case_reference = _validate_identifier(reference, "case reference")
    row = _select_case_by_reference(connection, case_reference.casefold())
    if row is None:
        raise CaseNotFoundError(f"case does not exist: {case_reference}")
    return _case_from_row(row)


def list_case_events(connection: sqlite3.Connection, reference: str) -> tuple[CaseEvent, ...]:
    """Return a case's immutable history in sequence order."""
    case = get_case(connection, reference)
    rows = connection.execute(
        """
        SELECT case_events.*, rma_cases.case_reference
        FROM case_events
        JOIN rma_cases ON rma_cases.id = case_events.case_id
        WHERE case_events.case_id = ?
        ORDER BY case_events.sequence ASC
        """,
        (case.id,),
    ).fetchall()
    return tuple(_event_from_row(row) for row in rows)


def record_vendor_response(
    connection: sqlite3.Connection,
    reference: str,
    *,
    at: str,
    operator_alias: str,
    vendor_reference: str | None = None,
) -> RmaCase:
    """Record the first vendor response without changing the lifecycle status."""
    case = get_case(connection, reference)
    if case.vendor_responded_at is not None:
        raise CaseStateError("vendor response has already been recorded")
    normalised_reference = _validate_optional_text(vendor_reference, "vendor reference", 128)
    updates: dict[str, str | int | None] = {"vendor_responded_at": _parse_utc_timestamp(at, "at")}
    if normalised_reference is not None:
        updates["vendor_reference"] = normalised_reference
    return _append_milestone(
        connection,
        case,
        at=updates["vendor_responded_at"],
        operator_alias=operator_alias,
        event_type="vendor_response_recorded",
        payload={"vendor_reference": normalised_reference},
        updates=updates,
        allowed_statuses=frozenset(
            {"open", "authorised", "outbound", "with_vendor", "returning", "returned"}
        ),
    )


def authorise_case(
    connection: sqlite3.Connection, reference: str, *, at: str, operator_alias: str
) -> RmaCase:
    """Move an open case to authorised."""
    case = get_case(connection, reference)
    return _append_milestone(
        connection,
        case,
        at=_parse_utc_timestamp(at, "at"),
        operator_alias=operator_alias,
        event_type="status_changed",
        payload={"from_status": "open", "to_status": "authorised"},
        updates={"current_status": "authorised"},
        allowed_statuses=frozenset({"open"}),
    )


def dispatch_case(
    connection: sqlite3.Connection,
    reference: str,
    *,
    at: str,
    operator_alias: str,
    carrier: str,
    tracking: str,
) -> RmaCase:
    """Record outbound dispatch and set the associated asset to in_rma."""
    case = get_case(connection, reference)
    normalised_carrier = _validate_required_text(carrier, "carrier", 200)
    normalised_tracking = _validate_required_text(tracking, "tracking", 200)
    occurred_at = _parse_utc_timestamp(at, "at")
    return _append_milestone(
        connection,
        case,
        at=occurred_at,
        operator_alias=operator_alias,
        event_type="outbound_dispatched",
        payload={"carrier": normalised_carrier, "tracking": normalised_tracking},
        updates={"current_status": "outbound", "outbound_dispatched_at": occurred_at},
        allowed_statuses=frozenset({"authorised"}),
        asset_status_change=("in_stock_or_deployed", "in_rma"),
    )


def record_vendor_receipt(
    connection: sqlite3.Connection, reference: str, *, at: str, operator_alias: str
) -> RmaCase:
    """Record vendor receipt of an outbound asset."""
    case = get_case(connection, reference)
    occurred_at = _parse_utc_timestamp(at, "at")
    return _append_milestone(
        connection,
        case,
        at=occurred_at,
        operator_alias=operator_alias,
        event_type="vendor_receipt_recorded",
        payload={},
        updates={"current_status": "with_vendor", "vendor_received_at": occurred_at},
        allowed_statuses=frozenset({"outbound"}),
    )


def dispatch_return(
    connection: sqlite3.Connection,
    reference: str,
    *,
    at: str,
    operator_alias: str,
    carrier: str,
    tracking: str,
) -> RmaCase:
    """Record dispatch from the vendor back to the IT team."""
    case = get_case(connection, reference)
    normalised_carrier = _validate_required_text(carrier, "carrier", 200)
    normalised_tracking = _validate_required_text(tracking, "tracking", 200)
    occurred_at = _parse_utc_timestamp(at, "at")
    return _append_milestone(
        connection,
        case,
        at=occurred_at,
        operator_alias=operator_alias,
        event_type="return_dispatched",
        payload={"carrier": normalised_carrier, "tracking": normalised_tracking},
        updates={"current_status": "returning", "return_dispatched_at": occurred_at},
        allowed_statuses=frozenset({"with_vendor"}),
    )


def receive_return(
    connection: sqlite3.Connection, reference: str, *, at: str, operator_alias: str
) -> RmaCase:
    """Record asset return and restore the associated asset to in_stock."""
    case = get_case(connection, reference)
    occurred_at = _parse_utc_timestamp(at, "at")
    return _append_milestone(
        connection,
        case,
        at=occurred_at,
        operator_alias=operator_alias,
        event_type="return_received",
        payload={},
        updates={"current_status": "returned", "returned_at": occurred_at},
        allowed_statuses=frozenset({"returning"}),
        asset_status_change=("in_rma", "in_stock"),
    )


def change_case_deadlines(
    connection: sqlite3.Connection,
    reference: str,
    *,
    at: str,
    operator_alias: str,
    reason: str,
    response_due_at: str | None = None,
    resolution_due_at: str | None = None,
) -> RmaCase:
    """Change one or both deadlines through an immutable reasoned event."""
    if response_due_at is None and resolution_due_at is None:
        raise CaseValidationError("at least one deadline value must be supplied")
    case = get_case(connection, reference)
    occurred_at = _parse_utc_timestamp(at, "at")
    next_response_due = _parse_optional_utc_timestamp(response_due_at, "response due-at")
    next_resolution_due = _parse_optional_utc_timestamp(resolution_due_at, "resolution due-at")
    normalised_reason = _validate_required_text(reason, "reason", 500)
    effective_response_due = (
        next_response_due if response_due_at is not None else case.response_due_at
    )
    effective_resolution_due = (
        next_resolution_due if resolution_due_at is not None else case.resolution_due_at
    )
    return _append_milestone(
        connection,
        case,
        at=occurred_at,
        operator_alias=operator_alias,
        event_type="deadline_changed",
        payload={
            "previous_response_due_at": case.response_due_at,
            "response_due_at": effective_response_due,
            "previous_resolution_due_at": case.resolution_due_at,
            "resolution_due_at": effective_resolution_due,
            "reason": normalised_reason,
        },
        updates={
            "response_due_at": effective_response_due,
            "resolution_due_at": effective_resolution_due,
        },
        allowed_statuses=frozenset(
            {"open", "authorised", "outbound", "with_vendor", "returning", "returned"}
        ),
    )


def record_case_outcome(
    connection: sqlite3.Connection,
    reference: str,
    *,
    at: str,
    operator_alias: str,
    outcome: str,
    note: str | None = None,
) -> RmaCase:
    """Record the effective outcome that must precede case closure."""
    case = get_case(connection, reference)
    occurred_at = _parse_utc_timestamp(at, "at")
    normalised_outcome = _validate_outcome(outcome)
    normalised_note = _validate_optional_text(note, "outcome note", 2000)
    if normalised_outcome == "other" and normalised_note is None:
        raise CaseValidationError("other outcome requires an explanatory note")
    payload: dict[str, Any] = {"outcome": normalised_outcome}
    if normalised_note is not None:
        payload["note"] = normalised_note
    return _append_milestone(
        connection,
        case,
        at=occurred_at,
        operator_alias=operator_alias,
        event_type="outcome_recorded",
        payload=payload,
        updates={"current_outcome": normalised_outcome},
        allowed_statuses=_OUTCOME_STATUSES,
        additional_validation=_require_outcome_absent,
    )


def add_case_note(
    connection: sqlite3.Connection,
    reference: str,
    *,
    at: str,
    operator_alias: str,
    note: str,
) -> RmaCase:
    """Append a bounded operational note without changing case status."""
    case = get_case(connection, reference)
    occurred_at = _parse_utc_timestamp(at, "at")
    normalised_note = _validate_required_text(note, "note", 2000)
    return _append_milestone(
        connection,
        case,
        at=occurred_at,
        operator_alias=operator_alias,
        event_type="note_added",
        payload={"note": normalised_note},
        updates={},
        allowed_statuses=_ACTIVE_CASE_STATUSES,
    )


def correct_case_outcome(
    connection: sqlite3.Connection,
    reference: str,
    *,
    at: str,
    operator_alias: str,
    original_event_id: str,
    outcome: str,
    reason: str,
    note: str | None = None,
) -> RmaCase:
    """Correct an outcome through a compensating immutable event."""
    case = get_case(connection, reference)
    occurred_at = _parse_utc_timestamp(at, "at")
    normalised_event_id = _validate_required_text(original_event_id, "original event ID", 36)
    normalised_outcome = _validate_outcome(outcome)
    normalised_reason = _validate_required_text(reason, "reason", 500)
    normalised_note = _validate_optional_text(note, "outcome note", 2000)
    if normalised_outcome == "other" and normalised_note is None:
        raise CaseValidationError("other outcome requires an explanatory note")
    if case.current_outcome == normalised_outcome:
        raise CaseValidationError("replacement outcome must differ from the current outcome")
    original_event = _select_case_event(connection, case.id, normalised_event_id)
    if original_event is None or original_event["event_type"] != "outcome_recorded":
        raise CaseValidationError("original event ID must reference an outcome-recorded event")
    payload: dict[str, Any] = {
        "field": "outcome",
        "original_event_id": normalised_event_id,
        "previous_value": case.current_outcome,
        "replacement_value": normalised_outcome,
        "reason": normalised_reason,
    }
    if normalised_note is not None:
        payload["note"] = normalised_note
    return _append_milestone(
        connection,
        case,
        at=occurred_at,
        operator_alias=operator_alias,
        event_type="correction_recorded",
        payload=payload,
        updates={"current_outcome": normalised_outcome},
        allowed_statuses=_OUTCOME_STATUSES,
        additional_validation=_require_outcome_present,
    )


def close_case(
    connection: sqlite3.Connection,
    reference: str,
    *,
    at: str,
    operator_alias: str,
    asset_status: str | None = None,
) -> RmaCase:
    """Close a returned case or a documented exceptional case."""
    case = get_case(connection, reference)
    occurred_at = _parse_utc_timestamp(at, "at")
    _require_outcome_present(case)
    if case.current_status == "returned":
        if asset_status is not None:
            raise CaseValidationError("asset status is only valid for exceptional closure")
        return _append_milestone(
            connection,
            case,
            at=occurred_at,
            operator_alias=operator_alias,
            event_type="case_closed",
            payload={
                "asset_status": "in_stock",
                "closure_type": "returned",
                "outcome": case.current_outcome,
            },
            updates={"current_status": "closed", "closed_at": occurred_at},
            allowed_statuses=frozenset({"returned"}),
            additional_validation=_require_outcome_present,
        )

    if case.current_status not in _EXCEPTIONAL_CLOSE_STATUSES:
        raise CaseStateError("case close requires returned or exceptional eligible status")
    final_asset_status = _validate_exceptional_asset_status(asset_status)
    _require_exceptional_outcome(case)
    return _append_milestone(
        connection,
        case,
        at=occurred_at,
        operator_alias=operator_alias,
        event_type="case_closed",
        payload={
            "asset_status": final_asset_status,
            "closure_type": "exceptional",
            "outcome": case.current_outcome,
        },
        updates={"current_status": "closed", "closed_at": occurred_at},
        allowed_statuses=_EXCEPTIONAL_CLOSE_STATUSES,
        asset_status_change=(_asset_status_before_exceptional_close(case), final_asset_status),
        additional_validation=_require_exceptional_outcome,
    )


def cancel_case(
    connection: sqlite3.Connection,
    reference: str,
    *,
    at: str,
    operator_alias: str,
    reason: str,
) -> RmaCase:
    """Cancel a case before dispatch without altering its history."""
    case = get_case(connection, reference)
    occurred_at = _parse_utc_timestamp(at, "at")
    normalised_reason = _validate_required_text(reason, "reason", 500)
    return _append_milestone(
        connection,
        case,
        at=occurred_at,
        operator_alias=operator_alias,
        event_type="case_cancelled",
        payload={"reason": normalised_reason},
        updates={"current_status": "cancelled"},
        allowed_statuses=frozenset({"open", "authorised"}),
    )


def _append_milestone(
    connection: sqlite3.Connection,
    case: RmaCase,
    *,
    at: str,
    operator_alias: str,
    event_type: str,
    payload: dict[str, Any],
    updates: dict[str, str | int | None],
    allowed_statuses: frozenset[str],
    asset_status_change: tuple[str, str] | None = None,
    additional_validation: Callable[[RmaCase], None] | None = None,
) -> RmaCase:
    """Append one lifecycle event and update the affected projections atomically."""
    alias = _validate_identifier(operator_alias, "operator alias")
    if case.current_status not in allowed_statuses:
        expected = " or ".join(sorted(allowed_statuses))
        raise CaseStateError(f"{event_type} requires {expected} status")
    if additional_validation is not None:
        additional_validation(case)
    _validate_event_time(connection, case.id, at)

    with _write_transaction(connection):
        current = _case_from_row(_select_case_by_id(connection, case.id))
        if current.current_status not in allowed_statuses:
            expected = " or ".join(sorted(allowed_statuses))
            raise CaseStateError(f"{event_type} requires {expected} status")
        if additional_validation is not None:
            additional_validation(current)
        _validate_event_time(connection, current.id, at)
        sequence, event_hash, recorded_at = _insert_case_event(
            connection,
            current,
            at=at,
            operator_alias=alias,
            event_type=event_type,
            payload=payload,
        )
        _update_case_projection(
            connection,
            current.id,
            updates={
                **updates,
                "last_event_sequence": sequence,
                "last_event_hash": event_hash,
                "updated_at": recorded_at,
            },
        )
        if asset_status_change is not None:
            _update_asset_lifecycle(connection, current.id, *asset_status_change, recorded_at)
        row = _select_case_by_id(connection, current.id)
    return _case_from_row(row)


def _insert_case_event(
    connection: sqlite3.Connection,
    case: RmaCase,
    *,
    at: str,
    operator_alias: str,
    event_type: str,
    payload: dict[str, Any],
) -> tuple[int, str, str]:
    sequence = case.last_event_sequence + 1
    previous_hash = case.last_event_hash
    if previous_hash is None:
        raise CaseStateError("case is missing its previous event hash")
    event_id = str(uuid.uuid4())
    recorded_at = _utc_now()
    payload_json = canonical_json(payload)
    event_hash = calculate_event_hash(
        case_reference=case.reference,
        sequence=sequence,
        event_id=event_id,
        event_type=event_type,
        occurred_at=at,
        recorded_at=recorded_at,
        operator_alias=operator_alias,
        payload_json=payload_json,
        previous_hash=previous_hash,
    )
    connection.execute(
        """
        INSERT INTO case_events (
            event_id, case_id, sequence, event_type, occurred_at, recorded_at,
            operator_alias, payload_json, previous_hash, event_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            case.id,
            sequence,
            event_type,
            at,
            recorded_at,
            operator_alias,
            payload_json,
            previous_hash,
            event_hash,
        ),
    )
    return sequence, event_hash, recorded_at


def _update_case_projection(
    connection: sqlite3.Connection, case_id: int, *, updates: dict[str, str | int | None]
) -> None:
    assignments = ", ".join(f"{column} = ?" for column in updates)
    connection.execute(
        f"UPDATE rma_cases SET {assignments} WHERE id = ?", [*updates.values(), case_id]
    )


def _update_asset_lifecycle(
    connection: sqlite3.Connection,
    case_id: int,
    expected_status: str,
    next_status: str,
    updated_at: str,
) -> None:
    if expected_status == "in_stock_or_deployed":
        cursor = connection.execute(
            """
            UPDATE assets SET lifecycle_status = ?, updated_at = ?
            WHERE id = (SELECT asset_id FROM rma_cases WHERE id = ?)
            AND lifecycle_status IN ('in_stock', 'deployed')
            """,
            (next_status, updated_at, case_id),
        )
    else:
        cursor = connection.execute(
            """
            UPDATE assets SET lifecycle_status = ?, updated_at = ?
            WHERE id = (SELECT asset_id FROM rma_cases WHERE id = ?)
            AND lifecycle_status = ?
            """,
            (next_status, updated_at, case_id, expected_status),
        )
    if cursor.rowcount != 1:
        raise CaseStateError(f"asset lifecycle must be {expected_status} before this milestone")


def _validate_event_time(connection: sqlite3.Connection, case_id: int, at: str) -> None:
    latest = connection.execute(
        "SELECT occurred_at FROM case_events WHERE case_id = ? ORDER BY sequence DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    if latest is not None and at < latest["occurred_at"]:
        raise CaseValidationError("event time must not precede the previous case event")


def _validate_required_text(value: str, label: str, maximum_length: int) -> str:
    normalised = value.strip()
    if not normalised:
        raise CaseValidationError(f"{label} is required")
    if len(normalised) > maximum_length:
        raise CaseValidationError(f"{label} must be at most {maximum_length} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalised):
        raise CaseValidationError(f"{label} must not contain control characters")
    return normalised


def _validate_outcome(value: str) -> str:
    normalised = _validate_required_text(value, "outcome", 64)
    if normalised not in _OUTCOMES:
        choices = ", ".join(sorted(_OUTCOMES))
        raise CaseValidationError(f"outcome must be one of: {choices}")
    return normalised


def _require_outcome_absent(case: RmaCase) -> None:
    if case.current_outcome is not None:
        raise CaseStateError("an outcome has already been recorded; use an outcome correction")


def _require_outcome_present(case: RmaCase) -> None:
    if case.current_outcome is None:
        raise CaseStateError("an outcome must be recorded before this operation")


def _require_exceptional_outcome(case: RmaCase) -> None:
    _require_outcome_present(case)
    if case.current_outcome not in _EXCEPTIONAL_OUTCOMES:
        allowed = ", ".join(sorted(_EXCEPTIONAL_OUTCOMES))
        raise CaseStateError(f"exceptional closure requires one of: {allowed}")


def _validate_exceptional_asset_status(value: str | None) -> str:
    if value is None:
        raise CaseValidationError("asset status is required for exceptional closure")
    normalised = _validate_required_text(value, "asset status", 64)
    if normalised not in {"in_stock", "retired"}:
        raise CaseValidationError(
            "asset status must be in_stock or retired for exceptional closure"
        )
    return normalised


def _asset_status_before_exceptional_close(case: RmaCase) -> str:
    return "in_rma" if case.current_status == "with_vendor" else "in_stock_or_deployed"


def _validate_optional_text(value: str | None, label: str, maximum_length: int) -> str | None:
    if value is None:
        return None
    return _validate_required_text(value, label, maximum_length)


def _require_case_asset(connection: sqlite3.Connection, tag: str):
    try:
        asset = get_asset(connection, tag)
    except AssetNotFoundError:
        raise CaseStateError(f"asset does not exist: {tag.strip()}") from None
    if asset.lifecycle_status == "retired":
        raise CaseStateError(f"asset is retired: {asset.tag}")
    if asset.lifecycle_status == "in_rma":
        raise CaseStateError(f"asset is already in RMA: {asset.tag}")
    return asset


def _require_case_vendor(connection: sqlite3.Connection, key: str):
    try:
        return require_active_vendor(connection, key)
    except VendorInactiveError:
        raise CaseStateError(f"vendor is inactive: {key.strip()}") from None
    except VendorNotFoundError:
        raise CaseStateError(f"vendor does not exist: {key.strip()}") from None
    except VendorValidationError:
        raise CaseValidationError("vendor key is invalid") from None


def _validate_identifier(value: str, label: str) -> str:
    normalised = value.strip()
    if _IDENTIFIER_PATTERN.fullmatch(normalised) is None:
        raise CaseValidationError(f"{label} must contain only letters, numbers, '.', '_' or '-'")
    return normalised


def _parse_optional_utc_timestamp(value: str | None, label: str) -> str | None:
    return _parse_utc_timestamp(value, label) if value is not None else None


def _parse_utc_timestamp(value: str, label: str) -> str:
    normalised = value.strip()
    if "T" not in normalised or not normalised.endswith("Z"):
        raise CaseValidationError(f"{label} must be a UTC RFC 3339 timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(normalised[:-1] + "+00:00")
    except ValueError:
        raise CaseValidationError(f"{label} must be a UTC RFC 3339 timestamp ending in Z") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise CaseValidationError(f"{label} must be a UTC RFC 3339 timestamp ending in Z")
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _deadline_from_default(opened_at: str, minutes: int | None) -> str | None:
    if minutes is None:
        return None
    opened = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
    return (opened + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _ensure_case_reference_available(connection: sqlite3.Connection, reference: str) -> None:
    row = connection.execute(
        "SELECT 1 FROM rma_cases WHERE case_reference_folded = ?", (reference.casefold(),)
    ).fetchone()
    if row is not None:
        raise CaseConflictError(f"case reference already exists: {reference}")


def _ensure_asset_has_no_active_case(connection: sqlite3.Connection, asset_id: int) -> None:
    row = connection.execute(
        """
        SELECT 1 FROM rma_cases
        WHERE asset_id = ? AND current_status NOT IN ('closed', 'cancelled')
        """,
        (asset_id,),
    ).fetchone()
    if row is not None:
        raise CaseConflictError("asset already has an active RMA case")


_CASE_SELECT = """
SELECT rma_cases.*, assets.asset_tag, vendors.vendor_key
FROM rma_cases
JOIN assets ON assets.id = rma_cases.asset_id
JOIN vendors ON vendors.id = rma_cases.vendor_id
"""


def _select_case_by_id(connection: sqlite3.Connection, case_id: int) -> sqlite3.Row:
    row = connection.execute(_CASE_SELECT + " WHERE rma_cases.id = ?", (case_id,)).fetchone()
    if row is None:
        raise AssertionError("case disappeared during a transaction")
    return row


def _select_case_by_reference(
    connection: sqlite3.Connection, folded_reference: str
) -> sqlite3.Row | None:
    return connection.execute(
        _CASE_SELECT + " WHERE rma_cases.case_reference_folded = ?", (folded_reference,)
    ).fetchone()


def _select_case_event(
    connection: sqlite3.Connection, case_id: int, event_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT event_id, event_type FROM case_events WHERE case_id = ? AND event_id = ?",
        (case_id, event_id),
    ).fetchone()


def _case_from_row(row: sqlite3.Row) -> RmaCase:
    return RmaCase(
        id=row["id"],
        reference=row["case_reference"],
        asset_tag=row["asset_tag"],
        vendor_key=row["vendor_key"],
        opened_at=row["opened_at"],
        current_status=row["current_status"],
        vendor_reference=row["vendor_reference"],
        response_due_at=row["response_due_at"],
        resolution_due_at=row["resolution_due_at"],
        vendor_responded_at=row["vendor_responded_at"],
        outbound_dispatched_at=row["outbound_dispatched_at"],
        vendor_received_at=row["vendor_received_at"],
        return_dispatched_at=row["return_dispatched_at"],
        returned_at=row["returned_at"],
        current_outcome=row["current_outcome"],
        closed_at=row["closed_at"],
        last_event_sequence=row["last_event_sequence"],
        last_event_hash=row["last_event_hash"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event_from_row(row: sqlite3.Row) -> CaseEvent:
    payload: dict[str, Any] = json.loads(row["payload_json"])
    return CaseEvent(
        event_id=row["event_id"],
        case_reference=row["case_reference"],
        sequence=row["sequence"],
        event_type=row["event_type"],
        occurred_at=row["occurred_at"],
        recorded_at=row["recorded_at"],
        operator_alias=row["operator_alias"],
        payload_json=row["payload_json"],
        payload=payload,
        previous_hash=row["previous_hash"],
        event_hash=row["event_hash"],
    )


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@contextmanager
def _write_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Commit a case projection and its event together or not at all."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
