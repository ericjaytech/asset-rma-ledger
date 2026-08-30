from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from asset_rma_ledger.database import connect_database, initialise_database
from asset_rma_ledger.vendors import (
    VendorConflictError,
    VendorInactiveError,
    VendorValidationError,
    add_vendor,
    edit_vendor,
    get_vendor,
    list_vendors,
    require_active_vendor,
    set_vendor_active,
)


@pytest.fixture
def connection(tmp_path: Path):
    database_path = tmp_path / "team-assets.db"
    initialise_database(database_path)
    connection = connect_database(database_path)
    try:
        yield connection
    finally:
        connection.close()


def test_add_vendor_stores_a_normalised_key_and_sla_minutes(connection) -> None:
    vendor = add_vendor(
        connection,
        key=" NorthStar ",
        name=" Northstar Repairs ",
        support_url="https://support.example.test/rma",
        response_sla_hours=Decimal("0.5"),
        resolution_sla_hours=Decimal("120"),
    )

    assert vendor.key == "NorthStar"
    assert vendor.name == "Northstar Repairs"
    assert vendor.response_sla_minutes == 30
    assert vendor.resolution_sla_minutes == 7200
    assert vendor.active is True
    assert get_vendor(connection, "northstar") == vendor


def test_vendor_keys_are_case_insensitively_unique(connection) -> None:
    add_vendor(connection, key="NorthStar", name="Northstar Repairs")

    with pytest.raises(VendorConflictError, match="already exists"):
        add_vendor(connection, key="northstar", name="Duplicate")

    assert [vendor.key for vendor in list_vendors(connection)] == ["NorthStar"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("key", "not a key", "vendor key"),
        ("support_url", "http://support.example.test", "HTTPS"),
        ("support_url", "https://[bad", "HTTPS"),
        ("support_email", "not-an-email", "email"),
        ("response_sla_hours", Decimal("0"), "positive"),
        ("resolution_sla_hours", Decimal("0.001"), "whole minute"),
    ],
)
def test_add_vendor_rejects_invalid_input(
    connection, field: str, value: str | Decimal, message: str
) -> None:
    values: dict[str, str | Decimal] = {
        "key": "northstar",
        "name": "Northstar Repairs",
    }
    values[field] = value

    with pytest.raises(VendorValidationError, match=message):
        add_vendor(connection, **values)


def test_inactive_vendors_remain_visible_but_cannot_be_selected_for_a_new_case(connection) -> None:
    add_vendor(connection, key="northstar", name="Northstar Repairs")

    inactive_vendor = set_vendor_active(connection, "northstar", active=False)

    assert inactive_vendor.active is False
    assert list_vendors(connection) == (inactive_vendor,)
    with pytest.raises(VendorInactiveError, match="inactive"):
        require_active_vendor(connection, "northstar")


def test_edit_vendor_updates_only_supplied_fields(connection) -> None:
    add_vendor(
        connection,
        key="northstar",
        name="Northstar Repairs",
        support_phone="+44 20 0000 0000",
        response_sla_hours=Decimal("8"),
    )

    vendor = edit_vendor(
        connection,
        "northstar",
        name="Northstar Hardware Services",
        resolution_sla_hours=Decimal("72"),
    )

    assert vendor.name == "Northstar Hardware Services"
    assert vendor.support_phone == "+44 20 0000 0000"
    assert vendor.response_sla_minutes == 480
    assert vendor.resolution_sla_minutes == 4320


def test_vendor_list_is_ordered_by_casefolded_key(connection) -> None:
    add_vendor(connection, key="zebra", name="Zebra Repairs")
    add_vendor(connection, key="alpha", name="Alpha Repairs")

    assert [vendor.key for vendor in list_vendors(connection)] == ["alpha", "zebra"]
