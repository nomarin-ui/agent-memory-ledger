from __future__ import annotations

import time

import pytest

from agent_memory_ledger import MemoryLedger
from agent_memory_ledger.storage import utcnow_iso


def test_write_then_snapshot(ledger):
    ledger.write("employer", "Acme")
    assert ledger.snapshot() == {"employer": "Acme"}


def test_read_returns_current_value(ledger):
    ledger.write("employer", "Acme")
    assert ledger.read("employer") == "Acme"


def test_read_missing_key_returns_none(ledger):
    assert ledger.read("never_set") is None


def test_second_write_is_an_update(ledger):
    ledger.write("employer", "Acme")
    op = ledger.write("employer", "Globex")
    assert op.operation == "update"
    assert op.old_value == "Acme"
    assert op.new_value == "Globex"


def test_first_write_is_a_write(ledger):
    op = ledger.write("employer", "Acme")
    assert op.operation == "write"
    assert op.old_value is None


def test_as_of_returns_past_belief(ledger):
    ledger.write("employer", "Acme")
    time.sleep(0.01)
    mid = utcnow_iso()
    time.sleep(0.01)
    ledger.write("employer", "Globex")

    assert ledger.as_of(mid) == {"employer": "Acme"}
    assert ledger.snapshot() == {"employer": "Globex"}


def test_as_of_before_any_write_is_empty(ledger):
    before = utcnow_iso()
    time.sleep(0.01)
    ledger.write("employer", "Acme")
    assert ledger.as_of(before) == {}


def test_delete_hides_key_from_snapshot(ledger):
    ledger.write("employer", "Acme")
    ledger.delete("employer")
    assert ledger.snapshot() == {}


def test_delete_preserves_past_belief(ledger):
    """Forgetting is not the same as never having known."""
    ledger.write("employer", "Acme")
    time.sleep(0.01)
    mid = utcnow_iso()
    time.sleep(0.01)
    ledger.delete("employer")

    assert ledger.snapshot() == {}
    assert ledger.as_of(mid) == {"employer": "Acme"}


def test_delete_is_recorded_in_history(ledger):
    ledger.write("employer", "Acme")
    ledger.delete("employer", provenance="user asked to forget")
    ops = [h["operation"] for h in ledger.history("employer")]
    assert ops == ["write", "delete"]


def test_write_after_delete_is_a_write_not_update(ledger):
    ledger.write("employer", "Acme")
    ledger.delete("employer")
    op = ledger.write("employer", "Initech")
    assert op.operation == "write"
    assert op.old_value is None


def test_decay_records_decay_operation(ledger):
    ledger.write("employer", "Acme")
    op = ledger.decay("employer")
    assert op.operation == "decay"
    assert ledger.snapshot() == {}


def test_history_is_chronological(ledger):
    ledger.write("employer", "Acme")
    ledger.write("employer", "Globex")
    ledger.write("employer", "Initech")
    values = [h["new_value"] for h in ledger.history("employer")]
    assert values == ["Acme", "Globex", "Initech"]


def test_history_of_unknown_key_is_empty(ledger):
    assert ledger.history("nope") == []


def test_provenance_is_preserved(ledger):
    ledger.write("employer", "Acme", provenance="user said so")
    assert ledger.history("employer")[0]["provenance"] == "user said so"


def test_complex_values_round_trip(ledger):
    value = {"name": "John", "tags": ["a", "b"], "score": 0.95, "active": True}
    ledger.write("profile", value)
    assert ledger.read("profile") == value


def test_agents_are_isolated(db_path):
    a = MemoryLedger("agent_a", db_path)
    b = MemoryLedger("agent_b", db_path)
    try:
        a.write("employer", "Acme")
        b.write("employer", "Globex")
        assert a.snapshot() == {"employer": "Acme"}
        assert b.snapshot() == {"employer": "Globex"}
    finally:
        a.close()
        b.close()