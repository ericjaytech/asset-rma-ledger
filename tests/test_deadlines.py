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


def _register_vendor_and_assets(connection) -> None:
    add_vendor(
        connection,
        key="northstar",
        name="Northstar Repairs",
        response_sla_hours="8",
        resolution_sla_hours="120",
    )
    for number in (42, 43):
        add_asset(
            connection,
            tag=f"LAP-00{number}",
            serial=f"SN-A1B2C{number}",
            asset_type="laptop",
            manufacturer="ExampleCo",
            model="ProBook-14",
        )


@pytest.mark.parametrize(
    ("deadline_at", "completed_at", "as_of", "within_hours", "expected"),
    [
        (None, None, "2026-08-30T10:00:00Z", 48, "not_set"),
        (
            "2026-08-30T10:00:00Z",
            "2026-08-30T10:00:00Z",
            "2026-08-30T12:00:00Z",
            48,
            "met",
        ),
        (
            "2026-08-30T10:00:00Z",
            "2026-08-30T10:00:01Z",
            "2026-08-30T12:00:00Z",
            48,
            "breached",
        ),
        ("2026-08-30T09:59:59Z", None, "2026-08-30T10:00:00Z", 48, "overdue"),
        ("2026-08-30T10:00:00Z", None, "2026-08-30T10:00:00Z", 48, "due_soon"),
        ("2026-09-02T10:00:00Z", None, "2026-08-30T10:00:00Z", 48, "on_track"),
    ],
)
def test_classify_deadline_handles_every_documented_boundary(
    deadline_at: str | None,
    completed_at: str | None,
    as_of: str,
    within_hours: int,
    expected: str,
) -> None:
    from asset_rma_ledger.deadlines import classify_deadline

    assert (
        classify_deadline(
            deadline_at=deadline_at,
            completed_at=completed_at,
            as_of=as_of,
            within_hours=within_hours,
        )
        == expected
    )


def test_due_cases_orders_overdue_before_upcoming_deadlines(connection) -> None:
    from asset_rma_ledger.cases import open_case
    from asset_rma_ledger.deadlines import due_cases

    _register_vendor_and_assets(connection)
    open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T08:00:00Z",
        operator_alias="ej",
        response_due_at="2026-08-30T09:00:00Z",
    )
    open_case(
        connection,
        reference="RMA-2026-002",
        asset_tag="LAP-0043",
        vendor_key="northstar",
        opened_at="2026-08-30T08:00:00Z",
        operator_alias="ej",
        response_due_at="2026-08-30T11:00:00Z",
        resolution_due_at="2026-08-30T12:00:00Z",
    )

    due = due_cases(connection, as_of="2026-08-30T10:00:00Z", within_hours=2)

    assert [(item.reference, item.deadline_type, item.state) for item in due] == [
        ("RMA-2026-001", "response", "overdue"),
        ("RMA-2026-002", "response", "due_soon"),
        ("RMA-2026-002", "resolution", "due_soon"),
    ]


def test_deadline_change_appends_old_and_new_values_with_a_reason(connection) -> None:
    from asset_rma_ledger.cases import change_case_deadlines, list_case_events, open_case

    _register_vendor_and_assets(connection)
    opened = open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
    )

    changed = change_case_deadlines(
        connection,
        "RMA-2026-001",
        at="2026-08-30T10:00:00Z",
        operator_alias="ej",
        reason="Vendor confirmed a shorter response target.",
        response_due_at="2026-08-30T12:00:00Z",
    )
    event = list_case_events(connection, "RMA-2026-001")[-1]

    assert changed.response_due_at == "2026-08-30T12:00:00Z"
    assert changed.resolution_due_at == opened.resolution_due_at
    assert event.event_type == "deadline_changed"
    assert event.payload == {
        "previous_resolution_due_at": "2026-09-04T09:00:00Z",
        "previous_response_due_at": "2026-08-30T17:00:00Z",
        "reason": "Vendor confirmed a shorter response target.",
        "resolution_due_at": "2026-09-04T09:00:00Z",
        "response_due_at": "2026-08-30T12:00:00Z",
    }


def test_deadline_change_requires_a_value_and_reason_without_appending_an_event(connection) -> None:
    from asset_rma_ledger.cases import (
        CaseValidationError,
        change_case_deadlines,
        list_case_events,
        open_case,
    )

    _register_vendor_and_assets(connection)
    open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
    )

    with pytest.raises(CaseValidationError, match="deadline value"):
        change_case_deadlines(
            connection,
            "RMA-2026-001",
            at="2026-08-30T10:00:00Z",
            operator_alias="ej",
            reason="Correcting data.",
        )
    with pytest.raises(CaseValidationError, match="reason is required"):
        change_case_deadlines(
            connection,
            "RMA-2026-001",
            at="2026-08-30T10:00:00Z",
            operator_alias="ej",
            response_due_at="2026-08-30T12:00:00Z",
            reason="",
        )

    assert len(list_case_events(connection, "RMA-2026-001")) == 1


def test_due_cases_excludes_cancelled_cases(connection) -> None:
    from asset_rma_ledger.cases import cancel_case, open_case
    from asset_rma_ledger.deadlines import due_cases

    _register_vendor_and_assets(connection)
    open_case(
        connection,
        reference="RMA-2026-001",
        asset_tag="LAP-0042",
        vendor_key="northstar",
        opened_at="2026-08-30T09:00:00Z",
        operator_alias="ej",
        response_due_at="2026-08-30T10:00:00Z",
    )
    cancel_case(
        connection,
        "RMA-2026-001",
        at="2026-08-30T09:30:00Z",
        operator_alias="ej",
        reason="Vendor confirmed that a return is not required.",
    )

    assert due_cases(connection, as_of="2026-08-30T11:00:00Z", within_hours=48) == ()
