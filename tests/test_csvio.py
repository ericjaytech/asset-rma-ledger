from __future__ import annotations

from pathlib import Path

import pytest

from asset_rma_ledger.database import connect_database, initialise_database


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
