# Agent Memory Ledger

**An append-only, tamper-evident ledger for LLM agent memory.**
When an agent makes a bad call, find out what it believed — and why.

---

## The problem

An agent wrote `user_employer: "Acme Corp"` on January 1.

On April 5 it read that memory and sent an email to the user at Acme.

The user left Acme in March. The agent was never told.

Your memory backend shows `user_employer = "Acme Corp"`. That is all it shows. There is no
record of when the agent learned it, where it came from, whether it was ever revisited, or
whether the agent even read it before it acted. The bug is undebuggable, because there is
nothing to debug against.

## What the ledger gives you

```text
  2. WAS ANY OF IT STALE?
──────────────────────────────────────────────────────────────────

     ok     user_seniority    43 days old at read
  ⚠  STALE  user_employer     94 days old at read

  Two beliefs, read in the same instant. One is fine -- seniority
  was refreshed in February. The other had not been touched since
  January, and the world had moved on without telling the agent.
```

Two memories, consulted at the same moment. One is fine. One is three months stale and about
to cause an incident. The ledger tells them apart.

Run it yourself:

```bash
python examples/stale_belief_incident.py
```

## Quick start

```bash
pip install agent-memory-ledger
aml --db demo.db --agent demo-agent demo
aml --db demo.db --agent demo-agent incident 2026-04-05T10:05:00+00:00
```

The third command answers the question you actually have at 2am — *why did the
agent believe that?*

```
Incident window: up to 2026-04-05T10:05:00

  WHAT IT READ
      ok   user_seniority         senior engineer        43d old at read
           why: drafting outreach email -- need seniority
    STALE  user_employer          Acme Corp              94d old at read
           why: drafting outreach email -- need current employer

  WHAT IT BELIEVED
    user_employer          = Acme Corp
    user_seniority         = senior engineer

  Chain verified: 3 operations.
```

The agent read `user_employer` one minute before it sent the email. The value
was 94 days old. It was written once, in January, from a single onboarding
message, and never checked again — the user changed jobs in March and nobody
told the agent.

A memory backend can only tell you the value is `Acme Corp`. The ledger tells
you when the agent read it, how stale it was at that instant, where it came
from, and — via the hash chain — that nobody edited the record afterward to
make the incident look better than it was.

The full scenario, with provenance and belief reconstruction, is in
[`examples/stale_belief_incident.py`](examples/stale_belief_incident.py).
See [ARCHITECTURE.md](ARCHITECTURE.md) for how it works.

## API

| Call | Answers |
|---|---|
| `write(key, value, provenance=)` | Record a belief, and why |
| `read(key, provenance=)` | Consult a belief — and log that you did |
| `as_of(when)` | What did the agent believe at that instant? |
| `history(key)` | Every change to this belief, with provenance |
| `reads_before(when)` | What did the agent consult before it acted? |
| `stale_reads(when, older_than=)` | Which of those were already out of date? |
| `verify()` | Has the record been tampered with? |
| `EntityResolver.resolve(agent, key)` | Are `Acme Corp` and `ACME` the same company? |

## How it works

- **Append-only.** Nothing is ever updated or deleted. A `delete` records a tombstone — the
  fact that the agent *forgot* something is itself a fact worth keeping.
- **Hash-chained.** Every operation is `SHA256(prev_hash + payload)`. Edit a value, delete a
  row, or rewrite a provenance string, and `verify()` fails. The record is tamper-evident.
- **Bitemporal.** Time-travel is a single indexed query, not a replay from genesis.
- **Reads are separate.** Reads are 10–100× write volume and don't change belief, so they
  live outside the chain — but each one points at the exact ledger row the agent saw.
- **Merges are beliefs too.** When the entity resolver decides `Acme` and `ACME` are the same
  company, that decision goes in the ledger with its confidence and method. *Why* the agent
  thinks two things are the same is itself auditable.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design in depth.

## Status

**v0.x — early, but real.** 81 tests passing. Not yet on PyPI.

Install from source:

```bash
git clone https://github.com/nomarin-ui/agent-memory-ledger
cd agent-memory-ledger
pip install -e ".[dev]"
pytest
```

## Roadmap

- [ ] LangChain integration — wrap your existing memory, change nothing else
- [ ] CLI for time-travel queries
- [ ] Postgres adapter
- [ ] PyPI release (v0.1.0)

## Feedback

If you run agents in production and have ever asked "why did it think that?" — I would like
to hear from you. Open an issue.

## License

MIT
