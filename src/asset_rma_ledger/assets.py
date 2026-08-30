"""Asset registration, warranty and lifecycle operations."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

from .models import Asset
from .vendors import VendorError, VendorNotFoundError, get_vendor

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}", flags=re.ASCII)
_LIFECYCLE_STATUSES = frozenset({"in_stock", "deployed", "retired"})


class AssetError(RuntimeError):
    """Base error for asset domain operations."""


class AssetValidationError(AssetError):
    """Raised when supplied asset input violates the public contract."""


class AssetConflictError(AssetError):
    """Raised when an asset tag or serial number is already registered."""


class AssetNotFoundError(AssetError):
    """Raised when a requested asset does not exist."""


def add_asset(
    connection: sqlite3.Connection,
    *,
    tag: str,
    serial: str,
    asset_type: str,
    manufacturer: str,
    model: str,
    lifecycle_status: str = "in_stock",
    warranty_vendor: str | None = None,
    warranty_reference: str | None = None,
    warranty_start: str | date | None = None,
    warranty_end: str | date | None = None,
) -> Asset:
    """Add one asset with optional warranty metadata."""
    values = _validate_asset_values(
        tag=tag,
        serial=serial,
        asset_type=asset_type,
        manufacturer=manufacturer,
        model=model,
        lifecycle_status=lifecycle_status,
        warranty_reference=warranty_reference,
        warranty_start=warranty_start,
        warranty_end=warranty_end,
    )
    warranty_vendor_id = _lookup_warranty_vendor_id(connection, warranty_vendor)
    timestamp = _utc_now()

    try:
        with _write_transaction(connection):
            _ensure_identifier_available(
                connection,
                column="asset_tag_folded",
                value=values["tag"].casefold(),
                label="asset tag",
            )
            _ensure_identifier_available(
                connection,
                column="serial_number_folded",
                value=values["serial"].casefold(),
                label="serial number",
            )
            cursor = connection.execute(
                """
                INSERT INTO assets (
                    asset_tag, asset_tag_folded, serial_number, serial_number_folded,
                    asset_type, manufacturer, model, lifecycle_status,
                    warranty_vendor_id, warranty_reference, warranty_start, warranty_end,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["tag"],
                    values["tag"].casefold(),
                    values["serial"],
                    values["serial"].casefold(),
                    values["asset_type"],
                    values["manufacturer"],
                    values["model"],
                    values["lifecycle_status"],
                    warranty_vendor_id,
                    values["warranty_reference"],
                    _date_to_storage(values["warranty_start"]),
                    _date_to_storage(values["warranty_end"]),
                    timestamp,
                    timestamp,
                ),
            )
            row = _select_asset_by_id(connection, cursor.lastrowid)
    except sqlite3.IntegrityError as error:
        raise AssetConflictError("asset identifiers must be unique") from error

    return _asset_from_row(row)


def edit_asset(
    connection: sqlite3.Connection,
    tag: str,
    *,
    asset_type: str | None = None,
    manufacturer: str | None = None,
    model: str | None = None,
    warranty_vendor: str | None = None,
    warranty_reference: str | None = None,
    warranty_start: str | date | None = None,
    warranty_end: str | date | None = None,
) -> Asset:
    """Update descriptive or warranty fields without changing asset identifiers."""
    asset_tag = _validate_identifier(tag, "asset tag")
    current = get_asset(connection, asset_tag)
    if current.lifecycle_status == "retired":
        raise AssetValidationError("a retired asset cannot be edited")

    if all(
        value is None
        for value in (
            asset_type,
            manufacturer,
            model,
            warranty_vendor,
            warranty_reference,
            warranty_start,
            warranty_end,
        )
    ):
        raise AssetValidationError("at least one asset field must be supplied")

    warranty_vendor_id = _lookup_warranty_vendor_id(connection, warranty_vendor)
    values = {
        "asset_type": _value_or_current(asset_type, current.asset_type, "asset type", 64),
        "manufacturer": _value_or_current(manufacturer, current.manufacturer, "manufacturer", 200),
        "model": _value_or_current(model, current.model, "model", 200),
        "warranty_reference": _value_or_current(
            warranty_reference,
            current.warranty_reference,
            "warranty reference",
            128,
            required=False,
        ),
        "warranty_start": _date_or_current(
            warranty_start, current.warranty_start, "warranty start"
        ),
        "warranty_end": _date_or_current(warranty_end, current.warranty_end, "warranty end"),
    }
    _validate_warranty_range(values["warranty_start"], values["warranty_end"])

    updates: dict[str, str | int | None] = {}
    if asset_type is not None:
        updates["asset_type"] = values["asset_type"]
    if manufacturer is not None:
        updates["manufacturer"] = values["manufacturer"]
    if model is not None:
        updates["model"] = values["model"]
    if warranty_vendor is not None:
        updates["warranty_vendor_id"] = warranty_vendor_id
    if warranty_reference is not None:
        updates["warranty_reference"] = values["warranty_reference"]
    if warranty_start is not None:
        updates["warranty_start"] = _date_to_storage(values["warranty_start"])
    if warranty_end is not None:
        updates["warranty_end"] = _date_to_storage(values["warranty_end"])

    return _update_asset(connection, asset_tag, updates)


def identify_asset(
    connection: sqlite3.Connection,
    tag: str,
    *,
    new_tag: str | None = None,
    serial: str | None = None,
) -> Asset:
    """Correct an asset's tag and/or serial number with uniqueness checks."""
    asset_tag = _validate_identifier(tag, "asset tag")
    current = get_asset(connection, asset_tag)
    if current.lifecycle_status == "retired":
        raise AssetValidationError("a retired asset cannot be identified")
    if new_tag is None and serial is None:
        raise AssetValidationError("a replacement asset tag or serial number is required")

    updated_tag = _validate_identifier(new_tag, "asset tag") if new_tag is not None else current.tag
    updated_serial = (
        _validate_text(serial, "serial number", 128) if serial is not None else current.serial
    )
    with _write_transaction(connection):
        if updated_tag.casefold() != current.tag.casefold():
            _ensure_identifier_available(
                connection,
                column="asset_tag_folded",
                value=updated_tag.casefold(),
                label="asset tag",
            )
        if updated_serial.casefold() != current.serial.casefold():
            _ensure_identifier_available(
                connection,
                column="serial_number_folded",
                value=updated_serial.casefold(),
                label="serial number",
            )
        connection.execute(
            """
            UPDATE assets
            SET asset_tag = ?, asset_tag_folded = ?, serial_number = ?,
                serial_number_folded = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                updated_tag,
                updated_tag.casefold(),
                updated_serial,
                updated_serial.casefold(),
                _utc_now(),
                current.id,
            ),
        )
        row = _select_asset_by_id(connection, current.id)
    return _asset_from_row(row)


def retire_asset(connection: sqlite3.Connection, tag: str) -> Asset:
    """Retire an asset only when it has no active RMA case."""
    asset_tag = _validate_identifier(tag, "asset tag")
    current = get_asset(connection, asset_tag)
    if current.lifecycle_status == "in_rma":
        raise AssetValidationError("an asset in RMA cannot be retired")

    with _write_transaction(connection):
        active_case = connection.execute(
            """
            SELECT 1 FROM rma_cases
            WHERE asset_id = ? AND current_status NOT IN ('closed', 'cancelled')
            """,
            (current.id,),
        ).fetchone()
        if active_case is not None:
            raise AssetValidationError("an asset with an active RMA case cannot be retired")
        connection.execute(
            "UPDATE assets SET lifecycle_status = 'retired', updated_at = ? WHERE id = ?",
            (_utc_now(), current.id),
        )
        row = _select_asset_by_id(connection, current.id)
    return _asset_from_row(row)


def get_asset(connection: sqlite3.Connection, tag: str) -> Asset:
    """Return an asset by its case-insensitive asset tag."""
    asset_tag = _validate_identifier(tag, "asset tag")
    row = _select_asset_by_tag(connection, asset_tag.casefold())
    if row is None:
        raise AssetNotFoundError(f"asset does not exist: {asset_tag}")
    return _asset_from_row(row)


def list_assets(connection: sqlite3.Connection) -> tuple[Asset, ...]:
    """Return assets in deterministic case-insensitive asset-tag order."""
    rows = connection.execute(
        _ASSET_SELECT + " ORDER BY assets.asset_tag_folded ASC, assets.id ASC"
    ).fetchall()
    return tuple(_asset_from_row(row) for row in rows)


def _update_asset(
    connection: sqlite3.Connection, asset_tag: str, updates: dict[str, str | int | None]
) -> Asset:
    assignments = ", ".join(f"{column} = ?" for column in updates)
    parameters = [*updates.values(), _utc_now(), asset_tag.casefold()]
    with _write_transaction(connection):
        cursor = connection.execute(
            f"UPDATE assets SET {assignments}, updated_at = ? WHERE asset_tag_folded = ?",
            parameters,
        )
        if cursor.rowcount == 0:
            raise AssetNotFoundError(f"asset does not exist: {asset_tag}")
        row = _select_asset_by_tag(connection, asset_tag.casefold())
    return _asset_from_row(row)


def _validate_asset_values(
    *,
    tag: str,
    serial: str,
    asset_type: str,
    manufacturer: str,
    model: str,
    lifecycle_status: str,
    warranty_reference: str | None,
    warranty_start: str | date | None,
    warranty_end: str | date | None,
) -> dict[str, str | date | None]:
    validated_status = _validate_lifecycle_status(lifecycle_status)
    start = _validate_date(warranty_start, "warranty start")
    end = _validate_date(warranty_end, "warranty end")
    _validate_warranty_range(start, end)
    return {
        "tag": _validate_identifier(tag, "asset tag"),
        "serial": _validate_text(serial, "serial number", 128),
        "asset_type": _validate_text(asset_type, "asset type", 64),
        "manufacturer": _validate_text(manufacturer, "manufacturer", 200),
        "model": _validate_text(model, "model", 200),
        "lifecycle_status": validated_status,
        "warranty_reference": _validate_text(
            warranty_reference, "warranty reference", 128, required=False
        ),
        "warranty_start": start,
        "warranty_end": end,
    }


def _validate_identifier(value: str, label: str) -> str:
    normalised = _validate_text(value, label, 64)
    if _IDENTIFIER_PATTERN.fullmatch(normalised) is None:
        raise AssetValidationError(f"{label} must contain only letters, numbers, '.', '_' or '-'")
    return normalised


def _validate_text(
    value: str | None, label: str, maximum_length: int, *, required: bool = True
) -> str | None:
    if value is None:
        if required:
            raise AssetValidationError(f"{label} is required")
        return None
    normalised = value.strip()
    if not normalised:
        if required:
            raise AssetValidationError(f"{label} is required")
        return None
    if len(normalised) > maximum_length:
        raise AssetValidationError(f"{label} must be at most {maximum_length} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalised):
        raise AssetValidationError(f"{label} must not contain control characters")
    return normalised


def _validate_date(value: str | date | None, label: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    normalised = value.strip()
    if not normalised:
        return None
    try:
        return date.fromisoformat(normalised)
    except ValueError:
        raise AssetValidationError(f"{label} must be an ISO calendar date") from None


def _validate_warranty_range(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and end < start:
        raise AssetValidationError("warranty end must not precede warranty start")


def _validate_lifecycle_status(value: str) -> str:
    normalised = value.strip()
    if normalised == "in_rma":
        raise AssetValidationError("in_rma cannot be selected outside the RMA workflow")
    if normalised not in _LIFECYCLE_STATUSES:
        choices = ", ".join(sorted(_LIFECYCLE_STATUSES))
        raise AssetValidationError(f"asset status must be one of: {choices}")
    return normalised


def _lookup_warranty_vendor_id(connection: sqlite3.Connection, key: str | None) -> int | None:
    if key is None:
        return None
    try:
        return get_vendor(connection, key).id
    except VendorNotFoundError:
        raise AssetValidationError(f"warranty vendor does not exist: {key.strip()}") from None
    except VendorError:
        raise AssetValidationError("warranty vendor is invalid") from None


def _value_or_current(
    value: str | None,
    current: str | None,
    label: str,
    maximum_length: int,
    *,
    required: bool = True,
) -> str | None:
    if value is None:
        return current
    return _validate_text(value, label, maximum_length, required=required)


def _date_or_current(value: str | date | None, current: date | None, label: str) -> date | None:
    return current if value is None else _validate_date(value, label)


def _ensure_identifier_available(
    connection: sqlite3.Connection, *, column: str, value: str, label: str
) -> None:
    row = connection.execute(f"SELECT 1 FROM assets WHERE {column} = ?", (value,)).fetchone()
    if row is not None:
        raise AssetConflictError(f"{label} already exists")


_ASSET_SELECT = """
SELECT assets.*, vendors.vendor_key AS warranty_vendor_key
FROM assets
LEFT JOIN vendors ON vendors.id = assets.warranty_vendor_id
"""


def _select_asset_by_id(connection: sqlite3.Connection, asset_id: int) -> sqlite3.Row:
    row = connection.execute(_ASSET_SELECT + " WHERE assets.id = ?", (asset_id,)).fetchone()
    if row is None:
        raise AssertionError("asset disappeared during a transaction")
    return row


def _select_asset_by_tag(connection: sqlite3.Connection, folded_tag: str) -> sqlite3.Row | None:
    return connection.execute(
        _ASSET_SELECT + " WHERE assets.asset_tag_folded = ?", (folded_tag,)
    ).fetchone()


def _asset_from_row(row: sqlite3.Row) -> Asset:
    return Asset(
        id=row["id"],
        tag=row["asset_tag"],
        serial=row["serial_number"],
        asset_type=row["asset_type"],
        manufacturer=row["manufacturer"],
        model=row["model"],
        lifecycle_status=row["lifecycle_status"],
        warranty_vendor_key=row["warranty_vendor_key"],
        warranty_reference=row["warranty_reference"],
        warranty_start=_date_from_storage(row["warranty_start"]),
        warranty_end=_date_from_storage(row["warranty_end"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _date_from_storage(value: str | None) -> date | None:
    return date.fromisoformat(value) if value is not None else None


def _date_to_storage(value: str | date | None) -> str | None:
    return value.isoformat() if isinstance(value, date) else value


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@contextmanager
def _write_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Commit one write operation in full or roll it back in full."""
    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
