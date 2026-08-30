"""Bounded, transactional import of canonical ledger CSV files."""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .assets import AssetError, add_asset
from .cases import CaseError, import_case_snapshot
from .vendors import VendorError, add_vendor, set_vendor_active

_MAX_BYTES = 100 * 1024 * 1024
_MAX_ROWS = 250_000
_VENDOR_FIELDS = (
    "vendor_key",
    "name",
    "support_url",
    "support_email",
    "support_phone",
    "account_reference",
    "response_sla_hours",
    "resolution_sla_hours",
    "active",
)
_ASSET_FIELDS = (
    "asset_tag",
    "serial_number",
    "asset_type",
    "manufacturer",
    "model",
    "lifecycle_status",
    "warranty_vendor",
    "warranty_reference",
    "warranty_start",
    "warranty_end",
)
_CASE_FIELDS = (
    "case_reference",
    "asset_tag",
    "vendor_key",
    "opened_at",
    "status",
    "vendor_reference",
    "response_due_at",
    "resolution_due_at",
    "vendor_responded_at",
    "outbound_dispatched_at",
    "vendor_received_at",
    "return_dispatched_at",
    "returned_at",
    "outcome",
    "closed_at",
)


class CsvImportError(RuntimeError):
    """Raised when a CSV file cannot be imported safely."""


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """The result of one successful import or dry run."""

    kind: str
    rows: int
    dry_run: bool


def import_vendors_csv(
    connection: sqlite3.Connection, path: Path, *, dry_run: bool = False
) -> ImportSummary:
    """Import new vendor records or roll the complete file back."""
    rows = _read_rows(path, _VENDOR_FIELDS)
    try:
        with _import_transaction(connection, dry_run=dry_run):
            for number, row in rows:
                vendor = add_vendor(
                    connection,
                    key=row["vendor_key"],
                    name=row["name"],
                    support_url=_none(row["support_url"]),
                    support_email=_none(row["support_email"]),
                    support_phone=_none(row["support_phone"]),
                    account_reference=_none(row["account_reference"]),
                    response_sla_hours=_none(row["response_sla_hours"]),
                    resolution_sla_hours=_none(row["resolution_sla_hours"]),
                )
                if _parse_active(row["active"], number) is False:
                    set_vendor_active(connection, vendor.key, active=False)
    except VendorError as error:
        raise CsvImportError(f"row {number}: {error}") from None
    return ImportSummary("vendors", len(rows), dry_run)


def import_assets_csv(
    connection: sqlite3.Connection, path: Path, *, dry_run: bool = False
) -> ImportSummary:
    """Import new asset records or roll the complete file back."""
    rows = _read_rows(path, _ASSET_FIELDS)
    try:
        with _import_transaction(connection, dry_run=dry_run):
            for _number, row in rows:
                add_asset(
                    connection,
                    tag=row["asset_tag"],
                    serial=row["serial_number"],
                    asset_type=row["asset_type"],
                    manufacturer=row["manufacturer"],
                    model=row["model"],
                    lifecycle_status=row["lifecycle_status"],
                    warranty_vendor=_none(row["warranty_vendor"]),
                    warranty_reference=_none(row["warranty_reference"]),
                    warranty_start=_none(row["warranty_start"]),
                    warranty_end=_none(row["warranty_end"]),
                )
    except AssetError as error:
        raise CsvImportError(f"row {_number}: {error}") from None
    return ImportSummary("assets", len(rows), dry_run)


def import_cases_csv(
    connection: sqlite3.Connection,
    path: Path,
    *,
    operator_alias: str,
    dry_run: bool = False,
) -> ImportSummary:
    """Import case snapshots as source-marked reconstructed histories."""
    rows = _read_rows(path, _CASE_FIELDS)
    try:
        with _import_transaction(connection, dry_run=dry_run):
            for _number, row in rows:
                import_case_snapshot(
                    connection,
                    reference=row["case_reference"],
                    asset_tag=row["asset_tag"],
                    vendor_key=row["vendor_key"],
                    opened_at=row["opened_at"],
                    status=row["status"],
                    operator_alias=operator_alias,
                    vendor_reference=_none(row["vendor_reference"]),
                    response_due_at=_none(row["response_due_at"]),
                    resolution_due_at=_none(row["resolution_due_at"]),
                    vendor_responded_at=_none(row["vendor_responded_at"]),
                    outbound_dispatched_at=_none(row["outbound_dispatched_at"]),
                    vendor_received_at=_none(row["vendor_received_at"]),
                    return_dispatched_at=_none(row["return_dispatched_at"]),
                    returned_at=_none(row["returned_at"]),
                    outcome=_none(row["outcome"]),
                    closed_at=_none(row["closed_at"]),
                )
    except CaseError as error:
        raise CsvImportError(f"row {_number}: {error}") from None
    return ImportSummary("cases", len(rows), dry_run)


def _read_rows(path: Path, fields: tuple[str, ...]) -> list[tuple[int, dict[str, str]]]:
    if not path.is_file() or path.stat().st_size > _MAX_BYTES:
        raise CsvImportError("CSV file is missing or exceeds the 100 MiB limit")
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if (
            reader.fieldnames is None
            or len(reader.fieldnames) != len(set(reader.fieldnames))
            or set(reader.fieldnames) != set(fields)
        ):
            raise CsvImportError("CSV header must contain exactly the canonical columns")
        rows = [(number, row) for number, row in enumerate(reader, start=2) if any(row.values())]
    if len(rows) > _MAX_ROWS or any(None in row.values() for _, row in rows):
        raise CsvImportError("CSV contains too many rows or malformed fields")
    return [(number, {field: row[field].strip() for field in fields}) for number, row in rows]


def _none(value: str) -> str | None:
    return value or None


def _parse_active(value: str, row: int) -> bool:
    if value.casefold() == "true":
        return True
    if value.casefold() == "false":
        return False
    raise CsvImportError(f"row {row}, active: must be true or false")


@contextmanager
def _import_transaction(connection: sqlite3.Connection, *, dry_run: bool) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        if dry_run:
            connection.rollback()
        else:
            connection.commit()
