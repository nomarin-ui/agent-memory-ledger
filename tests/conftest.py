from __future__ import annotations

import pytest

from agent_memory_ledger import MemoryLedger, SQLiteStorage


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def ledger(db_path):
    lg = MemoryLedger("agent_1", db_path)
    yield lg
    lg.close()          # must close before tmp_path cleanup on Windows


@pytest.fixture
def storage(db_path):
    st = SQLiteStorage(db_path)
    yield st
    st.close()