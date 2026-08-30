"""Typed domain records shared by ledger modules."""

from __future__ import annotations

from dataclasses import dataclass


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
