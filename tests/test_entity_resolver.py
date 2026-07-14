from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_memory_ledger.entity_resolver import (
    EntityResolver,
    EscalationBudgetExceeded,
    normalize,
)
from agent_memory_ledger.storage import utcnow_iso

FAR_FUTURE = "9999-12-31T23:59:59+00:00"


@pytest.fixture
def resolver(storage):
    return EntityResolver(storage)


def _seed(storage, agent, key, values):
    for i, v in enumerate(values):
        storage.append(
            agent_id=agent, operation="write" if i == 0 else "update",
            key=key, new_value=v,
        )


# -- normalization ----------------------------------------------------

def test_normalize_strips_corporate_suffix():
    assert normalize("Acme Corp.") == "acme"
    assert normalize("ACME Incorporated") == "acme"


def test_normalize_extracts_email_local_part():
    assert normalize("john.smith@acme.com") == "john smith"


def test_normalize_is_case_insensitive():
    assert normalize("ACME") == normalize("acme")


# -- resolution -------------------------------------------------------

def test_single_value_yields_no_merge(resolver, storage):
    _seed(storage, "a", "employer", ["Acme Corp"])
    assert resolver.resolve("a", "employer") == []


def test_identical_values_yield_no_merge(resolver, storage):
    """Same string twice isn't two entities."""
    _seed(storage, "a", "employer", ["Acme Corp", "Acme Corp"])
    assert resolver.resolve("a", "employer") == []


def test_fuzzy_match_merges(resolver, storage):
    _seed(storage, "a", "employer", ["Acme Corp", "ACME Corporation"])
    merges = resolver.resolve("a", "employer")

    assert len(merges) == 1
    assert set(merges[0].aliases) == {"Acme Corp", "ACME Corporation"}
    assert merges[0].method == "fuzzy"


def test_unrelated_values_do_not_merge(resolver, storage):
    _seed(storage, "a", "employer", ["Acme Corp", "Globex Industries"])
    assert resolver.resolve("a", "employer") == []


def test_transitive_grouping(resolver, storage):
    """A=B and B=C means one group of three, not two groups of two."""
    _seed(storage, "a", "employer", ["Acme Corp", "ACME Corp", "acme corp"])
    merges = resolver.resolve("a", "employer")

    assert len(merges) == 1
    assert len(merges[0].aliases) == 3


def test_group_confidence_is_weakest_link(resolver, storage):
    """One strong pair must not drag a weak member into the group at high confidence."""
    _seed(storage, "a", "employer", ["Acme Corp", "Acme Corporation"])
    merges = resolver.resolve("a", "employer")
    assert 0.0 < merges[0].confidence <= 1.0


def test_no_client_means_no_api_call(storage):
    """Free tier: fuzzy-only resolver must never touch the network."""
    r = EntityResolver(storage, client=None)
    _seed(storage, "a", "employer", ["Acme Corp", "ACME Corporation"])
    assert len(r.resolve("a", "employer")) == 1     # works without a client


# -- merges are ledger operations -------------------------------------

def test_merge_is_recorded_in_the_ledger(resolver, storage):
    _seed(storage, "a", "employer", ["Acme Corp", "ACME Corporation"])
    resolver.resolve("a", "employer")

    rows = storage._conn.execute(
        "SELECT * FROM memory_operations WHERE operation = 'entity_merge'"
    ).fetchall()
    assert len(rows) == 1
    assert "fuzzy" in rows[0]["provenance"]


def test_merge_extends_the_hash_chain(resolver, storage):
    """A merge is a belief. It must be tamper-evident like any other."""
    _seed(storage, "a", "employer", ["Acme Corp", "ACME Corporation"])
    resolver.resolve("a", "employer")
    assert storage.verify("a") == 3      # 2 writes + 1 merge, chain intact


def test_merge_provenance_explains_itself(resolver, storage):
    _seed(storage, "a", "employer", ["Acme Corp", "ACME Corporation"])
    merges = resolver.resolve("a", "employer")

    row = storage._conn.execute(
        "SELECT provenance FROM memory_operations WHERE operation = 'entity_merge'"
    ).fetchone()
    assert "employer" in row["provenance"]
    assert "confidence" in row["provenance"]


# -- time-boundedness -------------------------------------------------

def test_merge_is_invisible_before_it_happened(resolver, storage):
    """The whole point: a day-40 merge must not pollute a day-20 query."""
    _seed(storage, "a", "employer", ["Acme Corp", "ACME Corporation"])
    before = utcnow_iso()
    merges = resolver.resolve("a", "employer")

    assert resolver.aliases_for(merges[0].canonical_id, before) == []
    assert len(resolver.aliases_for(merges[0].canonical_id, FAR_FUTURE)) == 2


# -- cost control -----------------------------------------------------

def test_escalation_budget_raises(storage):
    """Fail loud rather than silently spend."""
    r = EntityResolver(storage, escalate_above=0.0, auto_merge_at=100.0,
                       max_escalations=2)
    _seed(storage, "a", "employer", ["Acme A", "Acme B", "Acme C", "Acme D"])

    with pytest.raises(EscalationBudgetExceeded):
        r.resolve("a", "employer")


def test_stats_track_escalation_rate(resolver, storage):
    _seed(storage, "a", "employer", ["Acme Corp", "ACME Corporation"])
    resolver.resolve("a", "employer")
    assert resolver.stats.compared > 0
    assert 0.0 <= resolver.stats.escalation_rate <= 1.0


# -- claude path (mocked -- no network in tests) ----------------------

def _mock_client(payload):
    client = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = json.dumps(payload)
    client.messages.create.return_value = MagicMock(content=[block])
    return client


def test_claude_merges_ambiguous_pair(storage):
    client = _mock_client([
        {"index": 0, "same_entity": True, "confidence": 0.93, "reason": "same firm"}
    ])
    r = EntityResolver(storage, client=client, auto_merge_at=99.0, escalate_above=10.0)
    _seed(storage, "a", "employer", ["Acme Corp", "Acme Holdings"])

    merges = r.resolve("a", "employer")
    assert len(merges) == 1
    assert merges[0].method == "claude"
    client.messages.create.assert_called_once()      # one batched call, not two


def test_claude_rejection_yields_no_merge(storage):
    client = _mock_client([
        {"index": 0, "same_entity": False, "confidence": 0.88, "reason": "different firms"}
    ])
    r = EntityResolver(storage, client=client, auto_merge_at=99.0, escalate_above=10.0)
    _seed(storage, "a", "employer", ["Acme Corp", "Acme Holdings"])

    assert r.resolve("a", "employer") == []


def test_malformed_claude_response_merges_nothing(storage):
    """Bad JSON must not merge anything. Safe default."""
    client = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = "I'm not sure, let me think about it..."
    client.messages.create.return_value = MagicMock(content=[block])

    r = EntityResolver(storage, client=client, auto_merge_at=99.0, escalate_above=10.0)
    _seed(storage, "a", "employer", ["Acme Corp", "Acme Holdings"])
    assert r.resolve("a", "employer") == []


# -- idempotency ------------------------------------------------------

def test_resolve_twice_is_stable(resolver, storage):
    _seed(storage, "a", "employer", ["Acme Corp", "ACME Corporation"])
    first = resolver.resolve("a", "employer")
    second = resolver.resolve("a", "employer")

    assert first[0].canonical_id == second[0].canonical_id
    assert first[0].aliases == second[0].aliases