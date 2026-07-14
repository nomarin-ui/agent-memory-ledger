"""Canonical serialization and hash-chain computation."""
from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS_HASH = "0" * 64


def canonical_json(value: Any) -> str | None:
    """Deterministic JSON. Same input -> same bytes, always.

    None round-trips as None (SQL NULL), not the string "null".
    """
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_op_hash(
    *,
    prev_hash: str,
    ts: str,
    agent_id: str,
    operation: str,
    key: str,
    old_value: str | None,
    new_value: str | None,
    provenance: str | None,
) -> str:
    """SHA256 over the previous hash plus this row's payload.

    Field order is fixed and MUST NOT change -- doing so invalidates
    every existing chain. Values are already canonical JSON strings.
    NUL byte as separator: it cannot appear in the JSON or in an
    ISO timestamp, so no field can be forged by splicing.
    """
    payload = "\x00".join(
        [
            prev_hash,
            ts,
            agent_id,
            operation,
            key,
            old_value or "",
            new_value or "",
            provenance or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()