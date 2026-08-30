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
        response_due_at="2026-08-30T17:00:00Z",
        resolution_due_at="2026-09-04T09:00:00Z",
    )


def _temporarily_disable_event_updates(connection) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'trigger' AND name = 'case_events_no_update'"
    ).fetchone()
    connection.execute("DROP TRIGGER case_events_no_update")
    return row["sql"]


def test_verify_accepts_a_valid_ledger_without_modifying_it(connection) -> None:
    from asset_rma_ledger.cases import (
        add_case_note,
        authorise_case,
        change_case_deadlines,
        close_case,
        correct_case_outcome,
        dispatch_case,
        dispatch_return,
        list_case_events,
        receive_return,
        record_case_outcome,
        record_vendor_receipt,
        record_vendor_response,
    )
    from asset_rma_ledger.verify import verify_database

    _open_case(connection)
    record_vendor_response(
        connection,
        "RMA-2026-001",
        at="2026-08-30T09:15:00Z",
        operator_alias="ej",
        vendor_reference="NS-88421",
    )
    change_case_deadlines(
        connection,
        "RMA-2026-001",
        at="2026-08-30T09:30:00Z",
        operator_alias="ej",
        reason="Vendor confirmed revised deadline",
        resolution_due_at="2026-09-05T09:00:00Z",
    )
    add_case_note(
        connection,
        "RMA-2026-001",
        at="2026-08-30T09:45:00Z",
        operator_alias="ej",
        note="Prepared asset for dispatch",
    )
    authorise_case(connection, "RMA-2026-001", at="2026-08-30T10:00:00Z", operator_alias="ej")
    dispatch_case(
        connection,
        "RMA-2026-001",
        at="2026-08-31T10:00:00Z",
        operator_alias="ej",
        carrier="Example Carrier",
        tracking="TRACK-001",
    )
    record_vendor_receipt(
        connection, "RMA-2026-001", at="2026-09-01T09:00:00Z", operator_alias="ej"
    )
    dispatch_return(
        connection,
        "RMA-2026-001",
        at="2026-09-03T09:00:00Z",
        operator_alias="ej",
        carrier="Example Carrier",
        tracking="TRACK-002",
    )
    receive_return(connection, "RMA-2026-001", at="2026-09-04T08:00:00Z", operator_alias="ej")
    record_case_outcome(
        connection,
        "RMA-2026-001",
        at="2026-09-04T08:30:00Z",
        operator_alias="ej",
        outcome="repaired",
    )
    outcome_event = list_case_events(connection, "RMA-2026-001")[-1]
    correct_case_outcome(
        connection,
        "RMA-2026-001",
        at="2026-09-04T08:45:00Z",
        operator_alias="ej",
        original_event_id=outcome_event.event_id,
        outcome="replaced",
        reason="Vendor supplied a replacement",
    )
    close_case(connection, "RMA-2026-001", at="2026-09-04T09:00:00Z", operator_alias="ej")
    changes_before = connection.total_changes

    summary = verify_database(connection)

    assert summary.cases == 1
    assert summary.events == 12
    assert summary.checks == 6
    assert connection.total_changes == changes_before
    assert connection.execute("PRAGMA query_only").fetchone()[0] == 0


def test_verify_detects_a_rehashed_invalid_transition(connection) -> None:
    from asset_rma_ledger.cases import authorise_case, list_case_events
    from asset_rma_ledger.events import calculate_event_hash
    from asset_rma_ledger.verify import VerificationError, verify_database

    _open_case(connection)
    authorise_case(connection, "RMA-2026-001", at="2026-08-30T10:00:00Z", operator_alias="ej")
    event = list_case_events(connection, "RMA-2026-001")[-1]
    trigger_sql = _temporarily_disable_event_updates(connection)
    replacement_hash = calculate_event_hash(
        case_reference=event.case_reference,
        sequence=event.sequence,
        event_id=event.event_id,
        event_type="return_received",
        occurred_at=event.occurred_at,
        recorded_at=event.recorded_at,
        operator_alias=event.operator_alias,
        payload_json=event.payload_json,
        previous_hash=event.previous_hash,
    )
    connection.execute(
        "UPDATE case_events SET event_type = ?, event_hash = ? WHERE event_id = ?",
        ("return_received", replacement_hash, event.event_id),
    )
    connection.execute(
        "UPDATE rma_cases SET last_event_hash = ? WHERE case_reference = ?",
        (replacement_hash, "RMA-2026-001"),
    )
    connection.execute(trigger_sql)

    with pytest.raises(VerificationError, match="lifecycle.*RMA-2026-001"):
        verify_database(connection)


def test_verify_detects_event_hash_tampering(connection) -> None:
    from asset_rma_ledger.verify import VerificationError, verify_database

    _open_case(connection)
    trigger_sql = _temporarily_disable_event_updates(connection)
    connection.execute(
        "UPDATE case_events SET payload_json = ? WHERE sequence = 1",
        ('{"asset_tag":"ALTERED"}',),
    )
    connection.execute(trigger_sql)

    with pytest.raises(VerificationError, match="event chain.*RMA-2026-001"):
        verify_database(connection)


def test_verify_rejects_a_rehashed_event_with_an_unexpected_payload_field(connection) -> None:
    from asset_rma_ledger.cases import list_case_events
    from asset_rma_ledger.events import calculate_event_hash, canonical_json
    from asset_rma_ledger.verify import VerificationError, verify_database

    _open_case(connection)
    event = list_case_events(connection, "RMA-2026-001")[0]
    payload_json = canonical_json({**event.payload, "unexpected": True})
    replacement_hash = calculate_event_hash(
        case_reference=event.case_reference,
        sequence=event.sequence,
        event_id=event.event_id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        recorded_at=event.recorded_at,
        operator_alias=event.operator_alias,
        payload_json=payload_json,
        previous_hash=event.previous_hash,
    )
    trigger_sql = _temporarily_disable_event_updates(connection)
    connection.execute(
        "UPDATE case_events SET payload_json = ?, event_hash = ? WHERE event_id = ?",
        (payload_json, replacement_hash, event.event_id),
    )
    connection.execute(
        "UPDATE rma_cases SET last_event_hash = ? WHERE case_reference = ?",
        (replacement_hash, "RMA-2026-001"),
    )
    connection.execute(trigger_sql)

    with pytest.raises(VerificationError, match="lifecycle.*RMA-2026-001"):
        verify_database(connection)


def test_verify_accepts_exceptional_closure_and_cancellation(connection) -> None:
    from asset_rma_ledger.cases import cancel_case, close_case, open_case, record_case_outcome
    from asset_rma_ledger.verify import verify_database

    _open_case(connection)
    record_case_outcome(
        connection,
        "RMA-2026-001",
        at="2026-08-30T10:00:00Z",
        operator_alias="ej",
        outcome="refund",
    )
    close_case(
        connection,
        "RMA-2026-001",
        at="2026-08-30T11:00:00Z",
        operator_alias="ej",
        asset_status="retired",
    )
    add_asset(
        connection,
        tag="LAP-0043",
        serial="SN-D4E5F6",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
    )
    open_case(
        connection,
        reference="RMA-2026-002",
        asset_tag="LAP-0043",
        vendor_key="northstar",
        opened_at="2026-08-30T12:00:00Z",
        operator_alias="ej",
    )
    cancel_case(
        connection,
        "RMA-2026-002",
        at="2026-08-30T13:00:00Z",
        operator_alias="ej",
        reason="Return no longer required",
    )

    summary = verify_database(connection)

    assert summary.cases == 2
    assert summary.events == 5


def test_verify_accepts_source_marked_reconstructed_history(connection) -> None:
    from asset_rma_ledger.cases import import_case_snapshot
    from asset_rma_ledger.verify import verify_database

    add_vendor(connection, key="northstar", name="Northstar Repairs")
    add_asset(
        connection,
        tag="LAP-0042",
        serial="SN-A1B2C3",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
    )
    import_case_snapshot(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        status="returned",
        operator_alias="migration",
        outbound_dispatched_at="2026-08-31T09:00:00Z",
        vendor_received_at="2026-09-01T09:00:00Z",
        return_dispatched_at="2026-09-03T09:00:00Z",
        returned_at="2026-09-04T09:00:00Z",
        outcome="repaired",
    )

    summary = verify_database(connection)

    assert summary.cases == 1
    assert summary.events == 7


def test_verify_detects_missing_append_only_trigger(connection) -> None:
    from asset_rma_ledger.verify import VerificationError, verify_database

    connection.execute("DROP TRIGGER case_events_no_delete")

    with pytest.raises(VerificationError, match="schema.*trigger"):
        verify_database(connection)


def test_verify_detects_case_projection_drift(connection) -> None:
    from asset_rma_ledger.verify import VerificationError, verify_database

    _open_case(connection)
    connection.execute(
        "UPDATE rma_cases SET current_status = 'authorised' WHERE case_reference = ?",
        ("RMA-2026-001",),
    )
    changes_before = connection.total_changes

    with pytest.raises(VerificationError, match="projection.*RMA-2026-001.*current_status"):
        verify_database(connection)

    assert connection.total_changes == changes_before
    assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
