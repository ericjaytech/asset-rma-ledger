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
