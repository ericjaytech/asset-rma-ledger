"""Command-line interface for Asset RMA Ledger."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .database import DatabaseError, initialise_database


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

    parser.error(f"unsupported command: {arguments.command}")
    return 2
