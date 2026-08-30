"""Command-line interface for Asset RMA Ledger."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .database import DatabaseError, connect_database, initialise_database
from .models import Vendor
from .vendors import (
    VendorError,
    VendorValidationError,
    add_vendor,
    edit_vendor,
    get_vendor,
    list_vendors,
    set_vendor_active,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the command parser without performing I/O."""
    parser = argparse.ArgumentParser(
        prog="asset-rma-ledger",
        description="Local asset-return and vendor RMA case tracking.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="Path to the local SQLite ledger database.",
    )
    parser.add_argument("--version", action="version", version=__version__)

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create a new empty ledger database.")

    vendor_parser = commands.add_parser("vendor", help="Manage vendor records.")
    vendor_commands = vendor_parser.add_subparsers(dest="vendor_command", required=True)

    add_vendor_parser = vendor_commands.add_parser("add", help="Register a vendor.")
    add_vendor_parser.add_argument("--key", required=True, help="Stable vendor key.")
    add_vendor_parser.add_argument("--name", required=True, help="Vendor organisation name.")
    _add_vendor_detail_arguments(add_vendor_parser)

    edit_vendor_parser = vendor_commands.add_parser("edit", help="Update a vendor.")
    edit_vendor_parser.add_argument("key", help="Existing vendor key.")
    edit_vendor_parser.add_argument("--name", help="Replacement vendor organisation name.")
    _add_vendor_detail_arguments(edit_vendor_parser)

    vendor_commands.add_parser("list", help="List active and inactive vendors.")
    show_vendor_parser = vendor_commands.add_parser("show", help="Show a vendor.")
    show_vendor_parser.add_argument("key", help="Existing vendor key.")
    activate_vendor_parser = vendor_commands.add_parser("activate", help="Activate a vendor.")
    activate_vendor_parser.add_argument("key", help="Existing vendor key.")
    deactivate_vendor_parser = vendor_commands.add_parser(
        "deactivate", help="Deactivate a vendor without deleting it."
    )
    deactivate_vendor_parser.add_argument("key", help="Existing vendor key.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface and return a process exit code."""
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.database is None:
        parser.error("--database is required for this command")

    if arguments.command == "init":
        try:
            initialise_database(arguments.database)
        except DatabaseError as error:
            print(f"error: {error}", file=sys.stderr)
            return 4

        print(f"Initialised ledger database: {arguments.database}")
        return 0

    if arguments.command == "vendor":
        return _run_vendor_command(arguments)

    parser.error(f"unsupported command: {arguments.command}")
    return 2


def _add_vendor_detail_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--support-url", help="HTTPS support portal URL.")
    parser.add_argument("--support-email", help="Vendor support email address.")
    parser.add_argument("--support-phone", help="Vendor support phone number.")
    parser.add_argument("--account-reference", help="Local vendor account or contract reference.")
    parser.add_argument("--response-sla-hours", help="Default elapsed response SLA in hours.")
    parser.add_argument("--resolution-sla-hours", help="Default elapsed resolution SLA in hours.")


def _run_vendor_command(arguments: argparse.Namespace) -> int:
    try:
        connection = connect_database(arguments.database)
    except DatabaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 4

    try:
        if arguments.vendor_command == "add":
            vendor = add_vendor(
                connection,
                key=arguments.key,
                name=arguments.name,
                support_url=arguments.support_url,
                support_email=arguments.support_email,
                support_phone=arguments.support_phone,
                account_reference=arguments.account_reference,
                response_sla_hours=arguments.response_sla_hours,
                resolution_sla_hours=arguments.resolution_sla_hours,
            )
            print(f"Added vendor: {vendor.key}")
            return 0
        if arguments.vendor_command == "edit":
            vendor = edit_vendor(
                connection,
                arguments.key,
                name=arguments.name,
                support_url=arguments.support_url,
                support_email=arguments.support_email,
                support_phone=arguments.support_phone,
                account_reference=arguments.account_reference,
                response_sla_hours=arguments.response_sla_hours,
                resolution_sla_hours=arguments.resolution_sla_hours,
            )
            print(f"Updated vendor: {vendor.key}")
            return 0
        if arguments.vendor_command == "activate":
            vendor = set_vendor_active(connection, arguments.key, active=True)
            print(f"Activated vendor: {vendor.key}")
            return 0
        if arguments.vendor_command == "deactivate":
            vendor = set_vendor_active(connection, arguments.key, active=False)
            print(f"Deactivated vendor: {vendor.key}")
            return 0
        if arguments.vendor_command == "list":
            _print_vendor_list(list_vendors(connection))
            return 0
        if arguments.vendor_command == "show":
            _print_vendor(get_vendor(connection, arguments.key))
            return 0
    except VendorValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except VendorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    finally:
        connection.close()

    raise AssertionError(f"unsupported vendor command: {arguments.vendor_command}")


def _print_vendor_list(vendors: tuple[Vendor, ...]) -> None:
    print("KEY\tSTATUS\tRESPONSE SLA\tRESOLUTION SLA\tNAME")
    for vendor in vendors:
        print(
            "\t".join(
                (
                    vendor.key,
                    "active" if vendor.active else "inactive",
                    _format_minutes(vendor.response_sla_minutes),
                    _format_minutes(vendor.resolution_sla_minutes),
                    vendor.name,
                )
            )
        )


def _print_vendor(vendor: Vendor) -> None:
    fields = (
        ("Key", vendor.key),
        ("Name", vendor.name),
        ("Status", "active" if vendor.active else "inactive"),
        ("Support URL", vendor.support_url or "-"),
        ("Support email", vendor.support_email or "-"),
        ("Support phone", vendor.support_phone or "-"),
        ("Account reference", vendor.account_reference or "-"),
        ("Response SLA", _format_minutes(vendor.response_sla_minutes)),
        ("Resolution SLA", _format_minutes(vendor.resolution_sla_minutes)),
    )
    for label, value in fields:
        print(f"{label}: {value}")


def _format_minutes(minutes: int | None) -> str:
    if minutes is None:
        return "-"
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours}h {remainder}m"
    if hours:
        return f"{hours}h"
    return f"{remainder}m"
