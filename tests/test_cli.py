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
