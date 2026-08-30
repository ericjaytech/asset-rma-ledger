from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from asset_rma_ledger.assets import add_asset, get_asset
from asset_rma_ledger.database import connect_database, initialise_database
from asset_rma_ledger.vendors import add_vendor


@pytest.fixture
def connection(tmp_path: Path):
    database_path = tmp_path / "team-assets.db"
    initialise_database(database_path)
    connection = connect_database(database_path)
    try:
        yield connection
    finally:
        connection.close()


def test_vendor_import_is_transactional_and_supports_dry_run(connection, tmp_path: Path) -> None:
    from asset_rma_ledger.csvio import import_vendors_csv
    from asset_rma_ledger.vendors import list_vendors

    source = tmp_path / "vendors.csv"
    source.write_text(
        "vendor_key,name,support_url,support_email,support_phone,account_reference,response_sla_hours,resolution_sla_hours,active\n"
        "northstar,Northstar Repairs,,,,,8,120,true\n",
        encoding="utf-8",
    )

    summary = import_vendors_csv(connection, source, dry_run=True)
    assert summary.rows == 1
    assert summary.dry_run is True
    assert list_vendors(connection) == ()

    summary = import_vendors_csv(connection, source)
    assert summary.rows == 1
    assert list_vendors(connection)[0].key == "northstar"


def test_asset_import_rolls_back_when_any_row_is_invalid(connection, tmp_path: Path) -> None:
    from asset_rma_ledger.assets import list_assets
    from asset_rma_ledger.csvio import CsvImportError, import_assets_csv

    source = tmp_path / "assets.csv"
    source.write_text(
        "asset_tag,serial_number,asset_type,manufacturer,model,lifecycle_status,warranty_vendor,warranty_reference,warranty_start,warranty_end\n"
        "LAP-0042,SN-A1B2C3,laptop,ExampleCo,ProBook-14,in_stock,,,,\n"
        "LAP-0043,SN-A1B2C3,laptop,ExampleCo,ProBook-14,in_stock,,,,\n",
        encoding="utf-8",
    )

    with pytest.raises(CsvImportError, match="row 3"):
        import_assets_csv(connection, source)

    assert list_assets(connection) == ()


def test_case_snapshot_import_reconstructs_source_marked_history(
    connection, tmp_path: Path
) -> None:
    from asset_rma_ledger.cases import get_case, list_case_events
    from asset_rma_ledger.csvio import import_cases_csv

    add_vendor(connection, key="northstar", name="Northstar Repairs")
    add_asset(
        connection,
        tag="LAP-0042",
        serial="SN-A1B2C3",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
    )
    source = tmp_path / "cases.csv"
    source.write_text(
        "case_reference,asset_tag,vendor_key,opened_at,status,vendor_reference,response_due_at,resolution_due_at,vendor_responded_at,outbound_dispatched_at,vendor_received_at,return_dispatched_at,returned_at,outcome,closed_at\n"
        "RMA-2026-001,LAP-0042,northstar,2026-08-30T09:00:00Z,returned,NS-88421,2026-08-30T17:00:00Z,2026-09-04T09:00:00Z,2026-08-30T09:30:00Z,2026-08-31T10:00:00Z,2026-09-01T09:00:00Z,2026-09-03T12:00:00Z,2026-09-04T09:00:00Z,repaired,\n",
        encoding="utf-8",
    )

    summary = import_cases_csv(connection, source, operator_alias="ej")

    case = get_case(connection, "RMA-2026-001")
    events = list_case_events(connection, case.reference)
    assert summary.rows == 1
    assert case.current_status == "returned"
    assert case.current_outcome == "repaired"
    assert get_asset(connection, "LAP-0042").lifecycle_status == "in_stock"
    assert [event.event_type for event in events] == [
        "case_opened",
        "vendor_response_recorded",
        "status_changed",
        "outbound_dispatched",
        "vendor_receipt_recorded",
        "return_dispatched",
        "return_received",
        "outcome_recorded",
    ]
    assert all(event.operator_alias == "ej" for event in events)
    assert all(event.payload["source"] == "csv_import" for event in events)
    outbound = next(event for event in events if event.event_type == "outbound_dispatched")
    returned = next(event for event in events if event.event_type == "return_dispatched")
    assert outbound.payload["carrier"] is None
    assert outbound.payload["tracking"] is None
    assert returned.payload["carrier"] is None
    assert returned.payload["tracking"] is None


def test_case_snapshot_dry_run_rolls_back_cases_events_and_asset_status(
    connection, tmp_path: Path
) -> None:
    from asset_rma_ledger.csvio import import_cases_csv

    add_vendor(connection, key="northstar", name="Northstar Repairs")
    add_asset(
        connection,
        tag="LAP-0042",
        serial="SN-A1B2C3",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
    )
    source = tmp_path / "cases.csv"
    source.write_text(
        "case_reference,asset_tag,vendor_key,opened_at,status,vendor_reference,response_due_at,resolution_due_at,vendor_responded_at,outbound_dispatched_at,vendor_received_at,return_dispatched_at,returned_at,outcome,closed_at\n"
        "RMA-2026-001,LAP-0042,northstar,2026-08-30T09:00:00Z,outbound,,,,,2026-08-31T10:00:00Z,,,,,\n",
        encoding="utf-8",
    )

    summary = import_cases_csv(connection, source, operator_alias="ej", dry_run=True)

    assert summary.dry_run is True
    assert connection.execute("SELECT COUNT(*) FROM rma_cases").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM case_events").fetchone()[0] == 0
    assert get_asset(connection, "LAP-0042").lifecycle_status == "in_stock"


def test_case_snapshot_import_rolls_back_all_rows_on_invalid_chronology(
    connection, tmp_path: Path
) -> None:
    from asset_rma_ledger.csvio import CsvImportError, import_cases_csv

    add_vendor(connection, key="northstar", name="Northstar Repairs")
    for number in (42, 43):
        add_asset(
            connection,
            tag=f"LAP-00{number}",
            serial=f"SN-A1B2C{number}",
            asset_type="laptop",
            manufacturer="ExampleCo",
            model="ProBook-14",
        )
    source = tmp_path / "cases.csv"
    source.write_text(
        "case_reference,asset_tag,vendor_key,opened_at,status,vendor_reference,response_due_at,resolution_due_at,vendor_responded_at,outbound_dispatched_at,vendor_received_at,return_dispatched_at,returned_at,outcome,closed_at\n"
        "RMA-2026-001,LAP-0042,northstar,2026-08-30T09:00:00Z,open,,,,,,,,,,,\n"
        "RMA-2026-002,LAP-0043,northstar,2026-08-30T09:00:00Z,with_vendor,,,,,2026-09-02T09:00:00Z,2026-09-01T09:00:00Z,,,,\n",
        encoding="utf-8",
    )

    with pytest.raises(CsvImportError, match="row 3"):
        import_cases_csv(connection, source, operator_alias="ej")

    assert connection.execute("SELECT COUNT(*) FROM rma_cases").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM case_events").fetchone()[0] == 0


def test_export_bundle_writes_sorted_tables_and_verified_manifest(
    connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from asset_rma_ledger.cases import add_case_note, open_case
    from asset_rma_ledger.csvio import export_csv_bundle

    monkeypatch.setattr("asset_rma_ledger.csvio._utc_now", lambda: "2026-08-30T18:00:00Z")
    add_vendor(connection, key="zeta", name="Zeta Repairs")
    add_vendor(connection, key="alpha", name='=HYPERLINK("https://invalid.test")')
    add_asset(
        connection,
        tag="LAP-0042",
        serial="SN-A1B2C3",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
        warranty_vendor="alpha",
    )
    open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="alpha",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
        response_due_at="2026-08-30T17:00:00Z",
        resolution_due_at="2026-09-01T09:00:00Z",
    )
    add_case_note(
        connection,
        "RMA-2026-001",
        at="2026-08-30T10:00:00Z",
        operator_alias="ej",
        note="=not a spreadsheet instruction",
    )
    output = tmp_path / "ledger-export"

    summary = export_csv_bundle(connection, output, as_of="2026-08-30T18:00:00Z")

    expected_files = {
        "assets.csv",
        "case_history.csv",
        "cases.csv",
        "due_cases.csv",
        "export_manifest.json",
        "vendors.csv",
    }
    assert summary.output_dir == output
    assert {path.name for path in output.iterdir()} == expected_files
    with (output / "vendors.csv").open(encoding="utf-8", newline="") as source:
        vendors = list(csv.DictReader(source))
    assert [row["vendor_key"] for row in vendors] == ["alpha", "zeta"]
    assert vendors[0]["name"].startswith("'=")
    with (output / "case_history.csv").open(encoding="utf-8", newline="") as source:
        history = list(csv.DictReader(source))
    assert [row["event_type"] for row in history] == ["case_opened", "note_added"]
    assert json.loads(history[1]["payload_json"])["note"].startswith("=")
    with (output / "due_cases.csv").open(encoding="utf-8", newline="") as source:
        due = list(csv.DictReader(source))
    assert [(row["deadline_type"], row["state"]) for row in due] == [
        ("response", "overdue"),
        ("resolution", "due_soon"),
    ]

    manifest = json.loads((output / "export_manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["tool_version"] == "0.1.0a0"
    assert manifest["generated_at"] == "2026-08-30T18:00:00Z"
    assert manifest["database"] == "team-assets.db"
    assert manifest["files"]["case_history.csv"]["rows"] == 2
    for filename, details in manifest["files"].items():
        assert details["sha256"] == hashlib.sha256((output / filename).read_bytes()).hexdigest()


def test_export_bundle_refuses_an_existing_destination(connection, tmp_path: Path) -> None:
    from asset_rma_ledger.csvio import CsvExportError, export_csv_bundle

    output = tmp_path / "ledger-export"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(CsvExportError, match="already exists"):
        export_csv_bundle(connection, output, as_of="2026-08-30T18:00:00Z")

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_export_bundle_removes_staging_data_after_a_write_failure(
    connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asset_rma_ledger.csvio as csvio

    output = tmp_path / "ledger-export"

    def fail_write(*_args, **_kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(csvio, "_write_csv", fail_write)

    with pytest.raises(csvio.CsvExportError, match="could not publish"):
        csvio.export_csv_bundle(connection, output, as_of="2026-08-30T18:00:00Z")

    assert not output.exists()
    assert list(tmp_path.iterdir()) == [tmp_path / "team-assets.db"]
