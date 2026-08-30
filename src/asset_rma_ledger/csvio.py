"""Bounded CSV import and deterministic local export."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import __version__
from .assets import AssetError, add_asset
from .cases import CaseError, import_case_snapshot
from .database import SCHEMA_VERSION
from .deadlines import DeadlineError, due_cases
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
_HISTORY_FIELDS = (
    "case_reference",
    "sequence",
    "event_id",
    "event_type",
    "occurred_at",
    "recorded_at",
    "operator_alias",
    "payload_json",
    "previous_hash",
    "event_hash",
)
_DUE_FIELDS = (
    "case_reference",
    "asset_tag",
    "vendor_key",
    "deadline_type",
    "deadline_at",
    "state",
    "as_of",
)


class CsvImportError(RuntimeError):
    """Raised when a CSV file cannot be imported safely."""


class CsvExportError(RuntimeError):
    """Raised when an export bundle cannot be published safely."""


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """The result of one successful import or dry run."""

    kind: str
    rows: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class ExportSummary:
    """The result of one atomically published export."""

    output_dir: Path
    row_counts: dict[str, int]


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


def export_csv_bundle(
    connection: sqlite3.Connection,
    output_dir: Path,
    *,
    as_of: str | None = None,
) -> ExportSummary:
    """Publish a fixed-schema CSV snapshot and checksum manifest atomically."""
    output = Path(output_dir)
    if output.exists():
        raise CsvExportError(f"export destination already exists: {output}")
    if not output.parent.is_dir():
        raise CsvExportError(f"export parent directory does not exist: {output.parent}")

    generated_at = _utc_now()
    effective_as_of = as_of or generated_at
    staging: Path | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        os.chmod(staging, 0o700)
        with _read_transaction(connection):
            tables = _export_tables(connection, as_of=effective_as_of)
            database_name = _database_basename(connection)

        row_counts: dict[str, int] = {}
        manifest_files: dict[str, dict[str, str | int]] = {}
        for filename, fields, rows in tables:
            path = staging / filename
            _write_csv(path, fields, rows)
            row_counts[filename] = len(rows)
            manifest_files[filename] = {
                "rows": len(rows),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        _write_manifest(
            staging / "export_manifest.json",
            {
                "database": database_name,
                "files": manifest_files,
                "generated_at": generated_at,
                "schema_version": SCHEMA_VERSION,
                "tool_version": __version__,
            },
        )
        if output.exists():
            raise CsvExportError(f"export destination already exists: {output}")
        staging.rename(output)
        staging = None
        return ExportSummary(output, row_counts)
    except CsvExportError:
        raise
    except (DeadlineError, OSError, sqlite3.Error) as error:
        raise CsvExportError("could not publish CSV export") from error
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def _export_tables(
    connection: sqlite3.Connection, *, as_of: str
) -> tuple[tuple[str, tuple[str, ...], list[dict[str, Any]]], ...]:
    vendors = [
        {
            "vendor_key": row["vendor_key"],
            "name": row["name"],
            "support_url": row["support_url"],
            "support_email": row["support_email"],
            "support_phone": row["support_phone"],
            "account_reference": row["account_reference"],
            "response_sla_hours": _minutes_to_hours(row["response_sla_minutes"]),
            "resolution_sla_hours": _minutes_to_hours(row["resolution_sla_minutes"]),
            "active": "true" if row["active"] else "false",
        }
        for row in connection.execute(
            """
            SELECT vendor_key, name, support_url, support_email, support_phone,
                   account_reference, response_sla_minutes, resolution_sla_minutes, active
            FROM vendors ORDER BY vendor_key_folded ASC
            """
        ).fetchall()
    ]
    assets = [
        {
            "asset_tag": row["asset_tag"],
            "serial_number": row["serial_number"],
            "asset_type": row["asset_type"],
            "manufacturer": row["manufacturer"],
            "model": row["model"],
            "lifecycle_status": row["lifecycle_status"],
            "warranty_vendor": row["warranty_vendor"],
            "warranty_reference": row["warranty_reference"],
            "warranty_start": row["warranty_start"],
            "warranty_end": row["warranty_end"],
        }
        for row in connection.execute(
            """
            SELECT assets.asset_tag, assets.serial_number, assets.asset_type,
                   assets.manufacturer, assets.model, assets.lifecycle_status,
                   vendors.vendor_key AS warranty_vendor, assets.warranty_reference,
                   assets.warranty_start, assets.warranty_end
            FROM assets
            LEFT JOIN vendors ON vendors.id = assets.warranty_vendor_id
            ORDER BY assets.asset_tag_folded ASC
            """
        ).fetchall()
    ]
    cases = [
        {
            "case_reference": row["case_reference"],
            "asset_tag": row["asset_tag"],
            "vendor_key": row["vendor_key"],
            "opened_at": row["opened_at"],
            "status": row["current_status"],
            "vendor_reference": row["vendor_reference"],
            "response_due_at": row["response_due_at"],
            "resolution_due_at": row["resolution_due_at"],
            "vendor_responded_at": row["vendor_responded_at"],
            "outbound_dispatched_at": row["outbound_dispatched_at"],
            "vendor_received_at": row["vendor_received_at"],
            "return_dispatched_at": row["return_dispatched_at"],
            "returned_at": row["returned_at"],
            "outcome": row["current_outcome"],
            "closed_at": row["closed_at"],
        }
        for row in connection.execute(
            """
            SELECT rma_cases.*, assets.asset_tag, vendors.vendor_key
            FROM rma_cases
            JOIN assets ON assets.id = rma_cases.asset_id
            JOIN vendors ON vendors.id = rma_cases.vendor_id
            ORDER BY rma_cases.case_reference_folded ASC
            """
        ).fetchall()
    ]
    history = [
        {field: row[field] for field in _HISTORY_FIELDS}
        for row in connection.execute(
            """
            SELECT rma_cases.case_reference, case_events.sequence, case_events.event_id,
                   case_events.event_type, case_events.occurred_at, case_events.recorded_at,
                   case_events.operator_alias, case_events.payload_json,
                   case_events.previous_hash, case_events.event_hash
            FROM case_events
            JOIN rma_cases ON rma_cases.id = case_events.case_id
            ORDER BY rma_cases.case_reference_folded ASC, case_events.sequence ASC
            """
        ).fetchall()
    ]
    due = [
        {
            "case_reference": item.reference,
            "asset_tag": item.asset_tag,
            "vendor_key": item.vendor_key,
            "deadline_type": item.deadline_type,
            "deadline_at": item.deadline_at,
            "state": item.state,
            "as_of": as_of,
        }
        for item in due_cases(connection, as_of=as_of, within_hours=48)
    ]
    return (
        ("vendors.csv", _VENDOR_FIELDS, vendors),
        ("assets.csv", _ASSET_FIELDS, assets),
        ("cases.csv", _CASE_FIELDS, cases),
        ("case_history.csv", _HISTORY_FIELDS, history),
        ("due_cases.csv", _DUE_FIELDS, due),
    )


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _spreadsheet_safe(row[field]) for field in fields})
    os.chmod(path, 0o600)


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as target:
        json.dump(manifest, target, ensure_ascii=False, indent=2, sort_keys=True)
        target.write("\n")
    os.chmod(path, 0o600)


def _spreadsheet_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def _minutes_to_hours(value: int | None) -> str:
    if value is None:
        return ""
    hours = format(Decimal(value) / Decimal(60), "f")
    return hours.rstrip("0").rstrip(".") if "." in hours else hours


def _database_basename(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA database_list").fetchone()
    path = row["file"]
    return Path(path).name if path else ":memory:"


@contextmanager
def _read_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        yield
        return
    connection.execute("BEGIN")
    try:
        yield
    finally:
        connection.rollback()


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
