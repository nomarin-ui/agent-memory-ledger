"""The stale belief incident.

An agent learns something in January. It acts on it in April. Nobody told it
the world changed in between.

Run:  python examples/stale_belief_incident.py
"""
from __future__ import annotations

import os
from datetime import timedelta

from agent_memory_ledger import MemoryLedger

DB = "incident.db"

# The timeline. Real timestamps -- the ledger does the date math, not us.
JAN_01 = "2026-01-01T09:15:00+00:00"     # agent learns where the user works
MAR_12 = "2026-03-12T00:00:00+00:00"     # user changes jobs. Agent is not told.
APR_05_READ = "2026-04-05T10:04:00+00:00"   # agent consults its memory
APR_05_ACTION = "2026-04-05T10:05:00+00:00"  # agent acts on what it read

RULE = "─" * 66


def header(text: str) -> None:
    print(f"\n{RULE}\n  {text}\n{RULE}")


JAN_01 = "2026-01-01T09:15:00+00:00"
FEB_20 = "2026-02-20T14:30:00+00:00"     # user mentions a promotion
MAR_12 = "2026-03-12T00:00:00+00:00"     # user changes jobs. Agent is not told.
APR_05_READ = "2026-04-05T10:04:00+00:00"
APR_05_ACTION = "2026-04-05T10:05:00+00:00"


def build_incident(ledger: MemoryLedger) -> None:
    """Replay what actually happened, in order."""
    a = ledger.agent_id

    # January: onboarding. The agent learns two things.
    ledger.storage.append(
        agent_id=a, operation="write", key="user_employer",
        new_value="Acme Corp", provenance="user said so in onboarding chat",
        ts=JAN_01,
    )
    ledger.storage.append(
        agent_id=a, operation="write", key="user_seniority",
        new_value="mid-level engineer", provenance="inferred from resume",
        ts=JAN_01,
    )

    # February: the user mentions a promotion in passing. The agent updates.
    ledger.storage.append(
        agent_id=a, operation="update", key="user_seniority",
        old_value="mid-level engineer", new_value="senior engineer",
        provenance="user mentioned promotion in chat", ts=FEB_20,
    )

    # March 12: the user changes jobs.
    #
    # Nothing happens here. That is the entire problem -- the world moved and
    # the agent's memory did not. There is no operation to record, because no
    # operation occurred. This gap is invisible to every memory backend on the
    # market, and it is the thing the ledger makes visible.

    # April 5: the agent consults memory before drafting an outreach email.
    ledger.storage.log_read(
        agent_id=a, key="user_employer", op_id_seen=1,
        provenance="drafting outreach email -- need current employer",
        ts=APR_05_READ,
    )
    ledger.storage.log_read(
        agent_id=a, key="user_seniority", op_id_seen=3,   # the Feb update
        provenance="drafting outreach email -- need seniority",
        ts=APR_05_READ,
    )


def main() -> None:
    if os.path.exists(DB):
        os.remove(DB)

    with MemoryLedger("outreach_agent", DB) as ledger:
        build_incident(ledger)

        header("THE INCIDENT")
        print("""
  On April 5 at 10:05, the agent sent an email addressed to the user
  at Acme Corp. The user left Acme in March.

  The memory backend shows: user_employer = "Acme Corp".
  That is all it shows. There is nothing else to look at.

  So: why did the agent believe that?""")

        # ---------------------------------------------------------------
        header("1. WHAT DID THE AGENT READ BEFORE IT ACTED?")

        reads = ledger.reads_before(APR_05_ACTION)
        for r in reads:
            age_days = r.age_at_read.days if r.age_at_read else 0
            print(f"\n  {r.ts[:16]}  {r.key}")
            print(f"    value seen   : {r.value_seen!r}")
            print(f"    written      : {r.written_at[:10]}")
            print(f"    AGE AT READ  : {age_days} days")
            print(f"    why it read  : {r.provenance}")

        # ---------------------------------------------------------------
        header("2. WAS ANY OF IT STALE?")

        stale = ledger.stale_reads(APR_05_ACTION, older_than=timedelta(days=90))
        print()
        for r in reads:
            age = r.age_at_read.days if r.age_at_read else 0
            flagged = any(s.key == r.key for s in stale)
            mark = "⚠  STALE" if flagged else "   ok   "
            print(f"  {mark}  {r.key:<16} {age:>3} days old at read")

        print("\n  Two beliefs, read in the same instant. One is fine -- seniority")
        print("  was refreshed in February. The other had not been touched since")
        print("  January, and the world had moved on without telling the agent.")

        # ---------------------------------------------------------------
        header("3. WHERE DID THOSE BELIEFS COME FROM?")

        for key in ("user_employer", "user_seniority"):
            print(f"\n  {key}")
            for h in ledger.history(key):
                print(f"    {h['ts'][:10]}  {h['operation']:<7} "
                      f"{h['old_value']!r} → {h['new_value']!r}")
                print(f"                provenance: {h['provenance']}")

        print("\n  Seniority was written, then corrected. Employer was written once")
        print("  and never revisited. The difference is the whole story.")

        # ---------------------------------------------------------------
        header("4. WHAT DID THE AGENT BELIEVE AT THE MOMENT IT ACTED?")

        print()
        for key, value in ledger.as_of(APR_05_ACTION).items():
            print(f"  {key:<16} = {value!r}")
        print("\n  This is the agent's complete belief state at 10:05 on April 5.")
        print("  Reconstructed from the log, not from a backup.")

        # ---------------------------------------------------------------
        header("5. CAN THIS RECORD BE TRUSTED?")

        ops = ledger.verify()
        print(f"\n  Hash chain verified: {ops} operations, chain intact.")
        print("  Every operation is chained to the one before it. If anyone had")
        print("  edited a value, deleted a row, or rewritten a provenance string")
        print("  after the fact, this check would have failed.")
        print("\n  The record is admissible.")

        # ---------------------------------------------------------------
        header("SUMMARY")
        print("""
  Without the ledger:
    "The agent said Acme. The memory says Acme. I don't know why."

  With the ledger:
    The agent read user_employer at 10:04, one minute before it acted.
    The value was 94 days old, written once in January from a single
    onboarding message, and never verified since. Here is the agent's
    entire belief state at that instant, and here is cryptographic
    proof that this record has not been altered.
""")

    os.remove(DB)


if __name__ == "__main__":
    main()