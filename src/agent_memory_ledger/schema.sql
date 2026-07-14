PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- THE LEDGER. Append-only, hash-chained. Beliefs only.
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_operations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT    NOT NULL,
  agent_id    TEXT    NOT NULL,
  operation   TEXT    NOT NULL CHECK (operation IN ('write','update','delete','decay')),
  key         TEXT    NOT NULL,
  old_value   TEXT,
  new_value   TEXT,
  provenance  TEXT,
  prev_hash   TEXT    NOT NULL,
  op_hash     TEXT    NOT NULL UNIQUE,
  created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS ix_ops_agent      ON memory_operations (agent_id, id);
CREATE INDEX IF NOT EXISTS ix_ops_agent_key  ON memory_operations (agent_id, key, ts);

-- ============================================================
-- READS. High-volume, unchained. Points at the ledger row the
-- agent actually saw -> "it read a stale value" is one join.
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_reads (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT    NOT NULL,
  agent_id    TEXT    NOT NULL,
  key         TEXT    NOT NULL,
  op_id_seen  INTEGER REFERENCES memory_operations(id),
  provenance  TEXT
);

CREATE INDEX IF NOT EXISTS ix_reads_agent_ts ON memory_reads (agent_id, ts);
CREATE INDEX IF NOT EXISTS ix_reads_op       ON memory_reads (op_id_seen);

-- ============================================================
-- SNAPSHOTS. Derived cache. Droppable + rebuildable from the ledger.
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_snapshots (
  agent_id    TEXT    NOT NULL,
  key         TEXT    NOT NULL,
  value       TEXT,
  valid_from  TEXT    NOT NULL,
  valid_to    TEXT,
  op_id       INTEGER NOT NULL REFERENCES memory_operations(id),
  PRIMARY KEY (agent_id, key, valid_from)
);

CREATE INDEX IF NOT EXISTS ix_snap_travel ON memory_snapshots (agent_id, valid_from DESC, valid_to);

-- ============================================================
-- ENTITY ALIASES. Time-bounded.
-- ============================================================
CREATE TABLE IF NOT EXISTS entity_aliases (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_id  TEXT    NOT NULL,
  alias         TEXT    NOT NULL,
  confidence    REAL    NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
  method        TEXT    NOT NULL CHECK (method IN ('exact','fuzzy','claude','manual')),
  valid_from    TEXT    NOT NULL,
  valid_to      TEXT
);

CREATE INDEX IF NOT EXISTS ix_alias_lookup ON entity_aliases (alias, valid_from DESC);

-- ============================================================
-- STALENESS FLAGS. Append-only: keep the history of judgments.
-- ============================================================
CREATE TABLE IF NOT EXISTS staleness_flags (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id        TEXT    NOT NULL,
  key             TEXT    NOT NULL,
  evaluated_at    TEXT    NOT NULL,
  last_verified   TEXT,
  staleness_score REAL    NOT NULL CHECK (staleness_score BETWEEN 0.0 AND 1.0),
  reason          TEXT,
  recommendation  TEXT    CHECK (recommendation IN ('flag','review','safe'))
);

CREATE INDEX IF NOT EXISTS ix_stale_lookup ON staleness_flags (agent_id, key, evaluated_at DESC);
