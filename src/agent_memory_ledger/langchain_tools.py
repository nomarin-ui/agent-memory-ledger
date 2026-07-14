"""LangChain tools that let an agent record and consult beliefs in the ledger.

The agent decides what to remember and states why. That makes provenance real
rather than inferred, which is the whole point.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .ledger import MemoryLedger


class RememberInput(BaseModel):
    key: str = Field(
        description=(
            "A short, stable identifier for this belief, in snake_case. "
            "Reuse the same key when you learn something new about the same "
            "fact, so its history is tracked. Examples: 'user_employer', "
            "'user_timezone', 'project_deadline'."
        )
    )
    value: str = Field(description="What you now believe to be true.")
    why: str = Field(
        description=(
            "Why you believe it. Cite the source: what the user said, what you "
            "inferred and from what, or what tool returned it. This is recorded "
            "permanently and is what someone will read when debugging a mistake."
        )
    )


class RecallInput(BaseModel):
    key: str = Field(description="The key of the belief you want to look up.")
    why: str = Field(
        description="Why you need it right now -- what you are about to do with it."
    )


REMEMBER_DESCRIPTION = """Record a durable fact you have learned, so you can rely on it later.

Use this for things that are true about the world and could LATER BECOME FALSE:
where someone works, what they are called, a deadline, a preference, a decision.
These are beliefs. They have a shelf life. That is why they belong in a ledger.

Do NOT use this for transient conversational state -- the user's current mood, what
they just asked, what you are in the middle of doing. That is context, not belief,
and storing it here will bury the facts that matter.

If you are updating something you already recorded, reuse the same key. The ledger
keeps the history, so nothing is lost."""

RECALL_DESCRIPTION = """Look up a fact you previously recorded.

Every recall is logged along with the reason you gave, so if you later make a
mistake, someone can see exactly which belief you acted on and how old it was.
Give an honest reason."""


def make_memory_tools(ledger: MemoryLedger) -> list[Any]:
    """Build `remember` and `recall` tools bound to this agent's ledger.

    >>> tools = make_memory_tools(MemoryLedger("my_agent", "ledger.db"))
    >>> agent = create_react_agent(model, tools)
    """
    from langchain_core.tools import StructuredTool

    def _remember(key: str, value: str, why: str) -> str:
        op = ledger.write(key, value, provenance=why)
        verb = "Updated" if op.operation == "update" else "Recorded"
        if op.old_value is not None:
            return f"{verb} {key}: {op.old_value!r} -> {value!r}"
        return f"{verb} {key} = {value!r}"

    def _recall(key: str, why: str) -> str:
        value = ledger.read(key, provenance=why)
        if value is None:
            return f"No belief recorded for {key!r}."
        return f"{key} = {value!r}"

    remember = StructuredTool.from_function(
        func=_remember,
        name="remember",
        description=REMEMBER_DESCRIPTION,
        args_schema=RememberInput,
    )
    recall = StructuredTool.from_function(
        func=_recall,
        name="recall",
        description=RECALL_DESCRIPTION,
        args_schema=RecallInput,
    )
    return [remember, recall]