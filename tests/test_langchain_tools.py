from __future__ import annotations

import pytest

from agent_memory_ledger.langchain_tools import make_memory_tools

pytest.importorskip("langchain_core")     # skip cleanly if the extra isn't installed


@pytest.fixture
def tools(ledger):
    return {t.name: t for t in make_memory_tools(ledger)}


def test_remember_writes_to_the_ledger(tools, ledger):
    tools["remember"].invoke(
        {"key": "user_employer", "value": "Acme Corp", "why": "user said so"}
    )
    assert ledger.snapshot() == {"user_employer": "Acme Corp"}


def test_remember_records_the_agents_stated_reason(tools, ledger):
    """Provenance is what the agent actually said, not an inference."""
    tools["remember"].invoke(
        {"key": "user_employer", "value": "Acme", "why": "they told me in turn 1"}
    )
    assert ledger.history("user_employer")[0]["provenance"] == "they told me in turn 1"


def test_remember_same_key_twice_is_an_update(tools, ledger):
    tools["remember"].invoke({"key": "employer", "value": "Acme", "why": "onboarding"})
    result = tools["remember"].invoke(
        {"key": "employer", "value": "Globex", "why": "user changed jobs"}
    )
    assert "Updated" in result
    assert "Acme" in result           # agent is told what it overwrote
    assert len(ledger.history("employer")) == 2


def test_recall_returns_the_value(tools, ledger):
    ledger.write("employer", "Acme")
    result = tools["recall"].invoke({"key": "employer", "why": "drafting an email"})
    assert "Acme" in result


def test_recall_of_unknown_key_is_graceful(tools):
    result = tools["recall"].invoke({"key": "nope", "why": "checking"})
    assert "No belief recorded" in result


def test_recall_is_logged_with_its_reason(tools, ledger):
    """The read log is what proves the agent acted on a stale value."""
    ledger.write("employer", "Acme")
    tools["recall"].invoke({"key": "employer", "why": "drafting outreach email"})

    reads = ledger.reads_before("9999-12-31T23:59:59+00:00")
    assert len(reads) == 1
    assert reads[0].provenance == "drafting outreach email"
    assert reads[0].value_seen == "Acme"


def test_recall_miss_is_still_logged(tools, ledger):
    tools["recall"].invoke({"key": "never_set", "why": "checking"})
    reads = ledger.reads_before("9999-12-31T23:59:59+00:00")
    assert len(reads) == 1
    assert reads[0].op_id_seen is None


def test_tool_writes_extend_the_hash_chain(tools, ledger):
    tools["remember"].invoke({"key": "a", "value": "1", "why": "x"})
    tools["remember"].invoke({"key": "b", "value": "2", "why": "y"})
    assert ledger.verify() == 2


def test_both_tools_are_returned(tools):
    assert set(tools) == {"remember", "recall"}