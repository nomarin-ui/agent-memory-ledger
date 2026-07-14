"""An agent that remembers -- and a ledger that remembers why.

Needs:  pip install -e ".[langchain]" langchain-anthropic
        set ANTHROPIC_API_KEY

Run:    python examples/langchain_agent.py
"""
from __future__ import annotations

import os

from agent_memory_ledger import MemoryLedger
from agent_memory_ledger.langchain_tools import make_memory_tools

DB = "agent_demo.db"

SYSTEM = """You are a helpful assistant with a memory ledger.

When the user tells you a durable fact about themselves or their work, record it
with `remember`. When you need a fact you were told earlier, look it up with
`recall` rather than guessing.

Be specific in the `why` field. Someone may read it months from now while trying
to work out why you made a mistake."""


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY first.")

    from langchain_anthropic import ChatAnthropic
    from langgraph.prebuilt import create_react_agent

    if os.path.exists(DB):
        os.remove(DB)

    with MemoryLedger("demo_agent", DB) as ledger:
        agent = create_react_agent(
            ChatAnthropic(model="claude-sonnet-4-6"),
            make_memory_tools(ledger),
            prompt=SYSTEM,
        )

        turns = [
            "Hi -- I'm a senior engineer at Acme Corp, based in Berlin.",
            "What do you know about where I work?",
            "Actually I left Acme last month. I'm at Globex now.",
            "Where do I work?",
        ]

        for turn in turns:
            print(f"\n\033[1muser:\033[0m {turn}")
            result = agent.invoke({"messages": [("user", turn)]})
            print(f"\033[1magent:\033[0m {result['messages'][-1].content}")

        # ---- what the ledger caught --------------------------------------
        print("\n" + "─" * 66)
        print("  WHAT THE LEDGER RECORDED")
        print("─" * 66)

        for key, value in ledger.snapshot().items():
            print(f"\n  {key} = {value!r}")
            for h in ledger.history(key):
                print(f"    {h['operation']:<7} {h['old_value']!r} -> {h['new_value']!r}")
                print(f"            why: {h['provenance']}")

        print("\n" + "─" * 66)
        print("  WHAT THE AGENT CONSULTED")
        print("─" * 66 + "\n")

        for r in ledger.reads_before("9999-12-31T23:59:59+00:00"):
            age = f"{r.age_at_read.seconds}s old" if r.age_at_read else "not found"
            print(f"  {r.key:<16} saw {r.value_seen!r:<12} ({age})")
            print(f"    why: {r.provenance}")

        print(f"\n  Chain verified: {ledger.verify()} operations.\n")

    os.remove(DB)


if __name__ == "__main__":
    main()