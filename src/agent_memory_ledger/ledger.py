"""MemoryLedger -- the public API."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .storage import Operation, SQLiteStorage, utcnow_iso

from dataclasses import dataclass
from datetime import datetime, timedelta


def _to_iso(when: str | datetime) -> str:
    return when.isoformat() if isinstance(when, datetime) else when


def _decode(raw: str | None) -> Any:
    return None if raw is None else json.loads(raw)

@dataclass(frozen=True, slots=True)
class ReadRecord:
    """One time an agent consulted its memory, and what it saw."""

    ts: str                      # when the agent read
    key: str
    value_seen: Any              # what it got back (None = key was a miss)
    written_at: str | None       # when that value was written
    age_at_read: timedelta | None  # how stale the value already was
    provenance: str | None       # why the read happened
    op_id_seen: int | None       # the exact ledger row consulted

class MemoryLedger:
    """Append-only, hash-chained record of what an agent believed, and when."""

    def __init__(self, agent_id: str, path: str | Path = "ledger.db") -> None:
        self.agent_id = agent_id
        self.storage = SQLiteStorage(path)

    def close(self) -> None:
        self.storage.close()

    def __enter__(self) -> "MemoryLedger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- mutations -----------------------------------------------------

    def write(self, key: str, value: Any, *, provenance: str | None = None) -> Operation:
        """Record a belief. If the key already exists this is an 'update'."""
        existing = self.storage.current(self.agent_id, key)
        had_value = existing is not None and existing["value"] is not None
        return self.storage.append(
            agent_id=self.agent_id,
            operation="update" if had_value else "write",
            key=key,
            old_value=_decode(existing["value"]) if had_value else None,
            new_value=value,
            provenance=provenance,
        )

    def delete(self, key: str, *, provenance: str | None = None) -> Operation:
        """Forget a belief. The fact that it was forgotten is itself recorded."""
        existing = self.storage.current(self.agent_id, key)
        return self.storage.append(
            agent_id=self.agent_id,
            operation="delete",
            key=key,
            old_value=_decode(existing["value"]) if existing else None,
            new_value=None,
            provenance=provenance,
        )

    def decay(self, key: str, *, provenance: str | None = None) -> Operation:
        """Like delete, but attributed to a decay policy rather than a decision."""
        existing = self.storage.current(self.agent_id, key)
        return self.storage.append(
            agent_id=self.agent_id,
            operation="decay",
            key=key,
            old_value=_decode(existing["value"]) if existing else None,
            new_value=None,
            provenance=provenance or "decay policy",
        )

    # -- reads ---------------------------------------------------------

    def read(self, key: str, *, provenance: str | None = None) -> Any:
        """Return the current belief and log that it was consulted.

        The read log is what lets you prove the agent *acted on* a stale
        value, not merely that a stale value existed.
        """
        row = self.storage.current(self.agent_id, key)
        value = _decode(row["value"]) if row else None
        self.storage.log_read(
            agent_id=self.agent_id,
            key=key,
            op_id_seen=row["op_id"] if row else None,
            provenance=provenance,
        )
        return value

    def reads_before(
        self,
        when: str | datetime,
        *,
        key: str | None = None,
        limit: int = 50,
    ) -> list[ReadRecord]:
        """Every memory the agent consulted before this moment, newest first.

        The debugging primitive: an agent made a bad call at 10:05 -- what
        had it just read, and how old was it?

        >>> ledger.reads_before("2026-06-15T10:05:00+00:00")[0].age_at_read
        datetime.timedelta(days=94)
        """
        rows = self.storage.reads_before(
            self.agent_id, _to_iso(when), key=key, limit=limit
        )
        return [self._to_read_record(r) for r in rows]

    @staticmethod
    def _to_read_record(row: Any) -> ReadRecord:
        """Build a ReadRecord, computing how stale the value was when read."""
        written_at = row["written_at"]
        age = None
        if written_at is not None:
            age = datetime.fromisoformat(row["ts"]) - datetime.fromisoformat(written_at)
        return ReadRecord(
            ts=row["ts"],
            key=row["key"],
            value_seen=_decode(row["value_seen"]),
            written_at=written_at,
            age_at_read=age,
            provenance=row["provenance"],
            op_id_seen=row["op_id_seen"],
        )

    def stale_reads(
        self,
        when: str | datetime,
        *,
        older_than: timedelta,
        limit: int = 50,
    ) -> list[ReadRecord]:
        """Reads where the value was already older than `older_than`.

        >>> ledger.stale_reads(incident_time, older_than=timedelta(days=90))
        [ReadRecord(key='user_employer', age_at_read=..., ...)]
        """
        return [
            r
            for r in self.reads_before(when, limit=limit * 4)
            if r.age_at_read is not None and r.age_at_read > older_than
        ][:limit]
    
    # -- time travel ---------------------------------------------------

    def as_of(self, when: str | datetime) -> dict[str, Any]:
        """Everything this agent believed at that instant."""
        rows = self.storage.as_of(self.agent_id, _to_iso(when))
        return {r["key"]: _decode(r["value"]) for r in rows}

    def history(self, key: str) -> list[dict[str, Any]]:
        """Every operation ever performed on this key, oldest first."""
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "operation": r["operation"],
                "old_value": _decode(r["old_value"]),
                "new_value": _decode(r["new_value"]),
                "provenance": r["provenance"],
            }
            for r in self.storage.history(self.agent_id, key)
        ]

    def snapshot(self) -> dict[str, Any]:
        """Current beliefs."""
        return self.as_of(utcnow_iso())

    # -- integrity -----------------------------------------------------

    def verify(self) -> int:
        """Recompute the chain. Raises ChainIntegrityError if tampered with."""
        return self.storage.verify(self.agent_id)