from __future__ import annotations

from pathlib import Path

import pytest

from asset_rma_ledger.assets import add_asset
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


def _open_case(connection):
    from asset_rma_ledger.cases import open_case

    add_vendor(connection, key="northstar", name="Northstar Repairs")
    add_asset(
        connection,
        tag="LAP-0042",
        serial="SN-A1B2C3",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
    )
    return open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
    )


def test_opening_event_uses_canonical_json_and_a_recomputable_hash(connection) -> None:
    from asset_rma_ledger.cases import list_case_events
    from asset_rma_ledger.events import calculate_event_hash, canonical_json

    _open_case(connection)
    event = list_case_events(connection, "RMA-2026-001")[0]

    assert canonical_json({"vendor_key": "northstar", "asset_tag": "LAP-0042"}) == (
        '{"asset_tag":"LAP-0042","vendor_key":"northstar"}'
    )
    assert event.event_hash == calculate_event_hash(
        case_reference="RMA-2026-001",
        sequence=event.sequence,
        event_id=event.event_id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        recorded_at=event.recorded_at,
        operator_alias=event.operator_alias,
        payload_json=event.payload_json,
        previous_hash=event.previous_hash,
    )


def test_opening_event_cannot_be_updated_or_deleted(connection) -> None:
    from asset_rma_ledger.cases import list_case_events

    _open_case(connection)
    event = list_case_events(connection, "RMA-2026-001")[0]

    with pytest.raises(Exception, match="append-only"):
        connection.execute(
            "UPDATE case_events SET event_type = 'note_added' WHERE event_id = ?", (event.event_id,)
        )
    with pytest.raises(Exception, match="append-only"):
        connection.execute("DELETE FROM case_events WHERE event_id = ?", (event.event_id,))
