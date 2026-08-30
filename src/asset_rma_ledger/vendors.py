"""Vendor registration and lookup operations."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from .models import Vendor

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}", flags=re.ASCII)
_EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+", flags=re.ASCII)


class VendorError(RuntimeError):
    """Base error for vendor domain operations."""


class VendorValidationError(VendorError):
    """Raised when supplied vendor input violates the public contract."""


class VendorConflictError(VendorError):
    """Raised when a vendor key is already registered."""


class VendorNotFoundError(VendorError):
    """Raised when a requested vendor does not exist."""


class VendorInactiveError(VendorError):
    """Raised when an inactive vendor is selected for a new case."""


def add_vendor(
    connection: sqlite3.Connection,
    *,
    key: str,
    name: str,
    support_url: str | None = None,
    support_email: str | None = None,
    support_phone: str | None = None,
    account_reference: str | None = None,
    response_sla_hours: str | Decimal | None = None,
    resolution_sla_hours: str | Decimal | None = None,
) -> Vendor:
    """Add an active vendor and return its current register record."""
    vendor_key = _validate_identifier(key, "vendor key")
    values = _validate_vendor_fields(
        name=name,
        support_url=support_url,
        support_email=support_email,
        support_phone=support_phone,
        account_reference=account_reference,
        response_sla_hours=response_sla_hours,
        resolution_sla_hours=resolution_sla_hours,
    )
    timestamp = _utc_now()

    try:
        with _write_transaction(connection):
            existing = connection.execute(
                "SELECT 1 FROM vendors WHERE vendor_key_folded = ?",
                (vendor_key.casefold(),),
            ).fetchone()
            if existing is not None:
                raise VendorConflictError(f"vendor already exists: {vendor_key}")

            cursor = connection.execute(
                """
                INSERT INTO vendors (
                    vendor_key, vendor_key_folded, name, support_url, support_email,
                    support_phone, account_reference, response_sla_minutes,
                    resolution_sla_minutes, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vendor_key,
                    vendor_key.casefold(),
                    values["name"],
                    values["support_url"],
                    values["support_email"],
                    values["support_phone"],
                    values["account_reference"],
                    values["response_sla_minutes"],
                    values["resolution_sla_minutes"],
                    1,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM vendors WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
    except sqlite3.IntegrityError as error:
        raise VendorConflictError(f"vendor already exists: {vendor_key}") from error

    return _vendor_from_row(row)


def edit_vendor(
    connection: sqlite3.Connection,
    key: str,
    *,
    name: str | None = None,
    support_url: str | None = None,
    support_email: str | None = None,
    support_phone: str | None = None,
    account_reference: str | None = None,
    response_sla_hours: str | Decimal | None = None,
    resolution_sla_hours: str | Decimal | None = None,
) -> Vendor:
    """Update supplied vendor fields without changing the vendor identifier."""
    vendor_key = _validate_identifier(key, "vendor key")
    updates = _validated_vendor_updates(
        name=name,
        support_url=support_url,
        support_email=support_email,
        support_phone=support_phone,
        account_reference=account_reference,
        response_sla_hours=response_sla_hours,
        resolution_sla_hours=resolution_sla_hours,
    )
    if not updates:
        raise VendorValidationError("at least one vendor field must be supplied")

    assignments = ", ".join(f"{column} = ?" for column in updates)
    parameters = [*updates.values(), _utc_now(), vendor_key.casefold()]
    with _write_transaction(connection):
        cursor = connection.execute(
            f"UPDATE vendors SET {assignments}, updated_at = ? WHERE vendor_key_folded = ?",
            parameters,
        )
        if cursor.rowcount == 0:
            raise VendorNotFoundError(f"vendor does not exist: {vendor_key}")
        row = connection.execute(
            "SELECT * FROM vendors WHERE vendor_key_folded = ?",
            (vendor_key.casefold(),),
        ).fetchone()

    return _vendor_from_row(row)


def get_vendor(connection: sqlite3.Connection, key: str) -> Vendor:
    """Return a vendor by its case-insensitive key."""
    vendor_key = _validate_identifier(key, "vendor key")
    row = connection.execute(
        "SELECT * FROM vendors WHERE vendor_key_folded = ?",
        (vendor_key.casefold(),),
    ).fetchone()
    if row is None:
        raise VendorNotFoundError(f"vendor does not exist: {vendor_key}")
    return _vendor_from_row(row)


def require_active_vendor(connection: sqlite3.Connection, key: str) -> Vendor:
    """Return a vendor that is valid for opening a new case."""
    vendor = get_vendor(connection, key)
    if not vendor.active:
        raise VendorInactiveError(f"vendor is inactive: {vendor.key}")
    return vendor


def list_vendors(connection: sqlite3.Connection) -> tuple[Vendor, ...]:
    """Return active and inactive vendors in deterministic key order."""
    rows = connection.execute(
        "SELECT * FROM vendors ORDER BY vendor_key_folded ASC, id ASC"
    ).fetchall()
    return tuple(_vendor_from_row(row) for row in rows)


def set_vendor_active(connection: sqlite3.Connection, key: str, *, active: bool) -> Vendor:
    """Activate or deactivate a vendor without deleting its historical record."""
    vendor_key = _validate_identifier(key, "vendor key")
    with _write_transaction(connection):
        cursor = connection.execute(
            "UPDATE vendors SET active = ?, updated_at = ? WHERE vendor_key_folded = ?",
            (int(active), _utc_now(), vendor_key.casefold()),
        )
        if cursor.rowcount == 0:
            raise VendorNotFoundError(f"vendor does not exist: {vendor_key}")
        row = connection.execute(
            "SELECT * FROM vendors WHERE vendor_key_folded = ?",
            (vendor_key.casefold(),),
        ).fetchone()

    return _vendor_from_row(row)


@contextmanager
def _write_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Commit one write operation in full or roll it back in full."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _validate_vendor_fields(
    *,
    name: str,
    support_url: str | None,
    support_email: str | None,
    support_phone: str | None,
    account_reference: str | None,
    response_sla_hours: str | Decimal | None,
    resolution_sla_hours: str | Decimal | None,
) -> dict[str, str | int | None]:
    """Validate all fields required to create a vendor."""
    return {
        "name": _validate_text(name, "vendor name", maximum_length=200, required=True),
        "support_url": _validate_support_url(support_url),
        "support_email": _validate_email(support_email),
        "support_phone": _validate_text(
            support_phone, "support phone", maximum_length=64, required=False
        ),
        "account_reference": _validate_text(
            account_reference, "account reference", maximum_length=128, required=False
        ),
        "response_sla_minutes": _hours_to_minutes(response_sla_hours, "response SLA"),
        "resolution_sla_minutes": _hours_to_minutes(resolution_sla_hours, "resolution SLA"),
    }


def _validated_vendor_updates(
    *,
    name: str | None,
    support_url: str | None,
    support_email: str | None,
    support_phone: str | None,
    account_reference: str | None,
    response_sla_hours: str | Decimal | None,
    resolution_sla_hours: str | Decimal | None,
) -> dict[str, str | int | None]:
    """Validate only fields the caller supplied for an update."""
    updates: dict[str, str | int | None] = {}
    if name is not None:
        updates["name"] = _validate_text(name, "vendor name", maximum_length=200, required=True)
    if support_url is not None:
        updates["support_url"] = _validate_support_url(support_url)
    if support_email is not None:
        updates["support_email"] = _validate_email(support_email)
    if support_phone is not None:
        updates["support_phone"] = _validate_text(
            support_phone, "support phone", maximum_length=64, required=False
        )
    if account_reference is not None:
        updates["account_reference"] = _validate_text(
            account_reference, "account reference", maximum_length=128, required=False
        )
    if response_sla_hours is not None:
        updates["response_sla_minutes"] = _hours_to_minutes(response_sla_hours, "response SLA")
    if resolution_sla_hours is not None:
        updates["resolution_sla_minutes"] = _hours_to_minutes(
            resolution_sla_hours, "resolution SLA"
        )
    return updates


def _validate_identifier(value: str, label: str) -> str:
    normalised = _validate_text(value, label, maximum_length=64, required=True)
    if _IDENTIFIER_PATTERN.fullmatch(normalised) is None:
        raise VendorValidationError(f"{label} must contain only letters, numbers, '.', '_' or '-'")
    return normalised


def _validate_support_url(value: str | None) -> str | None:
    normalised = _validate_text(value, "support URL", maximum_length=2048, required=False)
    if normalised is None:
        return None
    try:
        parsed = urlsplit(normalised)
    except ValueError:
        raise VendorValidationError(
            "support URL must be an HTTPS URL without credentials"
        ) from None
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise VendorValidationError("support URL must be an HTTPS URL without credentials")
    return normalised


def _validate_email(value: str | None) -> str | None:
    normalised = _validate_text(value, "support email", maximum_length=254, required=False)
    if normalised is None:
        return None
    if _EMAIL_PATTERN.fullmatch(normalised) is None:
        raise VendorValidationError("support email must be a simple email address")
    return normalised


def _validate_text(
    value: str | None, label: str, *, maximum_length: int, required: bool
) -> str | None:
    if value is None:
        if required:
            raise VendorValidationError(f"{label} is required")
        return None
    normalised = value.strip()
    if not normalised:
        if required:
            raise VendorValidationError(f"{label} is required")
        return None
    if len(normalised) > maximum_length:
        raise VendorValidationError(f"{label} must be at most {maximum_length} characters")
    if any(character.isspace() and character not in {" "} for character in normalised):
        raise VendorValidationError(f"{label} must not contain control characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalised):
        raise VendorValidationError(f"{label} must not contain control characters")
    return normalised


def _hours_to_minutes(value: str | Decimal | None, label: str) -> int | None:
    if value is None:
        return None
    try:
        hours = Decimal(value)
    except (InvalidOperation, ValueError):
        raise VendorValidationError(f"{label} must be a number of hours") from None
    if not hours.is_finite() or hours <= 0:
        raise VendorValidationError(f"{label} must be positive")
    minutes = hours * Decimal(60)
    if minutes != minutes.to_integral_value():
        raise VendorValidationError(f"{label} must resolve to a whole minute")
    return int(minutes)


def _vendor_from_row(row: sqlite3.Row) -> Vendor:
    return Vendor(
        id=row["id"],
        key=row["vendor_key"],
        name=row["name"],
        support_url=row["support_url"],
        support_email=row["support_email"],
        support_phone=row["support_phone"],
        account_reference=row["account_reference"],
        response_sla_minutes=row["response_sla_minutes"],
        resolution_sla_minutes=row["resolution_sla_minutes"],
        active=bool(row["active"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
