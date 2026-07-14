from __future__ import annotations

from datetime import timedelta

import pytest

from agent_memory_ledger import MemoryLedger
from agent_memory_ledger.storage import utcnow_iso

FAR_FUTURE = "9999-12-31T23:59:59+00:00"


def test_no_reads_returns_empty(ledger):
    ledger.write("employer", "Acme")
    assert ledger.reads_before(FAR_FUTURE) == []


def test_single_read_is_captured(ledger):
    ledger.write("employer", "Acme")
    ledger.read("employer")

    reads = ledger.reads_before(FAR_FUTURE)
    assert len(reads) == 1
    assert reads[0].key == "employer"
    assert reads[0].value_seen == "Acme"


def test_multiple_reads_newest_first(ledger):
    ledger.write("a", 1)
    ledger.write("b", 2)
    ledger.read("a")
    ledger.read("b")

    reads = ledger.reads_before(FAR_FUTURE)
    assert [r.key for r in reads] == ["b", "a"]


def test_reads_after_cutoff_excluded(ledger):
    ledger.write("employer", "Acme")
    ledger.read("employer")
    cutoff = utcnow_iso()
    ledger.read("employer")          # after the cutoff

    assert len(ledger.reads_before(cutoff)) == 1


def test_filter_by_key(ledger):
    ledger.write("a", 1)
    ledger.write("b", 2)
    ledger.read("a")
    ledger.read("b")
    ledger.read("a")

    reads = ledger.reads_before(FAR_FUTURE, key="a")
    assert len(reads) == 2
    assert all(r.key == "a" for r in reads)


def test_read_miss_has_no_value_or_age(ledger):
    ledger.read("never_set")
    r = ledger.reads_before(FAR_FUTURE)[0]
    assert r.value_seen is None
    assert r.written_at is None
    assert r.age_at_read is None
    assert r.op_id_seen is None


def test_age_at_read_is_computed(ledger):
    ledger.storage.append(
        agent_id="agent_1", operation="write", key="employer",
        new_value="Acme", ts="2026-01-01T00:00:00+00:00",
    )
    ledger.storage.log_read(
        agent_id="agent_1", key="employer",
        op_id_seen=1, ts="2026-04-05T00:00:00+00:00",
    )

    r = ledger.reads_before(FAR_FUTURE)[0]
    assert r.age_at_read == timedelta(days=94)


def test_read_sees_value_current_at_read_time(ledger):
    """Not the latest value -- the one the agent actually got."""
    ledger.write("employer", "Acme")
    ledger.read("employer")           # sees Acme
    ledger.write("employer", "Globex")

    assert ledger.reads_before(FAR_FUTURE)[0].value_seen == "Acme"


def test_provenance_on_read_preserved(ledger):
    ledger.write("employer", "Acme")
    ledger.read("employer", provenance="deciding whether to email")
    assert ledger.reads_before(FAR_FUTURE)[0].provenance == "deciding whether to email"


def test_limit_is_respected(ledger):
    ledger.write("k", 1)
    for _ in range(10):
        ledger.read("k")
    assert len(ledger.reads_before(FAR_FUTURE, limit=3)) == 3


def test_stale_reads_flags_old_values(ledger):
    """The demo: the agent acted on a 94-day-old belief."""
    ledger.storage.append(
        agent_id="agent_1", operation="write", key="employer",
        new_value="Acme", ts="2026-01-01T00:00:00+00:00",
    )
    ledger.storage.append(
        agent_id="agent_1", operation="write", key="mood",
        new_value="happy", ts="2026-04-04T00:00:00+00:00",
    )
    ledger.storage.log_read(agent_id="agent_1", key="employer",
                            op_id_seen=1, ts="2026-04-05T00:00:00+00:00")
    ledger.storage.log_read(agent_id="agent_1", key="mood",
                            op_id_seen=2, ts="2026-04-05T00:00:00+00:00")

    stale = ledger.stale_reads(FAR_FUTURE, older_than=timedelta(days=90))
    assert len(stale) == 1
    assert stale[0].key == "employer"


def test_stale_reads_ignores_misses(ledger):
    ledger.read("never_set")
    assert ledger.stale_reads(FAR_FUTURE, older_than=timedelta(seconds=0)) == []


def test_reads_are_agent_scoped(db_path):
    a = MemoryLedger("agent_a", db_path)
    b = MemoryLedger("agent_b", db_path)
    try:
        a.write("k", 1)
        b.write("k", 2)
        a.read("k")
        assert len(a.reads_before(FAR_FUTURE)) == 1
        assert len(b.reads_before(FAR_FUTURE)) == 0
    finally:
        a.close()
        b.close()