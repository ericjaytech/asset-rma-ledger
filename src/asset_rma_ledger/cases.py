"""RMA case opening and immutable opening-event operations."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterator
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
