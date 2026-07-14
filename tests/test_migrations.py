from __future__ import annotations

import sqlite3

import pytest

from agent_memory_ledger import SQLiteStorage
from agent_memory_ledger.migrations import (
    MigrationError,
    current_version,
    discover,
    migrate,
)


def test_discover_finds_initial_migration():
    found = discover()
    assert found[0].version == 1
    assert found[0].name == "initial_schema"


def test_versions_are_contiguous():
    versions = [m.version for m in discover()]
    assert versions == list(range(1, len(versions) + 1))


def test_empty_db_is_version_zero(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db", isolation_level=None)
    assert current_version(conn) == 0
    conn.close()


def test_migrate_applies_and_records(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db", isolation_level=None)
    applied = migrate(conn)
    assert applied == [1, 2]
    assert current_version(conn) == 2
    conn.close()


def test_migrate_is_idempotent(tmp_path):
    """Running twice must not reapply. This is the whole point."""
    conn = sqlite3.connect(tmp_path / "x.db", isolation_level=None)
    assert migrate(conn) == [1, 2]
    assert migrate(conn) == []      # nothing pending the second time
    conn.close()


def test_storage_reports_schema_version(db_path):
    st = SQLiteStorage(db_path)
    assert st.schema_version == 2
    st.close()


def test_bad_filename_rejected(tmp_path):
    (tmp_path / "nope.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="bad migration filename"):
        discover(tmp_path)


def test_noncontiguous_versions_rejected(tmp_path):
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "003_c.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="contiguous"):
        discover(tmp_path)


def test_failed_migration_rolls_back(tmp_path):
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE a (x INT);")
    (tmp_path / "002_bad.sql").write_text("CREATE TABLE b (x INT); SYNTAX ERROR;")
    conn = sqlite3.connect(tmp_path / "x.db", isolation_level=None)

    with pytest.raises(MigrationError, match="002_bad"):
        migrate(conn, tmp_path)

    assert current_version(conn) == 1        # stopped at last good version
    conn.close()


def test_migrations_reach_working_schema(db_path):
    """Drop everything, migrate from scratch, ledger still works."""
    from agent_memory_ledger import MemoryLedger

    lg = MemoryLedger("a", db_path)
    lg.write("k", "v")
    assert lg.snapshot() == {"k": "v"}
    assert lg.verify() == 1
    lg.close()

def test_migration_002_preserves_the_chain(db_path):
    """Rebuilding the ops table must not break tamper-evidence."""
    from agent_memory_ledger import MemoryLedger

    lg = MemoryLedger("a", db_path)
    lg.write("k", "v1")
    lg.write("k", "v2")
    assert lg.verify() == 2          # chain survives the table rebuild
    assert lg.storage.schema_version == 2
    lg.close()


def test_entity_merge_operation_is_allowed(storage):
    """002 widened the CHECK constraint."""
    op = storage.append(agent_id="a", operation="entity_merge",
                        key="entity_acme", new_value={"aliases": ["A", "B"]})
    assert op.operation == "entity_merge"    