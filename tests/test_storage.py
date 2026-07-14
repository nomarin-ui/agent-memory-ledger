from __future__ import annotations

import sqlite3

import pytest

from agent_memory_ledger import MemoryLedger, SQLiteStorage
from agent_memory_ledger.hashing import GENESIS_HASH, canonical_json, compute_op_hash
from agent_memory_ledger.storage import ChainIntegrityError


def test_canonical_json_is_key_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_none_stays_none():
    assert canonical_json(None) is None


def test_first_op_chains_from_genesis(storage):
    op = storage.append(agent_id="a", operation="write", key="k", new_value=1)
    assert op.prev_hash == GENESIS_HASH


def test_each_op_chains_to_previous(storage):
    o1 = storage.append(agent_id="a", operation="write", key="k", new_value=1)
    o2 = storage.append(agent_id="a", operation="update", key="k", new_value=2)
    assert o2.prev_hash == o1.op_hash


def test_chains_are_per_agent(storage):
    storage.append(agent_id="a", operation="write", key="k", new_value=1)
    ob = storage.append(agent_id="b", operation="write", key="k", new_value=1)
    assert ob.prev_hash == GENESIS_HASH   # agent b starts its own chain


def test_verify_passes_on_clean_chain(storage):
    for i in range(5):
        storage.append(agent_id="a", operation="write", key=f"k{i}", new_value=i)
    assert storage.verify("a") == 5


def test_verify_detects_modified_value(storage, db_path):
    """The whole point: someone edits history, we catch it."""
    storage.append(agent_id="a", operation="write", key="salary", new_value=150000)
    storage.append(agent_id="a", operation="write", key="employer", new_value="Acme")

    # tamper directly, behind the API's back
    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE memory_operations SET new_value = '999999' WHERE id = 1")
    raw.commit()
    raw.close()

    with pytest.raises(ChainIntegrityError, match="op_hash mismatch"):
        storage.verify("a")


def test_verify_detects_deleted_row(storage, db_path):
    for i in range(3):
        storage.append(agent_id="a", operation="write", key=f"k{i}", new_value=i)

    raw = sqlite3.connect(db_path)
    raw.execute("DELETE FROM memory_operations WHERE id = 2")
    raw.commit()
    raw.close()

    with pytest.raises(ChainIntegrityError, match="prev_hash mismatch"):
        storage.verify("a")


def test_verify_detects_modified_provenance(storage, db_path):
    """Provenance is in the hash -- you can't rewrite *why* something happened."""
    storage.append(agent_id="a", operation="write", key="k",
                   new_value=1, provenance="user input")

    raw = sqlite3.connect(db_path)
    raw.execute("UPDATE memory_operations SET provenance = 'agent inference'")
    raw.commit()
    raw.close()

    with pytest.raises(ChainIntegrityError):
        storage.verify("a")


def test_hash_is_deterministic():
    kwargs = dict(
        prev_hash=GENESIS_HASH, ts="2026-01-01T00:00:00+00:00", agent_id="a",
        operation="write", key="k", old_value=None, new_value='"v"',
        provenance="p",
    )
    assert compute_op_hash(**kwargs) == compute_op_hash(**kwargs)


def test_hash_changes_when_any_field_changes():
    base = dict(
        prev_hash=GENESIS_HASH, ts="2026-01-01T00:00:00+00:00", agent_id="a",
        operation="write", key="k", old_value=None, new_value='"v"',
        provenance="p",
    )
    h = compute_op_hash(**base)
    for field, altered in [
        ("agent_id", "b"), ("operation", "delete"), ("key", "k2"),
        ("new_value", '"w"'), ("provenance", "q"),
    ]:
        assert compute_op_hash(**{**base, field: altered}) != h, field


def test_field_splice_does_not_collide():
    """NUL separator: moving a char across a field boundary must change the hash."""
    a = compute_op_hash(prev_hash=GENESIS_HASH, ts="t", agent_id="ab",
                        operation="write", key="c", old_value=None,
                        new_value=None, provenance=None)
    b = compute_op_hash(prev_hash=GENESIS_HASH, ts="t", agent_id="a",
                        operation="write", key="bc", old_value=None,
                        new_value=None, provenance=None)
    assert a != b


def test_read_log_points_at_op_seen(ledger, storage):
    ledger.write("employer", "Acme")
    ledger.read("employer")

    rows = ledger.storage._conn.execute(
        "SELECT op_id_seen FROM memory_reads"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["op_id_seen"] is not None


def test_read_miss_logs_null_op(ledger):
    ledger.read("never_set")
    rows = ledger.storage._conn.execute(
        "SELECT op_id_seen FROM memory_reads"
    ).fetchall()
    assert rows[0]["op_id_seen"] is None


def test_reads_are_not_in_the_chain(ledger):
    ledger.write("employer", "Acme")
    ledger.read("employer")
    ledger.read("employer")
    assert ledger.verify() == 1     # 1 write. reads don't extend the chain.


def test_snapshots_are_pure_derivation(storage, db_path):
    """Drop the cache, replay the log, get identical state back."""
    storage.append(agent_id="a", operation="write", key="k", new_value=1)
    storage.append(agent_id="a", operation="update", key="k", new_value=2)
    storage.append(agent_id="a", operation="write", key="j", new_value="x")
    storage.append(agent_id="a", operation="delete", key="j")

    before = [dict(r) for r in storage.as_of("a", "9999")]
    storage.rebuild_snapshots("a")
    after = [dict(r) for r in storage.as_of("a", "9999")]
    assert before == after


def test_snapshot_rebuild_after_cache_wipe(storage):
    storage.append(agent_id="a", operation="write", key="k", new_value=1)
    storage._conn.execute("DELETE FROM memory_snapshots")
    assert storage.as_of("a", "9999") == []
    storage.rebuild_snapshots("a")
    assert len(storage.as_of("a", "9999")) == 1