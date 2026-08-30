from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from asset_rma_ledger.database import (
    SCHEMA_VERSION,
    DatabaseError,
    connect_database,
    initialise_database,
)


def test_initialise_database_creates_the_schema_and_enforces_foreign_keys(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "team-assets.db"

    initialise_database(database_path)

    connection = connect_database(database_path)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        schema_version = connection.execute(
            "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
        ).fetchone()[0]
        foreign_keys_enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]

        assert {"schema_metadata", "vendors", "assets", "rma_cases", "case_events"} <= tables
        assert schema_version == SCHEMA_VERSION
        assert foreign_keys_enabled == 1
    finally:
        connection.close()


def test_initialise_database_refuses_to_overwrite_an_existing_file(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "team-assets.db"
    database_path.write_bytes(b"do not overwrite")

    with pytest.raises(DatabaseError, match="already exists"):
        initialise_database(database_path)

    assert database_path.read_bytes() == b"do not overwrite"


def test_case_events_cannot_be_updated_or_deleted_after_insertion(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "team-assets.db"
    initialise_database(database_path)

    connection = connect_database(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO vendors (
                vendor_key, vendor_key_folded, name, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "northstar",
                "northstar",
                "Northstar Repairs",
                1,
                "2026-08-30T09:00:00Z",
                "2026-08-30T09:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO assets (
                asset_tag, asset_tag_folded, serial_number, serial_number_folded,
                asset_type, manufacturer, model, lifecycle_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "LAP-0042",
                "lap-0042",
                "SN-A1B2C3",
                "sn-a1b2c3",
                "laptop",
                "ExampleCo",
                "ProBook-14",
                "in_stock",
                "2026-08-30T09:00:00Z",
                "2026-08-30T09:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO rma_cases (
                case_reference, case_reference_folded, asset_id, vendor_id, opened_at,
                current_status, last_event_sequence, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "RMA-2026-001",
                "rma-2026-001",
                1,
                1,
                "2026-08-30T09:00:00Z",
                "open",
                1,
                "2026-08-30T09:00:00Z",
                "2026-08-30T09:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO case_events (
                event_id, case_id, sequence, event_type, occurred_at, recorded_at,
                operator_alias, payload_json, previous_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "00000000-0000-4000-8000-000000000001",
                1,
                1,
                "case_opened",
                "2026-08-30T09:00:00Z",
                "2026-08-30T09:01:00Z",
                "ej",
                "{}",
                "0" * 64,
                "1" * 64,
            ),
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE case_events SET event_type = 'note_added'")

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM case_events")
    finally:
        connection.close()


def test_connect_database_rejects_an_unsupported_schema_version(tmp_path: Path) -> None:
    database_path = tmp_path / "team-assets.db"
    initialise_database(database_path)

    connection = connect_database(database_path)
    try:
        connection.execute("UPDATE schema_metadata SET schema_version = 999")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseError, match="unsupported schema version"):
        connect_database(database_path)
