from __future__ import annotations

from pathlib import Path

import pytest

from asset_rma_ledger import __version__
from asset_rma_ledger.cli import main


def test_init_command_creates_a_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"

    exit_code = main(["--database", str(database_path), "init"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert database_path.is_file()
    assert "Initialised ledger database" in captured.out
    assert captured.err == ""


def test_init_command_reports_an_existing_database_without_overwriting_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"

    assert main(["--database", str(database_path), "init"]) == 0
    exit_code = main(["--database", str(database_path), "init"])

    captured = capsys.readouterr()
    assert exit_code == 4
    assert "already exists" in captured.err


def test_mutating_commands_require_an_explicit_database_path() -> None:
    with pytest.raises(SystemExit) as error:
        main(["init"])

    assert error.value.code == 2


def test_version_option_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--version"])

    captured = capsys.readouterr()
    assert error.value.code == 0
    assert captured.out.strip() == __version__


def test_vendor_commands_add_list_show_and_deactivate_a_vendor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"
    assert main(["--database", str(database_path), "init"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--database",
                str(database_path),
                "vendor",
                "add",
                "--key",
                "northstar",
                "--name",
                "Northstar Repairs",
                "--response-sla-hours",
                "8",
            ]
        )
        == 0
    )
    assert main(["--database", str(database_path), "vendor", "deactivate", "northstar"]) == 0
    assert main(["--database", str(database_path), "vendor", "list"]) == 0
    assert main(["--database", str(database_path), "vendor", "show", "northstar"]) == 0

    captured = capsys.readouterr()
    assert "northstar" in captured.out
    assert "inactive" in captured.out
    assert "Northstar Repairs" in captured.out


def test_vendor_add_returns_invalid_argument_exit_code_for_bad_support_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"
    assert main(["--database", str(database_path), "init"]) == 0
    capsys.readouterr()

    exit_code = main(
        [
            "--database",
            str(database_path),
            "vendor",
            "add",
            "--key",
            "northstar",
            "--name",
            "Northstar Repairs",
            "--support-url",
            "http://support.example.test",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "HTTPS" in captured.err


def test_asset_commands_register_show_identify_retire_and_list_an_asset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"
    assert main(["--database", str(database_path), "init"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--database",
                str(database_path),
                "asset",
                "add",
                "--tag",
                "LAP-0042",
                "--serial",
                "SN-A1B2C3",
                "--type",
                "laptop",
                "--manufacturer",
                "ExampleCo",
                "--model",
                "ProBook-14",
                "--warranty-start",
                "2026-08-01",
                "--warranty-end",
                "2027-07-31",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "asset",
                "identify",
                "LAP-0042",
                "--serial",
                "SN-D4E5F6",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "asset",
                "show",
                "LAP-0042",
                "--as-of",
                "2026-08-30",
            ]
        )
        == 0
    )
    assert main(["--database", str(database_path), "asset", "retire", "LAP-0042"]) == 0
    assert main(["--database", str(database_path), "asset", "list"]) == 0

    captured = capsys.readouterr()
    assert "SN-D4E5F6" in captured.out
    assert "Warranty status: active" in captured.out
    assert "retired" in captured.out


def test_asset_add_returns_invalid_argument_exit_code_for_in_rma_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"
    assert main(["--database", str(database_path), "init"]) == 0
    capsys.readouterr()

    exit_code = main(
        [
            "--database",
            str(database_path),
            "asset",
            "add",
            "--tag",
            "LAP-0042",
            "--serial",
            "SN-A1B2C3",
            "--type",
            "laptop",
            "--manufacturer",
            "ExampleCo",
            "--model",
            "ProBook-14",
            "--status",
            "in_rma",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "cannot be selected" in captured.err


def test_case_open_and_show_history_commands_create_an_auditable_case(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"
    assert main(["--database", str(database_path), "init"]) == 0
    assert (
        main(
            [
                "--database",
                str(database_path),
                "vendor",
                "add",
                "--key",
                "northstar",
                "--name",
                "Northstar Repairs",
                "--response-sla-hours",
                "8",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "asset",
                "add",
                "--tag",
                "LAP-0042",
                "--serial",
                "SN-A1B2C3",
                "--type",
                "laptop",
                "--manufacturer",
                "ExampleCo",
                "--model",
                "ProBook-14",
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "--database",
                str(database_path),
                "case",
                "open",
                "--case",
                "RMA-2026-001",
                "--asset",
                "LAP-0042",
                "--vendor",
                "northstar",
                "--opened-at",
                "2026-08-30T09:00:00Z",
                "--by",
                "ej",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "case",
                "show",
                "RMA-2026-001",
                "--history",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "Opened case: RMA-2026-001" in captured.out
    assert "Response due: 2026-08-30T17:00:00Z" in captured.out
    assert "History:" in captured.out
    assert "1\tcase_opened\t2026-08-30T09:00:00Z\tej" in captured.out


def test_case_open_returns_invalid_argument_exit_code_for_non_utc_open_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"
    assert main(["--database", str(database_path), "init"]) == 0
    assert (
        main(
            [
                "--database",
                str(database_path),
                "vendor",
                "add",
                "--key",
                "northstar",
                "--name",
                "Northstar Repairs",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "asset",
                "add",
                "--tag",
                "LAP-0042",
                "--serial",
                "SN-A1B2C3",
                "--type",
                "laptop",
                "--manufacturer",
                "ExampleCo",
                "--model",
                "ProBook-14",
            ]
        )
        == 0
    )
    capsys.readouterr()

    exit_code = main(
        [
            "--database",
            str(database_path),
            "case",
            "open",
            "--case",
            "RMA-2026-001",
            "--asset",
            "LAP-0042",
            "--vendor",
            "northstar",
            "--opened-at",
            "2026-08-30T09:00:00+01:00",
            "--by",
            "ej",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "UTC RFC 3339" in captured.err


def test_case_lifecycle_commands_append_operational_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"
    assert main(["--database", str(database_path), "init"]) == 0
    assert (
        main(
            [
                "--database",
                str(database_path),
                "vendor",
                "add",
                "--key",
                "northstar",
                "--name",
                "Northstar Repairs",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "asset",
                "add",
                "--tag",
                "LAP-0042",
                "--serial",
                "SN-A1B2C3",
                "--type",
                "laptop",
                "--manufacturer",
                "ExampleCo",
                "--model",
                "ProBook-14",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "case",
                "open",
                "--case",
                "RMA-2026-001",
                "--asset",
                "LAP-0042",
                "--vendor",
                "northstar",
                "--opened-at",
                "2026-08-30T09:00:00Z",
                "--by",
                "ej",
            ]
        )
        == 0
    )

    commands = (
        ["vendor-response", "RMA-2026-001", "--at", "2026-08-30T09:30:00Z", "--by", "ej"],
        ["authorise", "RMA-2026-001", "--at", "2026-08-30T10:00:00Z", "--by", "ej"],
        [
            "dispatch",
            "RMA-2026-001",
            "--at",
            "2026-08-31T10:00:00Z",
            "--by",
            "ej",
            "--carrier",
            "Example Carrier",
            "--tracking",
            "TRACK-OUT-001",
        ],
        ["vendor-received", "RMA-2026-001", "--at", "2026-09-01T09:00:00Z", "--by", "ej"],
        [
            "return-dispatch",
            "RMA-2026-001",
            "--at",
            "2026-09-03T12:00:00Z",
            "--by",
            "ej",
            "--carrier",
            "Example Carrier",
            "--tracking",
            "TRACK-RETURN-001",
        ],
        ["return-received", "RMA-2026-001", "--at", "2026-09-04T09:00:00Z", "--by", "ej"],
    )
    for command in commands:
        assert main(["--database", str(database_path), "case", *command]) == 0

    assert (
        main(
            [
                "--database",
                str(database_path),
                "case",
                "show",
                "RMA-2026-001",
                "--history",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "Status: returned" in captured.out
    assert "7\treturn_received\t2026-09-04T09:00:00Z\tej" in captured.out


def test_case_deadline_and_due_commands_use_explicit_utc_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"
    assert main(["--database", str(database_path), "init"]) == 0
    assert (
        main(
            [
                "--database",
                str(database_path),
                "vendor",
                "add",
                "--key",
                "northstar",
                "--name",
                "Northstar Repairs",
                "--response-sla-hours",
                "8",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "asset",
                "add",
                "--tag",
                "LAP-0042",
                "--serial",
                "SN-A1B2C3",
                "--type",
                "laptop",
                "--manufacturer",
                "ExampleCo",
                "--model",
                "ProBook-14",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "case",
                "open",
                "--case",
                "RMA-2026-001",
                "--asset",
                "LAP-0042",
                "--vendor",
                "northstar",
                "--opened-at",
                "2026-08-30T09:00:00Z",
                "--by",
                "ej",
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "--database",
                str(database_path),
                "case",
                "deadline",
                "RMA-2026-001",
                "--at",
                "2026-08-30T10:00:00Z",
                "--by",
                "ej",
                "--reason",
                "Vendor confirmed a shorter response target.",
                "--response-due-at",
                "2026-08-30T12:00:00Z",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "due",
                "--within",
                "2h",
                "--as-of",
                "2026-08-30T10:00:00Z",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "Changed case deadlines: RMA-2026-001" in captured.out
    assert "response\tdue_soon\t2026-08-30T12:00:00Z\tRMA-2026-001" in captured.out


def test_case_outcome_correction_and_terminal_commands_append_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from asset_rma_ledger.cases import list_case_events
    from asset_rma_ledger.database import connect_database

    database_path = tmp_path / "team-assets.db"
    assert main(["--database", str(database_path), "init"]) == 0
    assert (
        main(
            [
                "--database",
                str(database_path),
                "vendor",
                "add",
                "--key",
                "northstar",
                "--name",
                "Northstar Repairs",
            ]
        )
        == 0
    )
    for tag, serial in (("LAP-0042", "SN-A1B2C3"), ("LAP-0043", "SN-A1B2C4")):
        assert (
            main(
                [
                    "--database",
                    str(database_path),
                    "asset",
                    "add",
                    "--tag",
                    tag,
                    "--serial",
                    serial,
                    "--type",
                    "laptop",
                    "--manufacturer",
                    "ExampleCo",
                    "--model",
                    "ProBook-14",
                ]
            )
            == 0
        )
    for reference, tag in (("RMA-2026-001", "LAP-0042"), ("RMA-2026-002", "LAP-0043")):
        assert (
            main(
                [
                    "--database",
                    str(database_path),
                    "case",
                    "open",
                    "--case",
                    reference,
                    "--asset",
                    tag,
                    "--vendor",
                    "northstar",
                    "--opened-at",
                    "2026-08-30T09:00:00Z",
                    "--by",
                    "ej",
                ]
            )
            == 0
        )

    assert (
        main(
            [
                "--database",
                str(database_path),
                "case",
                "outcome",
                "RMA-2026-001",
                "--at",
                "2026-08-30T10:00:00Z",
                "--by",
                "ej",
                "--outcome",
                "refund",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "case",
                "note",
                "RMA-2026-001",
                "--at",
                "2026-08-30T10:30:00Z",
                "--by",
                "ej",
                "--note",
                "Vendor confirmed that the device will not be returned.",
            ]
        )
        == 0
    )
    connection = connect_database(database_path)
    try:
        outcome_event = list_case_events(connection, "RMA-2026-001")[1]
    finally:
        connection.close()
    assert (
        main(
            [
                "--database",
                str(database_path),
                "case",
                "correct-outcome",
                "RMA-2026-001",
                "--at",
                "2026-08-30T11:00:00Z",
                "--by",
                "ej",
                "--original-event-id",
                outcome_event.event_id,
                "--outcome",
                "written_off",
                "--reason",
                "Vendor corrected the proposed financial remedy.",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "case",
                "close",
                "RMA-2026-001",
                "--at",
                "2026-08-30T11:30:00Z",
                "--by",
                "ej",
                "--asset-status",
                "retired",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "case",
                "cancel",
                "RMA-2026-002",
                "--at",
                "2026-08-30T10:00:00Z",
                "--by",
                "ej",
                "--reason",
                "Vendor confirmed that a return is not required.",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "case",
                "show",
                "RMA-2026-001",
                "--history",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "Recorded case outcome: RMA-2026-001" in captured.out
    assert "Added case note: RMA-2026-001" in captured.out
    assert "Corrected case outcome: RMA-2026-001" in captured.out
    assert "Closed case: RMA-2026-001" in captured.out
    assert "Cancelled case: RMA-2026-002" in captured.out
    assert outcome_event.event_id in captured.out


def test_import_vendor_command_supports_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"
    source = tmp_path / "vendors.csv"
    source.write_text(
        "vendor_key,name,support_url,support_email,support_phone,account_reference,response_sla_hours,resolution_sla_hours,active\n"
        "northstar,Northstar Repairs,,,,,8,120,true\n",
        encoding="utf-8",
    )
    assert main(["--database", str(database_path), "init"]) == 0
    assert (
        main(
            [
                "--database",
                str(database_path),
                "import",
                "vendors",
                str(source),
                "--by",
                "ej",
                "--dry-run",
            ]
        )
        == 0
    )
    assert (
        main(["--database", str(database_path), "import", "vendors", str(source), "--by", "ej"])
        == 0
    )
    captured = capsys.readouterr()
    assert "Validated 1 vendors rows." in captured.out
    assert "Imported 1 vendors rows." in captured.out


def test_import_case_command_reconstructs_a_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"
    source = tmp_path / "cases.csv"
    source.write_text(
        "case_reference,asset_tag,vendor_key,opened_at,status,vendor_reference,response_due_at,resolution_due_at,vendor_responded_at,outbound_dispatched_at,vendor_received_at,return_dispatched_at,returned_at,outcome,closed_at\n"
        "RMA-2026-001,LAP-0042,northstar,2026-08-30T09:00:00Z,outbound,,,,,2026-08-31T10:00:00Z,,,,,\n",
        encoding="utf-8",
    )
    assert main(["--database", str(database_path), "init"]) == 0
    assert (
        main(
            [
                "--database",
                str(database_path),
                "vendor",
                "add",
                "--key",
                "northstar",
                "--name",
                "Northstar Repairs",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "asset",
                "add",
                "--tag",
                "LAP-0042",
                "--serial",
                "SN-A1B2C3",
                "--type",
                "laptop",
                "--manufacturer",
                "ExampleCo",
                "--model",
                "ProBook-14",
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "--database",
                str(database_path),
                "import",
                "cases",
                str(source),
                "--by",
                "ej",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--database",
                str(database_path),
                "case",
                "show",
                "RMA-2026-001",
                "--history",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "Imported 1 cases rows." in captured.out
    assert "Status: outbound" in captured.out
    assert "outbound_dispatched" in captured.out


def test_export_command_publishes_a_csv_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"
    output = tmp_path / "ledger-export"
    assert main(["--database", str(database_path), "init"]) == 0
    assert (
        main(
            [
                "--database",
                str(database_path),
                "vendor",
                "add",
                "--key",
                "northstar",
                "--name",
                "Northstar Repairs",
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "--database",
                str(database_path),
                "export",
                "--output-dir",
                str(output),
                "--as-of",
                "2026-08-30T18:00:00Z",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert f"Exported ledger bundle: {output}" in captured.out
    assert (output / "vendors.csv").is_file()
    assert (output / "export_manifest.json").is_file()


def test_verify_command_reports_a_valid_empty_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "team-assets.db"
    assert main(["--database", str(database_path), "init"]) == 0

    assert main(["--database", str(database_path), "verify"]) == 0

    captured = capsys.readouterr()
    assert "Verified ledger: 6 checks, 0 cases, 0 events." in captured.out
    assert captured.err == ""


def test_verify_command_returns_integrity_exit_code_for_projection_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from asset_rma_ledger.assets import add_asset
    from asset_rma_ledger.cases import open_case
    from asset_rma_ledger.database import connect_database
    from asset_rma_ledger.vendors import add_vendor

    database_path = tmp_path / "team-assets.db"
    assert main(["--database", str(database_path), "init"]) == 0
    connection = connect_database(database_path)
    try:
        add_vendor(connection, key="northstar", name="Northstar Repairs")
        add_asset(
            connection,
            tag="LAP-0042",
            serial="SN-A1B2C3",
            asset_type="laptop",
            manufacturer="ExampleCo",
            model="ProBook-14",
        )
        open_case(
            connection,
            reference="RMA-2026-001",
            asset_tag="LAP-0042",
            vendor_key="northstar",
            opened_at="2026-08-30T09:00:00Z",
            operator_alias="ej",
        )
        connection.execute(
            "UPDATE rma_cases SET current_status = 'authorised' WHERE case_reference = ?",
            ("RMA-2026-001",),
        )
    finally:
        connection.close()
    capsys.readouterr()

    exit_code = main(["--database", str(database_path), "verify"])

    captured = capsys.readouterr()
    assert exit_code == 5
    assert "projection check failed for case RMA-2026-001: current_status" in captured.err
