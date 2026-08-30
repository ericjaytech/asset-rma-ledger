"""Read-only integrity, event-chain and projection verification."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .database import APPLICATION_ID, SCHEMA_STATEMENTS, SCHEMA_VERSION
from .events import ZERO_EVENT_HASH, calculate_event_hash, canonical_json

_ACTIVE_STATUSES = frozenset(
    {"open", "authorised", "outbound", "with_vendor", "returning", "returned"}
)
_OUTCOME_STATUSES = frozenset({"open", "authorised", "with_vendor", "returned"})
_OUTCOMES = frozenset(
    {"repaired", "replaced", "refund", "no_fault_found", "repair_declined", "written_off", "other"}
)
_PROJECTION_FIELDS = (
    "opened_at",
    "current_status",
    "vendor_reference",
    "response_due_at",
    "resolution_due_at",
    "vendor_responded_at",
    "outbound_dispatched_at",
    "vendor_received_at",
    "return_dispatched_at",
    "returned_at",
    "current_outcome",
    "closed_at",
    "last_event_sequence",
    "last_event_hash",
    "created_at",
    "updated_at",
)


class VerificationError(RuntimeError):
    """Raised when a named ledger integrity check fails."""


@dataclass(frozen=True, slots=True)
class VerificationSummary:
    """Concise counts for a successful verification run."""

    checks: int
    cases: int
    events: int


@dataclass(slots=True)
class _Projection:
    opened_at: str
    current_status: str
    vendor_reference: str | None
    response_due_at: str | None
    resolution_due_at: str | None
    vendor_responded_at: str | None
    outbound_dispatched_at: str | None
    vendor_received_at: str | None
    return_dispatched_at: str | None
    returned_at: str | None
    current_outcome: str | None
    closed_at: str | None
    last_event_sequence: int
    last_event_hash: str
    created_at: str
    updated_at: str


def verify_database(connection: sqlite3.Connection) -> VerificationSummary:
    """Verify the ledger without changing database content."""
    previous_query_only = connection.execute("PRAGMA query_only").fetchone()[0]
    owns_transaction = not connection.in_transaction
    connection.execute("PRAGMA query_only = ON")
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        _verify_sqlite_integrity(connection)
        _verify_foreign_keys(connection)
        _verify_schema(connection)
        cases = connection.execute(
            "SELECT * FROM rma_cases ORDER BY case_reference_folded ASC"
        ).fetchall()
        event_rows = connection.execute(
            "SELECT * FROM case_events ORDER BY case_id ASC, sequence ASC"
        ).fetchall()
        events_by_case: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for event in event_rows:
            events_by_case[event["case_id"]].append(event)
        for case in cases:
            _verify_case(case, events_by_case.get(case["id"], []))
        return VerificationSummary(checks=6, cases=len(cases), events=len(event_rows))
    except VerificationError:
        raise
    except sqlite3.Error as error:
        raise VerificationError("SQLite verification could not complete") from error
    finally:
        if owns_transaction:
            connection.rollback()
        connection.execute(f"PRAGMA query_only = {int(previous_query_only)}")


def _verify_sqlite_integrity(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if not rows or any(row[0] != "ok" for row in rows):
        raise VerificationError("SQLite integrity check failed")


def _verify_foreign_keys(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise VerificationError("foreign-key check failed: enforcement is disabled")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise VerificationError("foreign-key check failed")


def _verify_schema(connection: sqlite3.Connection) -> None:
    metadata = connection.execute(
        "SELECT application_id, schema_version FROM schema_metadata WHERE singleton = 1"
    ).fetchone()
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if (
        metadata is None
        or metadata["application_id"] != APPLICATION_ID
        or metadata["schema_version"] != SCHEMA_VERSION
        or user_version != SCHEMA_VERSION
    ):
        raise VerificationError("schema check failed: metadata mismatch")

    actual = {
        row["name"]: _normalise_sql(row["sql"])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_schema WHERE sql IS NOT NULL"
        ).fetchall()
    }
    for statement in SCHEMA_STATEMENTS:
        match = re.search(
            r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER)\s+([A-Za-z0-9_]+)",
            statement,
            flags=re.IGNORECASE,
        )
        if match is None:
            continue
        name = match.group(1)
        if actual.get(name) != _normalise_sql(statement):
            kind = "trigger" if "TRIGGER" in statement.upper() else "object"
            raise VerificationError(f"schema check failed: required {kind} {name}")


def _normalise_sql(value: str) -> str:
    return " ".join(value.split()).casefold()


def _verify_case(case: sqlite3.Row, events: list[sqlite3.Row]) -> None:
    reference = case["case_reference"]
    if case["case_reference_folded"] != reference.casefold():
        raise VerificationError(
            f"projection check failed for case {reference}: case_reference_folded"
        )
    if not events:
        raise VerificationError(f"event chain check failed for case {reference}")
    expected_previous = ZERO_EVENT_HASH
    previous_occurred_at: str | None = None
    payloads: list[dict[str, Any]] = []
    for expected_sequence, event in enumerate(events, start=1):
        try:
            payload = json.loads(event["payload_json"])
        except (TypeError, json.JSONDecodeError):
            raise VerificationError(f"event chain check failed for case {reference}") from None
        if not isinstance(payload, dict) or canonical_json(payload) != event["payload_json"]:
            raise VerificationError(f"event chain check failed for case {reference}")
        try:
            occurred_at = _canonical_timestamp(event["occurred_at"])
            _canonical_timestamp(event["recorded_at"])
        except ValueError:
            raise VerificationError(f"event chain check failed for case {reference}") from None
        expected_hash = calculate_event_hash(
            case_reference=reference,
            sequence=event["sequence"],
            event_id=event["event_id"],
            event_type=event["event_type"],
            occurred_at=event["occurred_at"],
            recorded_at=event["recorded_at"],
            operator_alias=event["operator_alias"],
            payload_json=event["payload_json"],
            previous_hash=event["previous_hash"],
        )
        if (
            event["sequence"] != expected_sequence
            or event["previous_hash"] != expected_previous
            or event["event_hash"] != expected_hash
            or (previous_occurred_at is not None and occurred_at < previous_occurred_at)
        ):
            raise VerificationError(f"event chain check failed for case {reference}")
        payloads.append(payload)
        expected_previous = event["event_hash"]
        previous_occurred_at = occurred_at

    try:
        replayed = _replay_events(events, payloads)
    except (KeyError, TypeError, ValueError):
        raise VerificationError(f"lifecycle replay failed for case {reference}") from None
    for field in _PROJECTION_FIELDS:
        if getattr(replayed, field) != case[field]:
            raise VerificationError(f"projection check failed for case {reference}: {field}")


def _replay_events(events: list[sqlite3.Row], payloads: list[dict[str, Any]]) -> _Projection:
    first = events[0]
    opening = payloads[0]
    if first["event_type"] != "case_opened" or first["previous_hash"] != ZERO_EVENT_HASH:
        raise ValueError("invalid opening event")
    state = _Projection(
        opened_at=first["occurred_at"],
        current_status="open",
        vendor_reference=None,
        response_due_at=opening["response_due_at"],
        resolution_due_at=opening["resolution_due_at"],
        vendor_responded_at=None,
        outbound_dispatched_at=None,
        vendor_received_at=None,
        return_dispatched_at=None,
        returned_at=None,
        current_outcome=None,
        closed_at=None,
        last_event_sequence=first["sequence"],
        last_event_hash=first["event_hash"],
        created_at=first["recorded_at"],
        updated_at=first["recorded_at"],
    )
    outcome_event_ids: set[str] = set()
    for event, payload in zip(events[1:], payloads[1:], strict=True):
        _apply_event(state, event, payload, outcome_event_ids)
        state.last_event_sequence = event["sequence"]
        state.last_event_hash = event["event_hash"]
        state.updated_at = event["recorded_at"]
    return state


def _apply_event(
    state: _Projection,
    event: sqlite3.Row,
    payload: dict[str, Any],
    outcome_event_ids: set[str],
) -> None:
    event_type = event["event_type"]
    occurred_at = event["occurred_at"]
    if event_type == "vendor_response_recorded":
        _require_status(state, _ACTIVE_STATUSES)
        if state.vendor_responded_at is not None:
            raise ValueError("duplicate response")
        state.vendor_responded_at = occurred_at
        state.vendor_reference = payload["vendor_reference"]
    elif event_type == "status_changed":
        if (
            state.current_status != "open"
            or payload["from_status"] != "open"
            or payload["to_status"] != "authorised"
        ):
            raise ValueError("invalid authorisation")
        state.current_status = "authorised"
    elif event_type == "outbound_dispatched":
        _require_status(state, frozenset({"authorised"}))
        state.current_status = "outbound"
        state.outbound_dispatched_at = occurred_at
    elif event_type == "vendor_receipt_recorded":
        _require_status(state, frozenset({"outbound"}))
        state.current_status = "with_vendor"
        state.vendor_received_at = occurred_at
    elif event_type == "return_dispatched":
        _require_status(state, frozenset({"with_vendor"}))
        state.current_status = "returning"
        state.return_dispatched_at = occurred_at
    elif event_type == "return_received":
        _require_status(state, frozenset({"returning"}))
        state.current_status = "returned"
        state.returned_at = occurred_at
    elif event_type == "deadline_changed":
        _require_status(state, _ACTIVE_STATUSES)
        if (
            payload["previous_response_due_at"] != state.response_due_at
            or payload["previous_resolution_due_at"] != state.resolution_due_at
        ):
            raise ValueError("deadline lineage mismatch")
        state.response_due_at = payload["response_due_at"]
        state.resolution_due_at = payload["resolution_due_at"]
    elif event_type == "outcome_recorded":
        _require_status(state, _OUTCOME_STATUSES)
        outcome = payload["outcome"]
        if state.current_outcome is not None or outcome not in _OUTCOMES:
            raise ValueError("invalid outcome")
        state.current_outcome = outcome
        outcome_event_ids.add(event["event_id"])
    elif event_type == "correction_recorded":
        _require_status(state, _OUTCOME_STATUSES)
        replacement = payload["replacement_value"]
        if (
            payload["field"] != "outcome"
            or payload["original_event_id"] not in outcome_event_ids
            or payload["previous_value"] != state.current_outcome
            or replacement not in _OUTCOMES
            or replacement == state.current_outcome
        ):
            raise ValueError("invalid correction")
        state.current_outcome = replacement
    elif event_type == "note_added":
        _require_status(state, _ACTIVE_STATUSES)
        if not isinstance(payload["note"], str):
            raise ValueError("invalid note")
    elif event_type == "case_closed":
        if state.current_outcome is None or payload["outcome"] != state.current_outcome:
            raise ValueError("invalid closure outcome")
        closure_type = payload["closure_type"]
        if closure_type == "returned":
            _require_status(state, frozenset({"returned"}))
        elif closure_type == "exceptional":
            _require_status(state, frozenset({"open", "authorised", "with_vendor"}))
        else:
            raise ValueError("invalid closure type")
        state.current_status = "closed"
        state.closed_at = occurred_at
    elif event_type == "case_cancelled":
        _require_status(state, frozenset({"open", "authorised"}))
        state.current_status = "cancelled"
    else:
        raise ValueError("unsupported event type")


def _require_status(state: _Projection, allowed: frozenset[str]) -> None:
    if state.current_status not in allowed:
        raise ValueError("invalid lifecycle status")


def _canonical_timestamp(value: str) -> str:
    if not isinstance(value, str) or "T" not in value or not value.endswith("Z"):
        raise ValueError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("invalid timestamp") from None
    canonical = parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise ValueError("non-canonical timestamp")
    return canonical
