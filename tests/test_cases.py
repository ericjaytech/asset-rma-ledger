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


def test_lifecycle_milestones_append_events_and_update_case_and_asset_projections(
    connection,
) -> None:
    from asset_rma_ledger.assets import get_asset
    from asset_rma_ledger.cases import (
        authorise_case,
        dispatch_case,
        dispatch_return,
        get_case,
        list_case_events,
        open_case,
        receive_return,
        record_vendor_receipt,
        record_vendor_response,
    )

    _register_vendor_and_asset(connection)
    open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
    )

    responded = record_vendor_response(
        connection,
        "RMA-2026-001",
        at="2026-08-30T09:30:00Z",
        operator_alias="ej",
        vendor_reference="NS-88421",
    )
    authorised = authorise_case(
        connection, "RMA-2026-001", at="2026-08-30T10:00:00Z", operator_alias="ej"
    )
    outbound = dispatch_case(
        connection,
        "RMA-2026-001",
        at="2026-08-31T10:00:00Z",
        operator_alias="ej",
        carrier="Example Carrier",
        tracking="TRACK-OUT-001",
    )
    assert get_asset(connection, "LAP-0042").lifecycle_status == "in_rma"

    with_vendor = record_vendor_receipt(
        connection, "RMA-2026-001", at="2026-09-01T09:00:00Z", operator_alias="ej"
    )
    returning = dispatch_return(
        connection,
        "RMA-2026-001",
        at="2026-09-03T12:00:00Z",
        operator_alias="ej",
        carrier="Example Carrier",
        tracking="TRACK-RETURN-001",
    )
    returned = receive_return(
        connection, "RMA-2026-001", at="2026-09-04T09:00:00Z", operator_alias="ej"
    )
    events = list_case_events(connection, "RMA-2026-001")

    assert responded.vendor_responded_at == "2026-08-30T09:30:00Z"
    assert responded.vendor_reference == "NS-88421"
    assert authorised.current_status == "authorised"
    assert outbound.current_status == "outbound"
    assert outbound.outbound_dispatched_at == "2026-08-31T10:00:00Z"
    assert with_vendor.current_status == "with_vendor"
    assert returning.current_status == "returning"
    assert returned.current_status == "returned"
    assert get_case(connection, "RMA-2026-001") == returned
    assert get_asset(connection, "LAP-0042").lifecycle_status == "in_stock"
    assert [event.event_type for event in events] == [
        "case_opened",
        "vendor_response_recorded",
        "status_changed",
        "outbound_dispatched",
        "vendor_receipt_recorded",
        "return_dispatched",
        "return_received",
    ]
    assert [event.sequence for event in events] == list(range(1, 8))
    assert all(
        event.previous_hash == events[index - 1].event_hash
        for index, event in enumerate(events[1:], 1)
    )


def test_milestones_reject_invalid_transitions_and_empty_shipping_data_without_mutation(
    connection,
) -> None:
    from asset_rma_ledger.cases import (
        CaseStateError,
        CaseValidationError,
        authorise_case,
        dispatch_case,
        list_case_events,
        open_case,
    )

    _register_vendor_and_asset(connection)
    open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
    )

    with pytest.raises(CaseStateError, match="requires authorised"):
        dispatch_case(
            connection,
            "RMA-2026-001",
            at="2026-08-30T10:00:00Z",
            operator_alias="ej",
            carrier="Example Carrier",
            tracking="TRACK-OUT-001",
        )

    authorise_case(connection, "RMA-2026-001", at="2026-08-30T10:00:00Z", operator_alias="ej")
    with pytest.raises(CaseValidationError, match="carrier is required"):
        dispatch_case(
            connection,
            "RMA-2026-001",
            at="2026-08-31T10:00:00Z",
            operator_alias="ej",
            carrier="",
            tracking="TRACK-OUT-001",
        )

    assert [event.event_type for event in list_case_events(connection, "RMA-2026-001")] == [
        "case_opened",
        "status_changed",
    ]


def test_outcome_notes_and_outcome_correction_append_auditable_events(connection) -> None:
    from asset_rma_ledger.cases import (
        add_case_note,
        correct_case_outcome,
        list_case_events,
        open_case,
        record_case_outcome,
    )

    _register_vendor_and_asset(connection)
    open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
    )

    outcome = record_case_outcome(
        connection,
        "RMA-2026-001",
        at="2026-08-30T10:00:00Z",
        operator_alias="ej",
        outcome="refund",
    )
    noted = add_case_note(
        connection,
        "RMA-2026-001",
        at="2026-08-30T10:30:00Z",
        operator_alias="ej",
        note="Vendor confirmed that the device will not be returned.",
    )
    original_event = list_case_events(connection, "RMA-2026-001")[1]
    corrected = correct_case_outcome(
        connection,
        "RMA-2026-001",
        at="2026-08-30T11:00:00Z",
        operator_alias="ej",
        original_event_id=original_event.event_id,
        outcome="written_off",
        reason="Vendor corrected the proposed financial remedy.",
    )
    events = list_case_events(connection, "RMA-2026-001")

    assert outcome.current_outcome == "refund"
    assert noted.current_status == "open"
    assert corrected.current_outcome == "written_off"
    assert [event.event_type for event in events] == [
        "case_opened",
        "outcome_recorded",
        "note_added",
        "correction_recorded",
    ]
    assert events[2].payload == {"note": "Vendor confirmed that the device will not be returned."}
    assert events[3].payload == {
        "field": "outcome",
        "original_event_id": original_event.event_id,
        "previous_value": "refund",
        "reason": "Vendor corrected the proposed financial remedy.",
        "replacement_value": "written_off",
    }


def test_outcome_operations_reject_invalid_or_ambiguous_mutations_without_history_changes(
    connection,
) -> None:
    from asset_rma_ledger.cases import (
        CaseStateError,
        CaseValidationError,
        correct_case_outcome,
        list_case_events,
        open_case,
        record_case_outcome,
    )

    _register_vendor_and_asset(connection)
    open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
    )

    with pytest.raises(CaseValidationError, match="explanatory note"):
        record_case_outcome(
            connection,
            "RMA-2026-001",
            at="2026-08-30T10:00:00Z",
            operator_alias="ej",
            outcome="other",
        )

    recorded = record_case_outcome(
        connection,
        "RMA-2026-001",
        at="2026-08-30T10:00:00Z",
        operator_alias="ej",
        outcome="refund",
    )
    with pytest.raises(CaseStateError, match="already been recorded"):
        record_case_outcome(
            connection,
            "RMA-2026-001",
            at="2026-08-30T11:00:00Z",
            operator_alias="ej",
            outcome="written_off",
        )
    with pytest.raises(CaseValidationError, match="outcome-recorded event"):
        correct_case_outcome(
            connection,
            "RMA-2026-001",
            at="2026-08-30T11:00:00Z",
            operator_alias="ej",
            original_event_id=list_case_events(connection, "RMA-2026-001")[0].event_id,
            outcome="written_off",
            reason="Correction after case review.",
        )

    assert recorded.current_outcome == "refund"
    assert [event.event_type for event in list_case_events(connection, "RMA-2026-001")] == [
        "case_opened",
        "outcome_recorded",
    ]


def test_close_case_completes_the_returned_workflow_after_an_outcome(connection) -> None:
    from asset_rma_ledger.assets import get_asset
    from asset_rma_ledger.cases import (
        authorise_case,
        close_case,
        dispatch_case,
        dispatch_return,
        list_case_events,
        open_case,
        receive_return,
        record_case_outcome,
        record_vendor_receipt,
    )

    _register_vendor_and_asset(connection)
    open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
    )
    authorise_case(connection, "RMA-2026-001", at="2026-08-30T10:00:00Z", operator_alias="ej")
    dispatch_case(
        connection,
        "RMA-2026-001",
        at="2026-08-31T10:00:00Z",
        operator_alias="ej",
        carrier="Example Carrier",
        tracking="TRACK-OUT-001",
    )
    record_vendor_receipt(
        connection, "RMA-2026-001", at="2026-09-01T09:00:00Z", operator_alias="ej"
    )
    dispatch_return(
        connection,
        "RMA-2026-001",
        at="2026-09-03T12:00:00Z",
        operator_alias="ej",
        carrier="Example Carrier",
        tracking="TRACK-RETURN-001",
    )
    receive_return(connection, "RMA-2026-001", at="2026-09-04T09:00:00Z", operator_alias="ej")
    record_case_outcome(
        connection,
        "RMA-2026-001",
        at="2026-09-04T10:00:00Z",
        operator_alias="ej",
        outcome="repaired",
    )

    closed = close_case(connection, "RMA-2026-001", at="2026-09-04T11:00:00Z", operator_alias="ej")
    event = list_case_events(connection, "RMA-2026-001")[-1]

    assert closed.current_status == "closed"
    assert closed.closed_at == "2026-09-04T11:00:00Z"
    assert get_asset(connection, "LAP-0042").lifecycle_status == "in_stock"
    assert event.event_type == "case_closed"
    assert event.payload == {
        "asset_status": "in_stock",
        "closure_type": "returned",
        "outcome": "repaired",
    }


def test_exceptional_close_and_pre_dispatch_cancellation_preserve_auditable_history(
    connection,
) -> None:
    from asset_rma_ledger.assets import get_asset
    from asset_rma_ledger.cases import (
        cancel_case,
        close_case,
        list_case_events,
        open_case,
        record_case_outcome,
    )

    _register_vendor_and_asset(connection)
    open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
    )
    record_case_outcome(
        connection,
        "RMA-2026-001",
        at="2026-08-30T10:00:00Z",
        operator_alias="ej",
        outcome="refund",
    )

    closed = close_case(
        connection,
        "RMA-2026-001",
        at="2026-08-30T11:00:00Z",
        operator_alias="ej",
        asset_status="retired",
    )
    event = list_case_events(connection, "RMA-2026-001")[-1]

    assert closed.current_status == "closed"
    assert get_asset(connection, "LAP-0042").lifecycle_status == "retired"
    assert event.payload == {
        "asset_status": "retired",
        "closure_type": "exceptional",
        "outcome": "refund",
    }

    add_asset(
        connection,
        tag="LAP-0043",
        serial="SN-A1B2C4",
        asset_type="laptop",
        manufacturer="ExampleCo",
        model="ProBook-14",
    )
    open_case(
        connection,
        reference="RMA-2026-002",
        asset_tag="LAP-0043",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
    )
    cancelled = cancel_case(
        connection,
        "RMA-2026-002",
        at="2026-08-30T10:00:00Z",
        operator_alias="ej",
        reason="Vendor confirmed that a return is not required.",
    )

    assert cancelled.current_status == "cancelled"
    assert list_case_events(connection, "RMA-2026-002")[-1].payload == {
        "reason": "Vendor confirmed that a return is not required."
    }
    assert get_asset(connection, "LAP-0043").lifecycle_status == "in_stock"


def test_terminal_operations_reject_missing_outcomes_and_invalid_paths_without_mutation(
    connection,
) -> None:
    from asset_rma_ledger.cases import (
        CaseStateError,
        CaseValidationError,
        authorise_case,
        cancel_case,
        close_case,
        dispatch_case,
        list_case_events,
        open_case,
        record_case_outcome,
    )

    _register_vendor_and_asset(connection)
    open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
    )

    with pytest.raises(CaseStateError, match="an outcome must be recorded"):
        close_case(connection, "RMA-2026-001", at="2026-08-30T10:00:00Z", operator_alias="ej")

    record_case_outcome(
        connection,
        "RMA-2026-001",
        at="2026-08-30T10:00:00Z",
        operator_alias="ej",
        outcome="refund",
    )
    with pytest.raises(CaseValidationError, match="asset status is required"):
        close_case(connection, "RMA-2026-001", at="2026-08-30T11:00:00Z", operator_alias="ej")

    authorise_case(connection, "RMA-2026-001", at="2026-08-30T11:00:00Z", operator_alias="ej")
    dispatch_case(
        connection,
        "RMA-2026-001",
        at="2026-08-31T10:00:00Z",
        operator_alias="ej",
        carrier="Example Carrier",
        tracking="TRACK-OUT-001",
    )
    with pytest.raises(CaseStateError, match="requires authorised or open status"):
        cancel_case(
            connection,
            "RMA-2026-001",
            at="2026-08-31T11:00:00Z",
            operator_alias="ej",
            reason="Not permitted after dispatch.",
        )

    assert [event.event_type for event in list_case_events(connection, "RMA-2026-001")] == [
        "case_opened",
        "outcome_recorded",
        "status_changed",
        "outbound_dispatched",
    ]
