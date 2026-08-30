"""Canonical payload and hashing helpers for immutable case events."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .database import SCHEMA_VERSION

ZERO_EVENT_HASH = "0" * 64


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Encode a fixed event payload into the ledger's canonical JSON form."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def calculate_event_hash(
    *,
    case_reference: str,
    sequence: int,
    event_id: str,
    event_type: str,
    occurred_at: str,
    recorded_at: str,
    operator_alias: str,
    payload_json: str,
    previous_hash: str,
) -> str:
    """Return the SHA-256 digest for one fully specified case event."""
    material = canonical_json(
        {
            "case_reference": case_reference,
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "operator_alias": operator_alias,
            "payload_json": payload_json,
            "previous_hash": previous_hash,
            "recorded_at": recorded_at,
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
