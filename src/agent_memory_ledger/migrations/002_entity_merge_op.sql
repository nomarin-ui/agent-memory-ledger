CREATE TABLE memory_operations_new (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT    NOT NULL,
  agent_id    TEXT    NOT NULL,
  operation   TEXT    NOT NULL CHECK (operation IN ('write','update','delete','decay','entity_merge')),
  key         TEXT    NOT NULL,
  old_value   TEXT,
  new_value   TEXT,
  provenance  TEXT,
  prev_hash   TEXT    NOT NULL,
  op_hash     TEXT    NOT NULL UNIQUE,
  created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

INSERT INTO memory_operations_new
  SELECT id, ts, agent_id, operation, key, old_value, new_value,
         provenance, prev_hash, op_hash, created_at
  FROM memory_operations;

DROP TABLE memory_operations;

ALTER TABLE memory_operations_new RENAME TO memory_operations;

CREATE INDEX ix_ops_agent      ON memory_operations (agent_id, id);

CREATE INDEX ix_ops_agent_key  ON memory_operations (agent_id, key, ts);
