"""Typed domain records shared by ledger modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True, slots=True)
class Vendor:
    """A vendor's current register record."""

    id: int
    key: str
    name: str
    support_url: str | None
    support_email: str | None
    support_phone: str | None
    account_reference: str | None
    response_sla_minutes: int | None
    resolution_sla_minutes: int | None
    active: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Asset:
    """An asset's current register record."""

    id: int
    tag: str
    serial: str
    asset_type: str
    manufacturer: str
    model: str
    lifecycle_status: str
    warranty_vendor_key: str | None
    warranty_reference: str | None
    warranty_start: date | None
    warranty_end: date | None
    created_at: str
    updated_at: str

    def warranty_state(self, as_of: date) -> str:
        """Return the warranty state at an explicit calendar date."""
        if self.warranty_start is None and self.warranty_end is None:
            return "not_recorded"
        if self.warranty_start is not None and as_of < self.warranty_start:
            return "not_started"
        if self.warranty_end is not None and as_of > self.warranty_end:
            return "expired"
        return "active"


@dataclass(frozen=True, slots=True)
class RmaCase:
    """An RMA case's current-state projection."""

    id: int
    reference: str
    asset_tag: str
    vendor_key: str
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
    last_event_hash: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class CaseEvent:
    """One immutable event in a case-local hash chain."""

    event_id: str
    case_reference: str
    sequence: int
    event_type: str
    occurred_at: str
    recorded_at: str
    operator_alias: str
    payload_json: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str
