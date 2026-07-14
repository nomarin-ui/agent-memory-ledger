from __future__ import annotations

import pytest
from click.testing import CliRunner

from agent_memory_ledger import MemoryLedger
from agent_memory_ledger.cli import main


@pytest.fixture
def seeded(db_path):
    lg = MemoryLedger("bot", db_path)
    lg.write("employer", "Acme Corp", provenance="user said so")
    lg.write("employer", "Globex", provenance="user changed jobs")
    lg.read("employer", provenance="drafting email")
    lg.close()
    return str(db_path)


@pytest.fixture
def run(seeded):
    def _run(*args: str):
        return CliRunner().invoke(main, ["--db", seeded, "--agent", "bot", *args])
    return _run


def test_verify_reports_intact_chain(run):
    result = run("verify")
    assert result.exit_code == 0
    assert "Chain intact" in result.output


def test_history_shows_provenance(run):
    result = run("history", "employer")
    assert result.exit_code == 0
    assert "user changed jobs" in result.output
    assert "Acme Corp" in result.output


def test_as_of_shows_current_belief(run):
    result = run("as-of", "now")
    assert result.exit_code == 0
    assert "Globex" in result.output


def test_reads_shows_what_was_consulted(run):
    result = run("reads")
    assert result.exit_code == 0
    assert "employer" in result.output
    assert "drafting email" in result.output


def test_incident_is_the_whole_story(run):
    result = run("incident", "now")
    assert result.exit_code == 0
    assert "WHAT IT READ" in result.output
    assert "WHAT IT BELIEVED" in result.output
    assert "Chain verified" in result.output


def test_incident_with_no_reads_says_so(db_path):
    lg = MemoryLedger("quiet", db_path)
    lg.write("k", "v")
    lg.close()

    result = CliRunner().invoke(
        main, ["--db", str(db_path), "--agent", "quiet", "incident"]
    )
    assert "read nothing" in result.output


def test_history_of_unknown_key(run):
    result = run("history", "nope")
    assert "No history" in result.output


def test_resolve_is_dry_run_by_default(db_path):
    """A merge is permanent. It must not happen because someone was exploring."""
    lg = MemoryLedger("bot", db_path)
    lg.write("employer", "Acme Corp")
    lg.write("employer", "ACME Corporation")
    lg.close()

    result = CliRunner().invoke(
        main, ["--db", str(db_path), "--agent", "bot", "resolve", "employer"]
    )
    assert "Dry run" in result.output

    lg = MemoryLedger("bot", db_path)
    merges = lg.storage._conn.execute(
        "SELECT COUNT(*) AS n FROM memory_operations WHERE operation='entity_merge'"
    ).fetchone()
    lg.close()
    assert merges["n"] == 0        # nothing was recorded


def test_resolve_apply_records_the_merge(db_path):
    lg = MemoryLedger("bot", db_path)
    lg.write("employer", "Acme Corp")
    lg.write("employer", "ACME Corporation")
    lg.close()

    result = CliRunner().invoke(
        main, ["--db", str(db_path), "--agent", "bot", "resolve", "employer", "--apply"]
    )
    assert "merged" in result.output


def test_relative_time_parsing(run):
    """Nobody types an ISO timestamp at 2am."""
    assert run("incident", "2h").exit_code == 0
    assert run("as-of", "30d").exit_code == 0