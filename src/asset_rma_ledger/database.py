"""SQLite schema and connection policy for the local ledger."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1
APPLICATION_ID = "asset-rma-ledger"


class DatabaseError(RuntimeError):
    """Raised when a database cannot be created or opened safely."""


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE schema_metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        application_id TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK (schema_version > 0),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE vendors (
        id INTEGER PRIMARY KEY,
        vendor_key TEXT NOT NULL,
        vendor_key_folded TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        support_url TEXT,
        support_email TEXT,
        support_phone TEXT,
        account_reference TEXT,
        response_sla_minutes INTEGER CHECK (
            response_sla_minutes IS NULL OR response_sla_minutes > 0
        ),
        resolution_sla_minutes INTEGER CHECK (
            resolution_sla_minutes IS NULL OR resolution_sla_minutes > 0
        ),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE assets (
        id INTEGER PRIMARY KEY,
        asset_tag TEXT NOT NULL,
        asset_tag_folded TEXT NOT NULL UNIQUE,
        serial_number TEXT NOT NULL,
        serial_number_folded TEXT NOT NULL UNIQUE,
        asset_type TEXT NOT NULL,
        manufacturer TEXT NOT NULL,
        model TEXT NOT NULL,
        lifecycle_status TEXT NOT NULL CHECK (
            lifecycle_status IN ('in_stock', 'deployed', 'in_rma', 'retired')
        ),
        warranty_vendor_id INTEGER REFERENCES vendors(id) ON DELETE RESTRICT,
        warranty_reference TEXT,
        warranty_start TEXT,
        warranty_end TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE rma_cases (
        id INTEGER PRIMARY KEY,
        case_reference TEXT NOT NULL,
        case_reference_folded TEXT NOT NULL UNIQUE,
        asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
        vendor_id INTEGER NOT NULL REFERENCES vendors(id) ON DELETE RESTRICT,
        opened_at TEXT NOT NULL,
        current_status TEXT NOT NULL CHECK (
            current_status IN (
                'open', 'authorised', 'outbound', 'with_vendor', 'returning',
                'returned', 'closed', 'cancelled'
            )
        ),
        vendor_reference TEXT,
        response_due_at TEXT,
        resolution_due_at TEXT,
        vendor_responded_at TEXT,
        outbound_dispatched_at TEXT,
        vendor_received_at TEXT,
        return_dispatched_at TEXT,
        returned_at TEXT,
        current_outcome TEXT CHECK (
            current_outcome IS NULL OR current_outcome IN (
                'repaired', 'replaced', 'refund', 'no_fault_found',
                'repair_declined', 'written_off', 'other'
            )
        ),
        closed_at TEXT,
        last_event_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_event_sequence >= 0),
        last_event_hash TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX one_active_case_per_asset
    ON rma_cases(asset_id)
    WHERE current_status NOT IN ('closed', 'cancelled')
    """,
    """
    CREATE TABLE case_events (
        id INTEGER PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE,
        case_id INTEGER NOT NULL REFERENCES rma_cases(id) ON DELETE RESTRICT,
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        event_type TEXT NOT NULL CHECK (
            event_type IN (
                'case_opened', 'vendor_response_recorded', 'status_changed',
                'outbound_dispatched', 'vendor_receipt_recorded',
                'return_dispatched', 'return_received', 'deadline_changed',
                'outcome_recorded', 'note_added', 'correction_recorded',
                'case_closed', 'case_cancelled'
            )
        ),
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        operator_alias TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        previous_hash TEXT NOT NULL,
        event_hash TEXT NOT NULL,
        UNIQUE (case_id, sequence)
    )
    """,
    """
    CREATE TRIGGER case_events_no_update
    BEFORE UPDATE ON case_events
    BEGIN
        SELECT RAISE(ABORT, 'case events are append-only');
    END
    """,
    """
    CREATE TRIGGER case_events_no_delete
    BEFORE DELETE ON case_events
    BEGIN
        SELECT RAISE(ABORT, 'case events are append-only');
    END
    """,
)


def _configure_connection(connection: sqlite3.Connection) -> None:
    """Apply connection-local integrity and lock controls."""
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    foreign_keys_enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    if foreign_keys_enabled != 1:
        raise DatabaseError("SQLite foreign-key enforcement could not be enabled")

    connection.execute("PRAGMA busy_timeout = 5000")


def initialise_database(database_path: Path) -> None:
    """Create a schema-1 ledger database without overwriting an existing path."""
    database_path = Path(database_path)

    try:
        file_descriptor = os.open(
            database_path,
            os.O_CREAT | os.O_EXCL | os.O_RDWR,
            0o600,
        )
    except FileExistsError as error:
        raise DatabaseError(f"database already exists: {database_path}") from error
    except OSError as error:
        raise DatabaseError(f"could not create database: {database_path}") from error

    os.close(file_descriptor)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database_path, isolation_level=None)
        _configure_connection(connection)
        connection.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO schema_metadata (
                singleton, application_id, schema_version, created_at
            ) VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """,
            (1, APPLICATION_ID, SCHEMA_VERSION),
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except (DatabaseError, OSError, sqlite3.Error) as error:
        if connection is not None:
            connection.rollback()
        try:
            database_path.unlink()
        except OSError:
            pass
        raise DatabaseError(f"could not initialise database: {database_path}") from error
    finally:
        if connection is not None:
            connection.close()


def connect_database(database_path: Path) -> sqlite3.Connection:
    """Open an existing schema-1 ledger database with required safety controls."""
    database_path = Path(database_path)
    if not database_path.is_file():
        raise DatabaseError(f"database does not exist: {database_path}")

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database_path, isolation_level=None)
        _configure_connection(connection)
        metadata = connection.execute(
            """
            SELECT application_id, schema_version
            FROM schema_metadata
            WHERE singleton = 1
            """
        ).fetchone()
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    except (DatabaseError, sqlite3.Error) as error:
        if connection is not None:
            connection.close()
        raise DatabaseError(f"could not open ledger database: {database_path}") from error

    if metadata is None or metadata["application_id"] != APPLICATION_ID:
        connection.close()
        raise DatabaseError(f"unrecognised ledger database: {database_path}")

    if metadata["schema_version"] != SCHEMA_VERSION or user_version != SCHEMA_VERSION:
        connection.close()
        raise DatabaseError(f"unsupported schema version: {database_path}")

    return connection
