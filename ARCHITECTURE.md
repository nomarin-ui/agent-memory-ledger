@'
# Architecture

The ledger is one table that never changes, plus caches derived from it.
Everything below follows from that.

---

## The ledger

`memory_operations` is append-only. No `UPDATE`, no `DELETE`, ever. Each row is one thing an
agent came to believe, and why:

| column | purpose |
|---|---|
| `ts` | when it happened (ISO8601 UTC, lexicographically sortable) |
| `agent_id` | whose belief |
| `operation` | `write` / `update` / `delete` / `decay` / `entity_merge` |
| `key` | the belief slot, e.g. `user_employer` |
| `old_value` / `new_value` | canonical JSON |
| `provenance` | **why this happened** — the field that makes the rest useful |
| `prev_hash` / `op_hash` | the chain |

`provenance` is the whole point. A log that records *what* changed tells you an agent's memory
is wrong. A log that records *why* it changed tells you how it got that way.

### Delete is a tombstone, not a gap

A `delete` writes a snapshot row with `value IS NULL` rather than removing anything. "The agent
forgot this on June 10" is a different fact from "the agent never knew this," and the difference
matters when you are reconstructing why it behaved the way it did.

---

## The hash chain
op_hash = SHA256(prev_hash ‖ ts ‖ agent_id ‖ operation ‖ key ‖ old ‖ new ‖ provenance)

Fields are joined with a NUL byte. NUL cannot appear in canonical JSON or in an ISO timestamp,
so no field can be forged by shifting characters across a boundary — naive concatenation would
let `agent_id="ab", key="c"` collide with `agent_id="a", key="bc"`.

Chains are **per-agent**. Agent A's first operation chains from genesis regardless of what agent
B has done. This keeps `verify()` scoped and means one agent's corruption doesn't invalidate
another's record.

`verify(agent_id)` recomputes the whole chain from genesis:

- `prev_hash` mismatch → a row was **deleted or reordered**
- `op_hash` mismatch → a row's **contents were modified**

This is tamper-**evident**, not tamper-proof. Someone with write access to the database can
still destroy the record; they cannot quietly alter it and have it still verify. That is the
guarantee, and it is the one that matters for an audit trail.

`BEGIN IMMEDIATE` on append takes the write lock *before* reading the chain tip, so two
concurrent writers cannot both read the same tip and fork the chain.

---

## Time travel

Naively, "what did the agent believe on June 15" means replaying every operation from the
beginning. That is O(n) per query and gets worse forever.

Instead, `memory_snapshots` holds bitemporal closing rows:

```sql
PRIMARY KEY (agent_id, key, op_id)
valid_to IS NULL   -- currently believed
```

Each write closes the previous open row (`valid_to = now`) and opens a new one. A time-travel
query is then a single indexed lookup:

```sql
WHERE agent_id = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)
```

The PK is on `op_id`, not `valid_from`. `op_id` is already unique and monotonic, so two writes
to the same key in the same microsecond cannot collide — a real failure mode that would have
silently dropped rows.

**Snapshots are a cache, not truth.** `rebuild_snapshots()` drops them and replays the log to
identical state, and there is a test asserting exactly that. If the cache and the ledger ever
disagree, the ledger wins.

---

## Reads

Reads live in `memory_reads`, outside the chain. Two reasons:

1. **Volume.** Reads are 10–100× writes. Chaining them would serialize a write lock on every
   memory access.
2. **They don't mutate belief.** Tamper-evidence protects the record of what the agent *thought*.
   A read changes nothing, so it needs no chain.

But each read stores `op_id_seen` — a foreign key to the exact ledger row the agent consulted.
That is what makes the debugging story work:

```python
ledger.reads_before(incident_time)
# -> ReadRecord(key='user_employer', value_seen='Acme Corp',
#               written_at='2026-01-01', age_at_read=timedelta(days=94))
```

`age_at_read` is the difference between when a value was written and when the agent used it.
"The agent acted on a 94-day-old belief" is one query, not an investigation.

---

## Entity resolution

Four stages, cheapest first. Each stage only sees what the previous one could not settle.

| stage | cost | what it does |
|---|---|---|
| normalize | free | lowercase, strip `Corp`/`Inc`/`Ltd`, extract email local parts |
| block | free | bucket by prefix; only compare within buckets |
| fuzzy | free | RapidFuzz `WRatio` |
| Claude | € | only the ambiguous band |

Thresholds: **≥ 92 auto-merge**, **≤ 65 auto-reject**, **66–91 → Claude**.

**92 is deliberately high.** A false merge is far worse than a missed one: merging two distinct
entities means the agent now holds a single confused belief about two different things in the
world. That is a *new* bug, introduced by the tool that claims to find bugs. A missed merge just
means you didn't help. Asymmetric cost, conservative threshold.

### Why not `token_set_ratio`

It scores strict subsets as 100. `Acme` vs `Acme Holdings` → 100 → auto-merged. A subsidiary is
not its parent and a first name is not a person. `WRatio` penalizes the length mismatch, which
is exactly the signal we want. This was caught by a test before it shipped.

### Cost control

`EntityResolver(client=None)` is a fully working fuzzy-only resolver that never touches the
network — the free tier costs nothing to serve, and the test suite never hits the API.

`max_escalations` is a circuit breaker. If blocking degrades or someone loads 50k values, the
ambiguous band explodes and you would otherwise find out on the invoice. It raises instead.

Group confidence is the **weakest link**: if A=B at 0.95 and B=C at 0.70, the group is 0.70.
Taking the max would let one strong pair drag a weak transitive member in, which is precisely
how false merges happen.

### Merges are beliefs

An `entity_merge` is a `memory_operations` row like any other — hash-chained, timestamped,
with provenance. So *why the agent thinks two things are the same* is itself time-travellable
and tamper-evident.

`entity_aliases` is time-bounded (`valid_from` / `valid_to`). A merge decided on day 40 is
invisible to a query about day 20. Without this, time-travel would silently lie.

---

## Migrations

`CREATE TABLE IF NOT EXISTS` is not a migration system. It sets up an empty database and
silently does nothing to an existing one — so the first schema change after release leaves
every user's database wrong, with no way for the code to tell.

Instead: numbered `NNN_name.sql` files, a `schema_migrations` table, applied in order, exactly
once. Each migration is atomic — a failure leaves the database at the last good version, never
half-migrated.

Migrations are executed statement-by-statement rather than via `executescript()`, because
`executescript()` implicitly commits and would break the surrounding transaction.

`002_entity_merge_op.sql` is the first real one: it widens a CHECK constraint, which SQLite
cannot do in place, so it rebuilds the entire operations table. **Every hash carried across
intact** — there is a test asserting `verify()` still passes on pre-migration data.

---

## Known gaps

- `storage._conn` is reached into from `ledger.py` and `entity_resolver.py`. Works now; needs a
  `StorageBackend` interface before the Postgres adapter lands.
- Migration statement splitting is naive on `;`. Fine for the current DDL; will break on a
  trigger body or a semicolon inside a string literal.
- Re-running `resolve()` records a second merge operation. This is intentional — "I checked
  again in June and still think so" is a belief with a timestamp — but it means `resolve()`
  should be called on-demand or on a schedule, never in a loop.
'@ | Out-File -Encoding utf8 ARCHITECTURE.md