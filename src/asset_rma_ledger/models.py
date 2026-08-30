"""Typed domain records shared by ledger modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


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
