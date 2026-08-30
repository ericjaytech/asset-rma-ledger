from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

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


def test_add_asset_stores_normalised_identifiers_and_warranty_details(connection) -> None:
    from asset_rma_ledger.assets import add_asset

    add_vendor(connection, key="northstar", name="Northstar Repairs")

    asset = add_asset(
        connection,
        tag=" LAP-0042 ",
        serial=" SN-A1B2C3 ",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
        warranty_vendor="northstar",
        warranty_reference="NW-2026-44",
        warranty_start="2026-08-01",
        warranty_end="2027-07-31",
    )

    assert asset.tag == "LAP-0042"
    assert asset.serial == "SN-A1B2C3"
    assert asset.lifecycle_status == "in_stock"
    assert asset.warranty_vendor_key == "northstar"
    assert asset.warranty_state(date(2026, 8, 30)) == "active"


def test_asset_identifiers_are_independently_case_insensitively_unique(connection) -> None:
    from asset_rma_ledger.assets import AssetConflictError, add_asset

    add_asset(
        connection,
        tag="LAP-0042",
        serial="SN-A1B2C3",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
    )

    with pytest.raises(AssetConflictError, match="asset tag already exists"):
        add_asset(
            connection,
            tag="lap-0042",
            serial="SN-D4E5F6",
            asset_type="laptop",
            manufacturer="ExampleCo",
            model="ProBook-14",
        )
    with pytest.raises(AssetConflictError, match="serial number already exists"):
        add_asset(
            connection,
            tag="LAP-0043",
            serial="sn-a1b2c3",
            asset_type="laptop",
            manufacturer="ExampleCo",
            model="ProBook-14",
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"tag": "bad tag"}, "asset tag"),
        ({"serial": ""}, "serial number is required"),
        ({"warranty_start": "30-08-2026"}, "ISO calendar date"),
        (
            {"warranty_start": "2027-01-01", "warranty_end": "2026-12-31"},
            "must not precede",
        ),
        ({"warranty_vendor": "not a key"}, "warranty vendor"),
        ({"lifecycle_status": "in_rma"}, "cannot be selected"),
    ],
)
def test_add_asset_rejects_invalid_identifiers_dates_and_status(
    connection, kwargs: dict[str, str], message: str
) -> None:
    from asset_rma_ledger.assets import AssetValidationError, add_asset

    values = {
        "tag": "LAP-0042",
        "serial": "SN-A1B2C3",
        "asset_type": "laptop",
        "manufacturer": "ExampleCo",
        "model": "ProBook-14",
    }
    values.update(kwargs)

    with pytest.raises(AssetValidationError, match=message):
        add_asset(connection, **values)


def test_add_asset_rejects_a_missing_warranty_vendor_without_creating_an_asset(connection) -> None:
    from asset_rma_ledger.assets import AssetValidationError, add_asset, list_assets

    with pytest.raises(AssetValidationError, match="warranty vendor does not exist"):
        add_asset(
            connection,
            tag="LAP-0042",
            serial="SN-A1B2C3",
            asset_type="laptop",
            manufacturer="ExampleCo",
            model="ProBook-14",
            warranty_vendor="northstar",
        )

    assert list_assets(connection) == ()


def test_asset_warranty_state_handles_not_recorded_future_active_and_expired_terms(
    connection,
) -> None:
    from asset_rma_ledger.assets import add_asset

    not_recorded = add_asset(
        connection,
        tag="LAP-0042",
        serial="SN-A1B2C3",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
    )
    future = add_asset(
        connection,
        tag="LAP-0043",
        serial="SN-D4E5F6",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
        warranty_start="2026-09-01",
    )
    expired = add_asset(
        connection,
        tag="LAP-0044",
        serial="SN-G7H8I9",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
        warranty_end="2026-08-29",
    )

    assert not_recorded.warranty_state(date(2026, 8, 30)) == "not_recorded"
    assert future.warranty_state(date(2026, 8, 30)) == "not_started"
    assert expired.warranty_state(date(2026, 8, 30)) == "expired"


def test_edit_identify_and_retire_preserve_asset_invariants(connection) -> None:
    from asset_rma_ledger.assets import (
        AssetValidationError,
        add_asset,
        edit_asset,
        get_asset,
        identify_asset,
        retire_asset,
    )

    add_asset(
        connection,
        tag="LAP-0042",
        serial="SN-A1B2C3",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
    )

    edited = edit_asset(connection, "lap-0042", model="ProBook-15")
    identified = identify_asset(connection, "LAP-0042", serial="SN-D4E5F6")
    retired = retire_asset(connection, "LAP-0042")

    assert edited.model == "ProBook-15"
    assert identified.serial == "SN-D4E5F6"
    assert retired.lifecycle_status == "retired"
    assert get_asset(connection, "LAP-0042") == retired
    with pytest.raises(AssetValidationError, match="retired asset"):
        edit_asset(connection, "LAP-0042", model="ProBook-16")


def test_retire_asset_rejects_an_asset_with_an_active_rma_case(connection) -> None:
    from asset_rma_ledger.assets import AssetValidationError, add_asset, retire_asset

    vendor = add_vendor(connection, key="northstar", name="Northstar Repairs")
    asset = add_asset(
        connection,
        tag="LAP-0042",
        serial="SN-A1B2C3",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
    )
    connection.execute(
        """
        INSERT INTO rma_cases (
            case_reference, case_reference_folded, asset_id, vendor_id, opened_at,
            current_status, last_event_sequence, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "RMA-2026-001",
            "rma-2026-001",
            asset.id,
            vendor.id,
            "2026-08-30T09:00:00Z",
            "open",
            0,
            "2026-08-30T09:00:00Z",
            "2026-08-30T09:00:00Z",
        ),
    )
    connection.commit()

    with pytest.raises(AssetValidationError, match="active RMA case"):
        retire_asset(connection, asset.tag)
