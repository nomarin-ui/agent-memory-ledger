"""SQLite storage adapter for the ledger."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .hashing import GENESIS_HASH, canonical_json, compute_op_hash

from .migrations import current_version, migrate

import threading

def utcnow_iso() -> str:
    """ISO8601 UTC with microseconds. Lexicographically sortable."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class Operation:
    id: int
    ts: str
    agent_id: str
    operation: str
    key: str
    old_value: Any
    new_value: Any
    provenance: str | None
    prev_hash: str
    op_hash: str


class ChainIntegrityError(Exception):
    """The hash chain does not verify. The ledger has been tampered with."""


class SQLiteStorage:
    def __init__(self, path: str | Path = "ledger.db") -> None:
        self.path = str(path)
        # check_same_thread=False: agent frameworks (LangGraph, etc.) run tool
        # calls on a thread pool. The connection must cross threads.
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Disabling the thread check does not make sqlite3 thread-safe. This
        # lock serializes appends so two threads cannot read the same chain
        # tip and fork the chain.
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        """Bring the database up to the latest schema version."""
        migrate(self._conn)

    @property
    def schema_version(self) -> int:
        """Highest migration applied to this database."""
        return current_version(self._conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteStorage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- ledger writes -------------------------------------------------

    def _tip_hash(self, agent_id: str) -> str:
        row = self._conn.execute(
            "SELECT op_hash FROM memory_operations WHERE agent_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        return row["op_hash"] if row else GENESIS_HASH

    def append(
        self,
        *,
        agent_id: str,
        operation: str,
        key: str,
        old_value: Any = None,
        new_value: Any = None,
        provenance: str | None = None,
        ts: str | None = None,
    ) -> Operation:
        """Append one operation and extend the hash chain.

        Serialized: reading the chain tip and writing the new link must be
        atomic, or concurrent writers fork the chain.
        """
        ts = ts or utcnow_iso()
        old_json = canonical_json(old_value)
        new_json = canonical_json(new_value)

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                prev_hash = self._tip_hash(agent_id)
                op_hash = compute_op_hash(
                    prev_hash=prev_hash,
                    ts=ts,
                    agent_id=agent_id,
                    operation=operation,
                    key=key,
                    old_value=old_json,
                    new_value=new_json,
                    provenance=provenance,
                )
                cur = self._conn.execute(
                    "INSERT INTO memory_operations "
                    "(ts, agent_id, operation, key, old_value, new_value, "
                    " provenance, prev_hash, op_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ts, agent_id, operation, key, old_json, new_json,
                     provenance, prev_hash, op_hash),
                )
                op_id = int(cur.lastrowid)
                self._apply_snapshot(
                    agent_id=agent_id, key=key, operation=operation,
                    new_json=new_json, ts=ts, op_id=op_id,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        return Operation(
            id=op_id, ts=ts, agent_id=agent_id, operation=operation, key=key,
            old_value=old_value, new_value=new_value, provenance=provenance,
            prev_hash=prev_hash, op_hash=op_hash,
        )

    def _apply_snapshot(
        self, *, agent_id: str, key: str, operation: str,
        new_json: str | None, ts: str, op_id: int,
    ) -> None:
        """Close the open snapshot row, open a new one.

        'delete' opens a tombstone (value IS NULL) rather than closing
        without replacement -- so as_of() can distinguish 'never existed'
        from 'was explicitly forgotten'. That distinction is the product.
        """
        self._conn.execute(
            "UPDATE memory_snapshots SET valid_to = ? "
            "WHERE agent_id = ? AND key = ? AND valid_to IS NULL",
            (ts, agent_id, key),
        )
        value = None if operation == "delete" else new_json
        self._conn.execute(
            "INSERT INTO memory_snapshots "
            "(agent_id, key, value, valid_from, valid_to, op_id) "
            "VALUES (?, ?, ?, ?, NULL, ?)",
            (agent_id, key, value, ts, op_id),
        )

    def log_read(
        self, *, agent_id: str, key: str, op_id_seen: int | None,
        provenance: str | None = None, ts: str | None = None,
    ) -> None:
        """Unchained. Reads don't mutate belief, so they don't need tamper-evidence."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO memory_reads (ts, agent_id, key, op_id_seen, provenance) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts or utcnow_iso(), agent_id, key, op_id_seen, provenance),
            )

    # -- queries -------------------------------------------------------

    def current(self, agent_id: str, key: str) -> sqlite3.Row | None:
        """The agent's present belief about this key, or None."""
        return self._conn.execute(
            "SELECT value, op_id, valid_from FROM memory_snapshots "
            "WHERE agent_id = ? AND key = ? AND valid_to IS NULL "
            "ORDER BY op_id DESC LIMIT 1",
            (agent_id, key),
        ).fetchone()

    def as_of(self, agent_id: str, at: str) -> list[sqlite3.Row]:
        """Every belief held by this agent at instant `at`. One indexed scan."""
        return self._conn.execute(
            "SELECT key, value, valid_from, op_id FROM memory_snapshots s "
            "WHERE agent_id = ? AND valid_from <= ? "
            "  AND (valid_to IS NULL OR valid_to > ?) "
            "  AND value IS NOT NULL "
            "  AND op_id = (SELECT MAX(op_id) FROM memory_snapshots s2 "
            "               WHERE s2.agent_id = s.agent_id AND s2.key = s.key "
            "                 AND s2.valid_from <= ? "
            "                 AND (s2.valid_to IS NULL OR s2.valid_to > ?)) "
            "ORDER BY key",
            (agent_id, at, at, at, at),
        ).fetchall()

    def history(self, agent_id: str, key: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM memory_operations "
            "WHERE agent_id = ? AND key = ? ORDER BY id",
            (agent_id, key),
        ).fetchall()

    def iter_ops(self, agent_id: str) -> Iterator[sqlite3.Row]:
        yield from self._conn.execute(
            "SELECT * FROM memory_operations WHERE agent_id = ? ORDER BY id",
            (agent_id,),
        )

    # -- integrity -----------------------------------------------------

    def verify(self, agent_id: str) -> int:
        """Recompute the chain from genesis. Raises on the first bad link.

        Returns the number of operations verified.
        """
        expected_prev = GENESIS_HASH
        count = 0
        for row in self.iter_ops(agent_id):
            if row["prev_hash"] != expected_prev:
                raise ChainIntegrityError(
                    f"op {row['id']}: prev_hash mismatch "
                    f"(chain broken -- a row was deleted or reordered)"
                )
            recomputed = compute_op_hash(
                prev_hash=row["prev_hash"],
                ts=row["ts"],
                agent_id=row["agent_id"],
                operation=row["operation"],
                key=row["key"],
                old_value=row["old_value"],
                new_value=row["new_value"],
                provenance=row["provenance"],
            )
            if recomputed != row["op_hash"]:
                raise ChainIntegrityError(
                    f"op {row['id']}: op_hash mismatch "
                    f"(row contents were modified after the fact)"
                )
            expected_prev = row["op_hash"]
            count += 1
        return count

    def rebuild_snapshots(self, agent_id: str) -> int:
        """Drop and replay. Proves snapshots are pure derivation, nothing more."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "DELETE FROM memory_snapshots WHERE agent_id = ?", (agent_id,)
                )
                n = 0
                for row in list(self.iter_ops(agent_id)):
                    self._apply_snapshot(
                        agent_id=row["agent_id"], key=row["key"],
                        operation=row["operation"], new_json=row["new_value"],
                        ts=row["ts"], op_id=row["id"],
                    )
                    n += 1
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return n