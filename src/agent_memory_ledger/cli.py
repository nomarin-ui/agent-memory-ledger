"""Command-line access to the ledger.

The organizing question is not "what can I query" but "my agent just did
something stupid -- what did it believe?"
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import click

from .entity_resolver import EntityResolver
from .ledger import MemoryLedger
from .storage import ChainIntegrityError

FAR_FUTURE = "9999-12-31T23:59:59+00:00"


def _parse_when(value: str | None) -> str:
    """Accept 'now', an ISO timestamp, or a relative offset like '2h' / '30d'."""
    if value is None or value == "now":
        return datetime.now(timezone.utc).isoformat()
    if value[-1] in "smhd" and value[:-1].isdigit():
        n = int(value[:-1])
        delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
                 "h": timedelta(hours=n), "d": timedelta(days=n)}[value[-1]]
        return (datetime.now(timezone.utc) - delta).isoformat()
    return value          # assume ISO8601


def _fmt(value: object) -> str:
    return json.dumps(value) if not isinstance(value, str) else value


@click.group()
@click.option("--db", default="ledger.db", show_default=True,
              help="Path to the ledger database.")
@click.option("--agent", required=True, help="Which agent's ledger to open.")
@click.pass_context
def main(ctx: click.Context, db: str, agent: str) -> None:
    """Query an agent memory ledger."""
    ctx.obj = MemoryLedger(agent, db)
    ctx.call_on_close(ctx.obj.close)


# -- the 2am command --------------------------------------------------

@main.command()
@click.argument("when", default="now")
@click.option("--stale-after", default=90, show_default=True,
              help="Flag beliefs older than this many days at the time they were read.")
@click.pass_obj
def incident(ledger: MemoryLedger, when: str, stale_after: int) -> None:
    """What did the agent believe and read around WHEN?

    WHEN is an ISO timestamp, 'now', or an offset like 2h / 30d.

        aml --agent bot incident 2026-04-05T10:05:00+00:00
        aml --agent bot incident 2h
    """
    at = _parse_when(when)
    click.echo(f"\nIncident window: up to {at[:19]}\n")

    reads = ledger.reads_before(at, limit=20)
    if not reads:
        click.echo("  The agent read nothing before this moment.")
        click.echo("  Either it did not consult its memory, or nothing was logged.\n")
        return

    stale = {r.key for r in ledger.stale_reads(
        at, older_than=timedelta(days=stale_after))}

    click.secho("  WHAT IT READ", bold=True)
    for r in reads:
        age = f"{r.age_at_read.days}d" if r.age_at_read else "—"
        flag = click.style("STALE", fg="red", bold=True) if r.key in stale else "  ok "
        click.echo(f"    {flag}  {r.key:<22} {_fmt(r.value_seen):<20} "
                   f"{age:>5} old at read")
        if r.provenance:
            click.echo(click.style(f"           why: {r.provenance}", dim=True))

    click.secho("\n  WHAT IT BELIEVED", bold=True)
    for key, value in ledger.as_of(at).items():
        click.echo(f"    {key:<22} = {_fmt(value)}")

    try:
        n = ledger.verify()
        click.secho(f"\n  Chain verified: {n} operations.\n", fg="green")
    except ChainIntegrityError as exc:
        click.secho(f"\n  CHAIN BROKEN: {exc}\n", fg="red", bold=True)
        sys.exit(1)


# -- the individual queries -------------------------------------------

@main.command(name="as-of")
@click.argument("when")
@click.pass_obj
def as_of(ledger: MemoryLedger, when: str) -> None:
    """Everything the agent believed at WHEN."""
    beliefs = ledger.as_of(_parse_when(when))
    if not beliefs:
        click.echo("  Nothing believed at that moment.")
        return
    for key, value in beliefs.items():
        click.echo(f"  {key:<24} = {_fmt(value)}")


@main.command()
@click.argument("key")
@click.pass_obj
def history(ledger: MemoryLedger, key: str) -> None:
    """Every change to KEY, oldest first, with provenance."""
    ops = ledger.history(key)
    if not ops:
        click.echo(f"  No history for {key!r}.")
        return
    for h in ops:
        old = _fmt(h["old_value"]) if h["old_value"] is not None else "—"
        new = _fmt(h["new_value"]) if h["new_value"] is not None else "—"
        click.echo(f"  {h['ts'][:19]}  {h['operation']:<12} {old} → {new}")
        if h["provenance"]:
            click.echo(click.style(f"                       {h['provenance']}", dim=True))


@main.command()
@click.argument("when", default="now")
@click.option("--key", default=None, help="Only reads of this key.")
@click.option("--limit", default=20, show_default=True)
@click.pass_obj
def reads(ledger: MemoryLedger, when: str, key: str | None, limit: int) -> None:
    """What the agent consulted before WHEN."""
    for r in ledger.reads_before(_parse_when(when), key=key, limit=limit):
        age = f"{r.age_at_read.days}d" if r.age_at_read else "—"
        click.echo(f"  {r.ts[:19]}  {r.key:<22} {_fmt(r.value_seen):<20} {age:>5} old")
        if r.provenance:
            click.echo(click.style(f"                       {r.provenance}", dim=True))


@main.command()
@click.pass_obj
def verify(ledger: MemoryLedger) -> None:
    """Check the hash chain. Exits non-zero if it has been tampered with."""
    try:
        n = ledger.verify()
        click.secho(f"  Chain intact. {n} operations verified.", fg="green")
    except ChainIntegrityError as exc:
        click.secho(f"  CHAIN BROKEN: {exc}", fg="red", bold=True)
        sys.exit(1)


@main.command()
@click.argument("key")
@click.option("--apply", "apply_", is_flag=True,
              help="Record the merges. Without this, only report what would merge.")
@click.pass_obj
def resolve(ledger: MemoryLedger, key: str, apply_: bool) -> None:
    """Find values under KEY that refer to the same entity.

    Dry-run by default. A merge is a permanent, hash-chained belief -- so
    you get to see it before it happens.
    """
    resolver = EntityResolver(ledger.storage)
    values = ledger.storage.distinct_values(ledger.agent_id, key)

    if len(values) < 2:
        click.echo(f"  Only {len(values)} distinct value(s) for {key!r}. Nothing to resolve.")
        return

    if not apply_:
        click.echo(f"  Distinct values for {key!r}:")
        for v in sorted(set(values)):
            click.echo(f"    {v!r}")
        candidates = resolver._candidates(values)
        merges = [c for c in candidates if c.score >= resolver.auto_merge_at]
        click.echo(f"\n  {len(merges)} pair(s) would auto-merge:")
        for c in merges:
            click.echo(f"    {c.a!r} = {c.b!r}  ({c.score:.0f}%)")
        click.echo(click.style("\n  Dry run. Pass --apply to record.", dim=True))
        return

    for m in resolver.resolve(ledger.agent_id, key):
        click.secho(f"  merged: {' = '.join(repr(a) for a in m.aliases)}", fg="yellow")
        click.echo(f"          canonical: {m.canonical_id}  "
                   f"confidence: {m.confidence:.2f}  method: {m.method}")

@main.command()
@click.pass_obj
def demo(ledger: MemoryLedger) -> None:
    """Seed a ledger with the stale-belief incident, then tell you what to run."""
    if ledger.verify():
        raise click.ClickException(
            f"{ledger.storage.path} already has operations. "
            "Point --db at a new file."
        )

    JAN_01 = "2026-01-01T09:15:00+00:00"
    FEB_20 = "2026-02-20T14:30:00+00:00"
    APR_05 = "2026-04-05T10:04:00+00:00"

    ledger.write("user_employer", "Acme Corp",
                 provenance="user said so in onboarding chat", at=JAN_01)
    ledger.write("user_seniority", "mid-level engineer",
                 provenance="inferred from resume", at=JAN_01)
    ledger.write("user_seniority", "senior engineer",
                 provenance="user mentioned promotion in chat", at=FEB_20)

    # March 12: the user changes jobs. Nothing is recorded, because nothing
    # happened -- that gap is the entire point.

    ledger.read("user_employer",
                provenance="drafting outreach email -- need current employer", at=APR_05)
    ledger.read("user_seniority",
                provenance="drafting outreach email -- need seniority", at=APR_05)

    db, agent = ledger.storage.path, ledger.agent_id
    click.echo(f"Seeded {db}: 3 operations, 2 reads, 1 stale belief.\n")
    click.echo("Now ask what the agent believed when it acted:\n")
    click.echo(f"  aml --db {db} --agent {agent} incident 2026-04-05T10:05:00+00:00")

if __name__ == "__main__":
    main()