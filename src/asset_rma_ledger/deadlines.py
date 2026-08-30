"""Read-only SLA deadline classification and due-case views."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class DeadlineError(RuntimeError):
    """Base error for deadline evaluation operations."""


class DeadlineValidationError(DeadlineError):
    """Raised when deadline-view inputs violate the public contract."""


@dataclass(frozen=True, slots=True)
class DueCase:
    """One incomplete response or resolution deadline requiring attention."""

    reference: str
    asset_tag: str
    vendor_key: str
    deadline_type: str
    deadline_at: str
    state: str


def classify_deadline(
    *,
    deadline_at: str | None,
    completed_at: str | None,
    as_of: str,
    within_hours: int,
) -> str:
    """Classify one deadline at a deterministic UTC reference time."""
    as_of_time = _parse_utc_timestamp(as_of, "as-of")
    window = _parse_within_hours(within_hours)
    if deadline_at is None:
        return "not_set"
    deadline = _parse_utc_timestamp(deadline_at, "deadline")
    if completed_at is not None:
        completed = _parse_utc_timestamp(completed_at, "completion")
        return "met" if completed <= deadline else "breached"
    if as_of_time > deadline:
        return "overdue"
    if deadline <= as_of_time + window:
        return "due_soon"
    return "on_track"


def due_cases(
    connection: sqlite3.Connection, *, as_of: str, within_hours: int
) -> tuple[DueCase, ...]:
    """Return overdue then upcoming incomplete deadlines in stable UTC order."""
    as_of_time = _parse_utc_timestamp(as_of, "as-of")
    _parse_within_hours(within_hours)
    rows = connection.execute(
        """
        SELECT rma_cases.case_reference, assets.asset_tag, vendors.vendor_key,
               rma_cases.response_due_at, rma_cases.vendor_responded_at,
               rma_cases.resolution_due_at, rma_cases.closed_at
        FROM rma_cases
        JOIN assets ON assets.id = rma_cases.asset_id
        JOIN vendors ON vendors.id = rma_cases.vendor_id
        WHERE rma_cases.current_status NOT IN ('closed', 'cancelled')
        ORDER BY rma_cases.case_reference_folded ASC
        """
    ).fetchall()

    due: list[DueCase] = []
    for row in rows:
        for deadline_type, deadline_at, completed_at in (
            ("response", row["response_due_at"], row["vendor_responded_at"]),
            ("resolution", row["resolution_due_at"], row["closed_at"]),
        ):
            state = classify_deadline(
                deadline_at=deadline_at,
                completed_at=completed_at,
                as_of=as_of_time.isoformat().replace("+00:00", "Z"),
                within_hours=within_hours,
            )
            if state not in {"overdue", "due_soon"} or deadline_at is None:
                continue
            due.append(
                DueCase(
                    reference=row["case_reference"],
                    asset_tag=row["asset_tag"],
                    vendor_key=row["vendor_key"],
                    deadline_type=deadline_type,
                    deadline_at=deadline_at,
                    state=state,
                )
            )

    return tuple(
        sorted(
            due,
            key=lambda item: (
                0 if item.state == "overdue" else 1,
                item.deadline_at,
                0 if item.deadline_type == "response" else 1,
                item.reference.casefold(),
            ),
        )
    )


def _parse_utc_timestamp(value: str, label: str) -> datetime:
    normalised = value.strip()
    if "T" not in normalised or not normalised.endswith("Z"):
        raise DeadlineValidationError(f"{label} must be a UTC RFC 3339 timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(normalised[:-1] + "+00:00")
    except ValueError:
        raise DeadlineValidationError(
            f"{label} must be a UTC RFC 3339 timestamp ending in Z"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise DeadlineValidationError(f"{label} must be a UTC RFC 3339 timestamp ending in Z")
    return parsed.astimezone(UTC).replace(microsecond=0)


def _parse_within_hours(value: int) -> timedelta:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DeadlineValidationError("within hours must be a positive whole number")
    return timedelta(hours=value)
