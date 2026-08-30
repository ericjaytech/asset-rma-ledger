from __future__ import annotations

from pathlib import Path

import pytest

from asset_rma_ledger.assets import add_asset, retire_asset
from asset_rma_ledger.database import connect_database, initialise_database
from asset_rma_ledger.vendors import add_vendor, set_vendor_active


@pytest.fixture
def connection(tmp_path: Path):
    database_path = tmp_path / "team-assets.db"
    initialise_database(database_path)
    connection = connect_database(database_path)
    try:
        yield connection
    finally:
        connection.close()


def _register_vendor_and_asset(connection):
    vendor = add_vendor(
        connection,
        key="northstar",
        name="Northstar Repairs",
        response_sla_hours="8",
        resolution_sla_hours="120",
    )
    asset = add_asset(
        connection,
        tag="LAP-0042",
        serial="SN-A1B2C3",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
    )
    return vendor, asset


def test_open_case_snapshots_vendor_slas_and_appends_the_opening_event(connection) -> None:
    from asset_rma_ledger.cases import get_case, list_case_events, open_case

    _register_vendor_and_asset(connection)

    case = open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
    )
    event = list_case_events(connection, "RMA-2026-001")[0]

    assert case.reference == "RMA-2026-001"
    assert case.current_status == "open"
    assert case.response_due_at == "2026-08-30T17:00:00Z"
    assert case.resolution_due_at == "2026-09-04T09:00:00Z"
    assert case.last_event_sequence == 1
    assert case.last_event_hash == event.event_hash
    assert get_case(connection, "rma-2026-001") == case
    assert event.sequence == 1
    assert event.event_type == "case_opened"
    assert event.previous_hash == "0" * 64
    assert event.payload == {
        "asset_tag": "LAP-0042",
        "resolution_due_at": "2026-09-04T09:00:00Z",
        "response_due_at": "2026-08-30T17:00:00Z",
        "vendor_key": "northstar",
    }


def test_open_case_keeps_explicit_deadlines_instead_of_vendor_defaults(connection) -> None:
    from asset_rma_ledger.cases import open_case

    _register_vendor_and_asset(connection)

    case = open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
        response_due_at="2026-08-30T12:00:00Z",
        resolution_due_at="2026-09-01T09:00:00Z",
    )

    assert case.response_due_at == "2026-08-30T12:00:00Z"
    assert case.resolution_due_at == "2026-09-01T09:00:00Z"


def test_open_case_rejects_a_second_active_case_without_writing_an_event(connection) -> None:
    from asset_rma_ledger.cases import CaseConflictError, list_case_events, open_case

    _register_vendor_and_asset(connection)
    open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
    )

    with pytest.raises(CaseConflictError, match="active RMA case"):
        open_case(
            connection,
            reference="RMA-2026-002",
            asset_tag="LAP-0042",
            vendor_key="northstar",
            opened_at="2026-08-30T10:00:00Z",
            operator_alias="ej",
        )

    assert len(list_case_events(connection, "RMA-2026-001")) == 1
    assert connection.execute("SELECT COUNT(*) FROM rma_cases").fetchone()[0] == 1


def test_open_case_rejects_an_inactive_vendor_and_retired_asset(connection) -> None:
    from asset_rma_ledger.cases import CaseStateError, open_case

    vendor, asset = _register_vendor_and_asset(connection)
    set_vendor_active(connection, vendor.key, active=False)

    with pytest.raises(CaseStateError, match="inactive"):
        open_case(
            connection,
            reference="RMA-2026-001",
            asset_tag=asset.tag,
            vendor_key=vendor.key,
            opened_at="2026-08-30T09:00:00Z",
            operator_alias="ej",
        )

    set_vendor_active(connection, vendor.key, active=True)
    retire_asset(connection, asset.tag)
    with pytest.raises(CaseStateError, match="retired"):
        open_case(
            connection,
            reference="RMA-2026-001",
            asset_tag=asset.tag,
            vendor_key=vendor.key,
            opened_at="2026-08-30T09:00:00Z",
            operator_alias="ej",
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"reference": "RMA 2026"}, "case reference"),
        ({"opened_at": "2026-08-30 09:00:00"}, "UTC RFC 3339"),
        ({"opened_at": "2026-08-30 09:00:00Z"}, "UTC RFC 3339"),
        ({"operator_alias": "not an alias"}, "operator alias"),
    ],
)
def test_open_case_rejects_invalid_case_fields(
    connection, kwargs: dict[str, str], message: str
) -> None:
    from asset_rma_ledger.cases import CaseValidationError, open_case

    _, asset = _register_vendor_and_asset(connection)
    values = {
        "reference": "RMA-2026-001",
        "asset_tag": asset.tag,
        "vendor_key": "northstar",
        "opened_at": "2026-08-30T09:00:00Z",
        "operator_alias": "ej",
    }
    values.update(kwargs)

    with pytest.raises(CaseValidationError, match=message):
        open_case(connection, **values)
