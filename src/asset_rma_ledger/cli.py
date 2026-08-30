"""Command-line interface for Asset RMA Ledger."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from . import __version__
from .assets import (
    AssetError,
    AssetValidationError,
    add_asset,
    edit_asset,
    get_asset,
    identify_asset,
    list_assets,
    retire_asset,
)
from .cases import (
    CaseError,
    CaseValidationError,
    add_case_note,
    authorise_case,
    cancel_case,
    change_case_deadlines,
    close_case,
    correct_case_outcome,
    dispatch_case,
    dispatch_return,
    get_case,
    list_case_events,
    open_case,
    receive_return,
    record_case_outcome,
    record_vendor_receipt,
    record_vendor_response,
)
from .csvio import (
    CsvExportError,
    CsvImportError,
    export_csv_bundle,
    import_assets_csv,
    import_cases_csv,
    import_vendors_csv,
)
from .database import DatabaseError, connect_database, initialise_database
from .deadlines import DeadlineError, DeadlineValidationError, DueCase, due_cases
from .models import Asset, CaseEvent, RmaCase, Vendor
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

    import_parser = commands.add_parser("import", help="Import new canonical CSV records.")
    import_commands = import_parser.add_subparsers(dest="import_command", required=True)
    for name, help_text in (
        ("vendors", "Import vendor records."),
        ("assets", "Import asset records."),
        ("cases", "Import reconstructed case snapshots."),
    ):
        command = import_commands.add_parser(name, help=help_text)
        command.add_argument("path", type=Path, help="Canonical CSV input file.")
        command.add_argument("--dry-run", action="store_true", help="Validate then roll back.")
        command.add_argument("--by", required=True, help="Import operator alias.")

    export_parser = commands.add_parser("export", help="Publish a local CSV ledger bundle.")
    export_parser.add_argument(
        "--output-dir", type=Path, required=True, help="New destination directory."
    )
    export_parser.add_argument(
        "--as-of", help="UTC reference time for the exported 48-hour due view."
    )

    due_parser = commands.add_parser(
        "due", help="List overdue and upcoming incomplete SLA deadlines."
    )
    due_parser.add_argument(
        "--within", required=True, help="Positive elapsed window in whole hours, such as 48h."
    )
    due_parser.add_argument(
        "--as-of", help="UTC RFC 3339 reference timestamp; defaults to the current time."
    )

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

    asset_parser = commands.add_parser("asset", help="Manage asset records.")
    asset_commands = asset_parser.add_subparsers(dest="asset_command", required=True)

    add_asset_parser = asset_commands.add_parser("add", help="Register an asset.")
    add_asset_parser.add_argument("--tag", required=True, help="Stable local asset tag.")
    add_asset_parser.add_argument("--serial", required=True, help="Device serial number.")
    add_asset_parser.add_argument(
        "--type", dest="asset_type", required=True, help="Asset type label."
    )
    add_asset_parser.add_argument("--manufacturer", required=True, help="Asset manufacturer.")
    add_asset_parser.add_argument("--model", required=True, help="Asset model name or number.")
    add_asset_parser.add_argument(
        "--status", default="in_stock", help="Initial status: in_stock, deployed or retired."
    )
    _add_asset_warranty_arguments(add_asset_parser)

    edit_asset_parser = asset_commands.add_parser("edit", help="Update an asset's details.")
    edit_asset_parser.add_argument("tag", help="Existing asset tag.")
    edit_asset_parser.add_argument(
        "--type", dest="asset_type", help="Replacement asset type label."
    )
    edit_asset_parser.add_argument("--manufacturer", help="Replacement manufacturer.")
    edit_asset_parser.add_argument("--model", help="Replacement model name or number.")
    _add_asset_warranty_arguments(edit_asset_parser)

    identify_asset_parser = asset_commands.add_parser(
        "identify", help="Correct an asset tag or serial number."
    )
    identify_asset_parser.add_argument("tag", help="Existing asset tag.")
    identify_asset_parser.add_argument("--new-tag", help="Replacement asset tag.")
    identify_asset_parser.add_argument("--serial", help="Replacement serial number.")

    retire_asset_parser = asset_commands.add_parser(
        "retire", help="Retire an asset that has no active RMA case."
    )
    retire_asset_parser.add_argument("tag", help="Existing asset tag.")
    asset_commands.add_parser("list", help="List asset records.")
    show_asset_parser = asset_commands.add_parser(
        "show", help="Show an asset and its warranty state."
    )
    show_asset_parser.add_argument("tag", help="Existing asset tag.")
    show_asset_parser.add_argument("--as-of", help="Warranty state date in YYYY-MM-DD format.")

    case_parser = commands.add_parser("case", help="Manage RMA cases and their history.")
    case_commands = case_parser.add_subparsers(dest="case_command", required=True)
    open_case_parser = case_commands.add_parser("open", help="Open an RMA case.")
    open_case_parser.add_argument(
        "--case", dest="case_reference", required=True, help="Case reference."
    )
    open_case_parser.add_argument(
        "--asset", dest="asset_tag", required=True, help="Existing asset tag."
    )
    open_case_parser.add_argument(
        "--vendor", dest="vendor_key", required=True, help="Active vendor key."
    )
    open_case_parser.add_argument(
        "--opened-at", required=True, help="UTC RFC 3339 opening timestamp."
    )
    open_case_parser.add_argument(
        "--by", dest="operator_alias", required=True, help="Operator alias."
    )
    open_case_parser.add_argument("--response-due-at", help="Explicit UTC response deadline.")
    open_case_parser.add_argument("--resolution-due-at", help="Explicit UTC resolution deadline.")

    show_case_parser = case_commands.add_parser("show", help="Show an RMA case.")
    show_case_parser.add_argument("reference", help="Existing case reference.")
    show_case_parser.add_argument(
        "--history", action="store_true", help="Include immutable event history."
    )

    vendor_response_parser = case_commands.add_parser(
        "vendor-response", help="Record the first response from the vendor."
    )
    _add_case_milestone_arguments(vendor_response_parser)
    vendor_response_parser.add_argument("--vendor-reference", help="Vendor case reference.")

    authorise_case_parser = case_commands.add_parser(
        "authorise", help="Record vendor authorisation for the return."
    )
    _add_case_milestone_arguments(authorise_case_parser)

    dispatch_case_parser = case_commands.add_parser(
        "dispatch", help="Record outbound dispatch to the vendor."
    )
    _add_case_milestone_arguments(dispatch_case_parser, shipping=True)

    vendor_received_parser = case_commands.add_parser(
        "vendor-received", help="Record vendor receipt of the asset."
    )
    _add_case_milestone_arguments(vendor_received_parser)

    return_dispatch_parser = case_commands.add_parser(
        "return-dispatch", help="Record dispatch from the vendor."
    )
    _add_case_milestone_arguments(return_dispatch_parser, shipping=True)

    return_received_parser = case_commands.add_parser(
        "return-received", help="Record return receipt by the IT team."
    )
    _add_case_milestone_arguments(return_received_parser)

    deadline_case_parser = case_commands.add_parser(
        "deadline", help="Change one or both SLA deadlines with an auditable reason."
    )
    _add_case_milestone_arguments(deadline_case_parser)
    deadline_case_parser.add_argument("--reason", required=True, help="Reason for the change.")
    deadline_case_parser.add_argument(
        "--response-due-at", help="Replacement UTC response deadline."
    )
    deadline_case_parser.add_argument(
        "--resolution-due-at", help="Replacement UTC resolution deadline."
    )

    outcome_case_parser = case_commands.add_parser(
        "outcome", help="Record the effective outcome before case closure."
    )
    _add_case_milestone_arguments(outcome_case_parser)
    outcome_case_parser.add_argument("--outcome", required=True, help="Supported outcome label.")
    outcome_case_parser.add_argument("--note", help="Optional explanatory or replacement note.")

    note_case_parser = case_commands.add_parser(
        "note", help="Append a bounded operational note to an active case."
    )
    _add_case_milestone_arguments(note_case_parser)
    note_case_parser.add_argument(
        "--note", required=True, help="Plain-text note, up to 2,000 characters."
    )

    correct_outcome_case_parser = case_commands.add_parser(
        "correct-outcome", help="Correct a recorded outcome through an immutable event."
    )
    _add_case_milestone_arguments(correct_outcome_case_parser)
    correct_outcome_case_parser.add_argument(
        "--original-event-id",
        required=True,
        help="Outcome event UUID shown by case show --history.",
    )
    correct_outcome_case_parser.add_argument(
        "--outcome", required=True, help="Replacement supported outcome label."
    )
    correct_outcome_case_parser.add_argument(
        "--reason", required=True, help="Reason for the correction."
    )
    correct_outcome_case_parser.add_argument(
        "--note", help="Optional explanatory or replacement note."
    )

    close_case_parser = case_commands.add_parser(
        "close", help="Close a returned or documented exceptional case."
    )
    _add_case_milestone_arguments(close_case_parser)
    close_case_parser.add_argument(
        "--asset-status", help="Required final status for exceptional closure: in_stock or retired."
    )

    cancel_case_parser = case_commands.add_parser(
        "cancel", help="Cancel a case before asset dispatch."
    )
    _add_case_milestone_arguments(cancel_case_parser)
    cancel_case_parser.add_argument("--reason", required=True, help="Reason for cancellation.")
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

    if arguments.command == "asset":
        return _run_asset_command(arguments)

    if arguments.command == "import":
        return _run_import_command(arguments)

    if arguments.command == "export":
        return _run_export_command(arguments)

    if arguments.command == "due":
        return _run_due_command(arguments)

    if arguments.command == "case":
        return _run_case_command(arguments)

    parser.error(f"unsupported command: {arguments.command}")
    return 2


def _add_vendor_detail_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--support-url", help="HTTPS support portal URL.")
    parser.add_argument("--support-email", help="Vendor support email address.")
    parser.add_argument("--support-phone", help="Vendor support phone number.")
    parser.add_argument("--account-reference", help="Local vendor account or contract reference.")
    parser.add_argument("--response-sla-hours", help="Default elapsed response SLA in hours.")
    parser.add_argument("--resolution-sla-hours", help="Default elapsed resolution SLA in hours.")


def _add_asset_warranty_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--warranty-vendor", help="Existing vendor key for warranty support.")
    parser.add_argument("--warranty-reference", help="Warranty or contract reference.")
    parser.add_argument("--warranty-start", help="Warranty start date in YYYY-MM-DD format.")
    parser.add_argument("--warranty-end", help="Warranty end date in YYYY-MM-DD format.")


def _add_case_milestone_arguments(
    parser: argparse.ArgumentParser, *, shipping: bool = False
) -> None:
    parser.add_argument("reference", help="Existing case reference.")
    parser.add_argument("--at", required=True, help="UTC RFC 3339 milestone timestamp.")
    parser.add_argument("--by", dest="operator_alias", required=True, help="Operator alias.")
    if shipping:
        parser.add_argument("--carrier", required=True, help="Shipping carrier.")
        parser.add_argument("--tracking", required=True, help="Carrier tracking reference.")


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


def _run_asset_command(arguments: argparse.Namespace) -> int:
    try:
        connection = connect_database(arguments.database)
    except DatabaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 4

    try:
        if arguments.asset_command == "add":
            asset = add_asset(
                connection,
                tag=arguments.tag,
                serial=arguments.serial,
                asset_type=arguments.asset_type,
                manufacturer=arguments.manufacturer,
                model=arguments.model,
                lifecycle_status=arguments.status,
                warranty_vendor=arguments.warranty_vendor,
                warranty_reference=arguments.warranty_reference,
                warranty_start=arguments.warranty_start,
                warranty_end=arguments.warranty_end,
            )
            print(f"Added asset: {asset.tag}")
            return 0
        if arguments.asset_command == "edit":
            asset = edit_asset(
                connection,
                arguments.tag,
                asset_type=arguments.asset_type,
                manufacturer=arguments.manufacturer,
                model=arguments.model,
                warranty_vendor=arguments.warranty_vendor,
                warranty_reference=arguments.warranty_reference,
                warranty_start=arguments.warranty_start,
                warranty_end=arguments.warranty_end,
            )
            print(f"Updated asset: {asset.tag}")
            return 0
        if arguments.asset_command == "identify":
            asset = identify_asset(
                connection, arguments.tag, new_tag=arguments.new_tag, serial=arguments.serial
            )
            print(f"Updated asset identity: {asset.tag}")
            return 0
        if arguments.asset_command == "retire":
            asset = retire_asset(connection, arguments.tag)
            print(f"Retired asset: {asset.tag}")
            return 0
        if arguments.asset_command == "list":
            _print_asset_list(list_assets(connection), as_of=date.today())
            return 0
        if arguments.asset_command == "show":
            _print_asset(get_asset(connection, arguments.tag), _parse_as_of(arguments.as_of))
            return 0
    except AssetValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except AssetError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    finally:
        connection.close()

    raise AssertionError(f"unsupported asset command: {arguments.asset_command}")


def _run_case_command(arguments: argparse.Namespace) -> int:
    try:
        connection = connect_database(arguments.database)
    except DatabaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 4

    try:
        if arguments.case_command == "open":
            case = open_case(
                connection,
                reference=arguments.case_reference,
                asset_tag=arguments.asset_tag,
                vendor_key=arguments.vendor_key,
                opened_at=arguments.opened_at,
                operator_alias=arguments.operator_alias,
                response_due_at=arguments.response_due_at,
                resolution_due_at=arguments.resolution_due_at,
            )
            print(f"Opened case: {case.reference}")
            return 0
        if arguments.case_command == "show":
            case = get_case(connection, arguments.reference)
            events = list_case_events(connection, arguments.reference) if arguments.history else ()
            _print_case(case, events)
            return 0
        if arguments.case_command == "vendor-response":
            case = record_vendor_response(
                connection,
                arguments.reference,
                at=arguments.at,
                operator_alias=arguments.operator_alias,
                vendor_reference=arguments.vendor_reference,
            )
            print(f"Recorded vendor response: {case.reference}")
            return 0
        if arguments.case_command == "authorise":
            case = authorise_case(
                connection,
                arguments.reference,
                at=arguments.at,
                operator_alias=arguments.operator_alias,
            )
            print(f"Authorised case: {case.reference}")
            return 0
        if arguments.case_command == "dispatch":
            case = dispatch_case(
                connection,
                arguments.reference,
                at=arguments.at,
                operator_alias=arguments.operator_alias,
                carrier=arguments.carrier,
                tracking=arguments.tracking,
            )
            print(f"Dispatched case: {case.reference}")
            return 0
        if arguments.case_command == "vendor-received":
            case = record_vendor_receipt(
                connection,
                arguments.reference,
                at=arguments.at,
                operator_alias=arguments.operator_alias,
            )
            print(f"Recorded vendor receipt: {case.reference}")
            return 0
        if arguments.case_command == "return-dispatch":
            case = dispatch_return(
                connection,
                arguments.reference,
                at=arguments.at,
                operator_alias=arguments.operator_alias,
                carrier=arguments.carrier,
                tracking=arguments.tracking,
            )
            print(f"Recorded return dispatch: {case.reference}")
            return 0
        if arguments.case_command == "return-received":
            case = receive_return(
                connection,
                arguments.reference,
                at=arguments.at,
                operator_alias=arguments.operator_alias,
            )
            print(f"Recorded return receipt: {case.reference}")
            return 0
        if arguments.case_command == "deadline":
            case = change_case_deadlines(
                connection,
                arguments.reference,
                at=arguments.at,
                operator_alias=arguments.operator_alias,
                reason=arguments.reason,
                response_due_at=arguments.response_due_at,
                resolution_due_at=arguments.resolution_due_at,
            )
            print(f"Changed case deadlines: {case.reference}")
            return 0
        if arguments.case_command == "outcome":
            case = record_case_outcome(
                connection,
                arguments.reference,
                at=arguments.at,
                operator_alias=arguments.operator_alias,
                outcome=arguments.outcome,
                note=arguments.note,
            )
            print(f"Recorded case outcome: {case.reference}")
            return 0
        if arguments.case_command == "note":
            case = add_case_note(
                connection,
                arguments.reference,
                at=arguments.at,
                operator_alias=arguments.operator_alias,
                note=arguments.note,
            )
            print(f"Added case note: {case.reference}")
            return 0
        if arguments.case_command == "correct-outcome":
            case = correct_case_outcome(
                connection,
                arguments.reference,
                at=arguments.at,
                operator_alias=arguments.operator_alias,
                original_event_id=arguments.original_event_id,
                outcome=arguments.outcome,
                reason=arguments.reason,
                note=arguments.note,
            )
            print(f"Corrected case outcome: {case.reference}")
            return 0
        if arguments.case_command == "close":
            case = close_case(
                connection,
                arguments.reference,
                at=arguments.at,
                operator_alias=arguments.operator_alias,
                asset_status=arguments.asset_status,
            )
            print(f"Closed case: {case.reference}")
            return 0
        if arguments.case_command == "cancel":
            case = cancel_case(
                connection,
                arguments.reference,
                at=arguments.at,
                operator_alias=arguments.operator_alias,
                reason=arguments.reason,
            )
            print(f"Cancelled case: {case.reference}")
            return 0
    except CaseValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except CaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    finally:
        connection.close()

    raise AssertionError(f"unsupported case command: {arguments.case_command}")


def _run_due_command(arguments: argparse.Namespace) -> int:
    try:
        within_hours = _parse_due_window(arguments.within)
        as_of = arguments.as_of or _current_utc_timestamp()
        connection = connect_database(arguments.database)
    except DeadlineValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except DatabaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 4

    try:
        _print_due_cases(due_cases(connection, as_of=as_of, within_hours=within_hours))
        return 0
    except DeadlineValidationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except DeadlineError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    finally:
        connection.close()


def _run_import_command(arguments: argparse.Namespace) -> int:
    try:
        connection = connect_database(arguments.database)
    except DatabaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 4
    try:
        if arguments.import_command == "vendors":
            summary = import_vendors_csv(connection, arguments.path, dry_run=arguments.dry_run)
        elif arguments.import_command == "assets":
            summary = import_assets_csv(connection, arguments.path, dry_run=arguments.dry_run)
        else:
            summary = import_cases_csv(
                connection,
                arguments.path,
                operator_alias=arguments.by,
                dry_run=arguments.dry_run,
            )
    except CsvImportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        connection.close()
    action = "Validated" if summary.dry_run else "Imported"
    print(f"{action} {summary.rows} {summary.kind} rows.")
    return 0


def _run_export_command(arguments: argparse.Namespace) -> int:
    try:
        connection = connect_database(arguments.database)
    except DatabaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 4
    try:
        summary = export_csv_bundle(connection, arguments.output_dir, as_of=arguments.as_of)
    except CsvExportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        connection.close()
    print(f"Exported ledger bundle: {summary.output_dir}")
    return 0


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


def _print_asset_list(assets: tuple[Asset, ...], *, as_of: date) -> None:
    print("TAG\tSTATUS\tTYPE\tSERIAL\tWARRANTY\tMODEL")
    for asset in assets:
        print(
            "\t".join(
                (
                    asset.tag,
                    asset.lifecycle_status,
                    asset.asset_type,
                    asset.serial,
                    asset.warranty_state(as_of),
                    asset.model,
                )
            )
        )


def _print_asset(asset: Asset, as_of: date) -> None:
    fields = (
        ("Tag", asset.tag),
        ("Serial", asset.serial),
        ("Type", asset.asset_type),
        ("Manufacturer", asset.manufacturer),
        ("Model", asset.model),
        ("Status", asset.lifecycle_status),
        ("Warranty vendor", asset.warranty_vendor_key or "-"),
        ("Warranty reference", asset.warranty_reference or "-"),
        ("Warranty start", _format_date(asset.warranty_start)),
        ("Warranty end", _format_date(asset.warranty_end)),
        ("Warranty status", asset.warranty_state(as_of)),
    )
    for label, value in fields:
        print(f"{label}: {value}")


def _print_case(case: RmaCase, events: tuple[CaseEvent, ...]) -> None:
    fields = (
        ("Reference", case.reference),
        ("Asset", case.asset_tag),
        ("Vendor", case.vendor_key),
        ("Status", case.current_status),
        ("Opened at", case.opened_at),
        ("Response due", case.response_due_at or "-"),
        ("Resolution due", case.resolution_due_at or "-"),
        ("Last event", str(case.last_event_sequence)),
    )
    for label, value in fields:
        print(f"{label}: {value}")
    if events:
        print("History:")
        print("SEQUENCE\tTYPE\tOCCURRED AT\tBY\tEVENT ID")
        for event in events:
            print(
                "\t".join(
                    (
                        str(event.sequence),
                        event.event_type,
                        event.occurred_at,
                        event.operator_alias,
                        event.event_id,
                    )
                )
            )


def _print_due_cases(cases: tuple[DueCase, ...]) -> None:
    print("TYPE\tSTATE\tDUE AT\tCASE\tASSET\tVENDOR")
    for case in cases:
        print(
            "\t".join(
                (
                    case.deadline_type,
                    case.state,
                    case.deadline_at,
                    case.reference,
                    case.asset_tag,
                    case.vendor_key,
                )
            )
        )


def _format_minutes(minutes: int | None) -> str:
    if minutes is None:
        return "-"
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours}h {remainder}m"
    if hours:
        return f"{hours}h"
    return f"{remainder}m"


def _parse_as_of(value: str | None) -> date:
    if value is None:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise AssetValidationError("as-of date must be an ISO calendar date") from None


def _parse_due_window(value: str) -> int:
    if not value.endswith("h"):
        raise DeadlineValidationError("within must be a positive whole-hour value such as 48h")
    try:
        hours = int(value[:-1])
    except ValueError:
        raise DeadlineValidationError(
            "within must be a positive whole-hour value such as 48h"
        ) from None
    if hours <= 0 or str(hours) != value[:-1]:
        raise DeadlineValidationError("within must be a positive whole-hour value such as 48h")
    return hours


def _current_utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _format_date(value: date | None) -> str:
    return value.isoformat() if value is not None else "-"
