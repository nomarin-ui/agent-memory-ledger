"""Detect when different memory values refer to the same real-world entity."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from rapidfuzz import fuzz

from .storage import SQLiteStorage, utcnow_iso

_PUNCT = re.compile(r"[^\w\s@.]")
_SUFFIXES = {"corp", "corporation", "inc", "incorporated", "ltd", "limited",
             "llc", "co", "company", "gmbh", "sa", "bv", "plc"}


class EscalationBudgetExceeded(Exception):
    """Too many ambiguous pairs. Failing loud rather than silently spending."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """Two values that might refer to the same entity."""

    a: str
    b: str
    score: float


@dataclass(frozen=True, slots=True)
class Merge:
    """A decision that two or more values are the same entity."""

    canonical_id: str
    aliases: tuple[str, ...]
    confidence: float
    method: str          # 'exact' | 'fuzzy' | 'claude' | 'manual'
    key: str


@dataclass(slots=True)
class ResolutionStats:
    """What the resolver did. Watch escalated/compared -- that's the cost."""

    compared: int = 0
    auto_merged: int = 0
    auto_rejected: int = 0
    escalated: int = 0

    @property
    def escalation_rate(self) -> float:
        return self.escalated / self.compared if self.compared else 0.0


def normalize(value: str) -> str:
    """Strip the noise that makes identical entities look different.

    >>> normalize("ACME Corp.")
    'acme'
    """
    text = value.lower().strip()
    if "@" in text:                      # john.smith@acme.com -> john smith
        text = text.split("@", 1)[0]
    text = text.replace(".", " ").replace("_", " ")
    text = _PUNCT.sub("", text)
    tokens = [t for t in text.split() if t not in _SUFFIXES]
    return " ".join(tokens)

def block_key(value: str) -> str:
    """Cheap bucket key. Only values sharing a bucket get compared.

    Turns an O(n^2) all-pairs comparison into something roughly linear.
    """
    norm = normalize(value)
    return norm[:2] if norm else ""

def _score(a: str, b: str) -> float:
    """Similarity of two values, 0-100.

    Deliberately NOT token_set_ratio: it scores strict subsets as 100, so
    'Acme' and 'Acme Holdings' would auto-merge. A subsidiary is not its
    parent, and a first name is not a person. WRatio penalizes the length
    mismatch, which is exactly the signal we want.
    """
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return 100.0
    return float(fuzz.WRatio(na, nb))

class EntityResolver:
    """Group an agent's memory values by the real-world entity they refer to."""

    def __init__(
        self,
        storage: SQLiteStorage,
        *,
        client: Any | None = None,
        auto_merge_at: float = 92.0,
        escalate_above: float = 65.0,
        max_escalations: int = 200,
    ) -> None:
        self.storage = storage
        self.client = client            # None -> fuzzy only, zero API cost
        self.auto_merge_at = auto_merge_at
        self.escalate_above = escalate_above
        self.max_escalations = max_escalations
        self.stats = ResolutionStats()

    # -- candidate generation -------------------------------------------

    def _values_for(self, agent_id: str, key: str) -> list[str]:
        """Every distinct value this agent has ever held for this key."""
        rows = self.storage._conn.execute(
            "SELECT DISTINCT new_value FROM memory_operations "
            "WHERE agent_id = ? AND key = ? AND new_value IS NOT NULL "
            "  AND operation != 'entity_merge'",
            (agent_id, key),
        ).fetchall()
        values = []
        for r in rows:
            decoded = json.loads(r["new_value"])
            if isinstance(decoded, str):      # only resolve string values
                values.append(decoded)
        return values

    def _candidates(self, values: Iterable[str]) -> list[Candidate]:
        """Compare only within blocks. Score every pair that shares a bucket."""
        buckets: dict[str, list[str]] = defaultdict(list)
        for v in set(values):
            buckets[block_key(v)].append(v)

        candidates: list[Candidate] = []
        for bucket in buckets.values():
            for i, a in enumerate(bucket):
                for b in bucket[i + 1:]:
                    candidates.append(Candidate(a=a, b=b, score=_score(a, b)))
                    self.stats.compared += 1
        return candidates

    # -- resolution ------------------------------------------------------

    def resolve(self, agent_id: str, key: str) -> list[Merge]:
        """Find merges among this agent's values for one key, and record them.

        Each merge is appended to the ledger as an operation, so *why* the
        agent believes two things are the same is itself auditable.

        >>> resolver.resolve("agent_1", "employer")
        [Merge(canonical_id='entity_acme', aliases=('Acme Corp', 'ACME'), ...)]
        """
        values = self._values_for(agent_id, key)
        if len(values) < 2:
            return []

        confirmed: list[tuple[str, str, float, str]] = []   # a, b, conf, method
        ambiguous: list[Candidate] = []

        for cand in self._candidates(values):
            if cand.score >= self.auto_merge_at:
                confirmed.append((cand.a, cand.b, cand.score / 100.0, "fuzzy"))
                self.stats.auto_merged += 1
            elif cand.score <= self.escalate_above:
                self.stats.auto_rejected += 1
            else:
                ambiguous.append(cand)
                self.stats.escalated += 1

        if len(ambiguous) > self.max_escalations:
            raise EscalationBudgetExceeded(
                f"{len(ambiguous)} ambiguous pairs exceeds cap of "
                f"{self.max_escalations}. Blocking or thresholds are wrong -- "
                f"refusing to spend."
            )

        if ambiguous and self.client is not None:
            confirmed.extend(self._ask_claude(ambiguous))

        groups = self._group(confirmed)
        return [self._record(agent_id, key, g) for g in groups]

    def _group(
        self, confirmed: list[tuple[str, str, float, str]]
    ) -> list[tuple[set[str], float, str]]:
        """Union-find. 'A=B' and 'B=C' means A, B, C are one entity."""
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        meta: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for a, b, conf, method in confirmed:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
            meta[find(a)].append((conf, method))

        clusters: dict[str, set[str]] = defaultdict(set)
        for value in parent:
            clusters[find(value)].add(value)

        out = []
        for root, members in clusters.items():
            if len(members) < 2:
                continue
            entries = meta.get(root, [(1.0, "fuzzy")])
            confidence = min(c for c, _ in entries)      # weakest link
            methods = sorted({m for _, m in entries})
            out.append((members, confidence, "+".join(methods)))
        return out

    def _record(
        self, agent_id: str, key: str, group: tuple[set[str], float, str]
    ) -> Merge:
        """Append the merge to the ledger and open the alias rows."""
        members, confidence, method = group
        aliases = tuple(sorted(members))
        canonical = f"entity_{normalize(aliases[0]).replace(' ', '_')}"
        ts = utcnow_iso()

        self.storage.append(
            agent_id=agent_id,
            operation="entity_merge",
            key=canonical,
            new_value={"aliases": list(aliases), "confidence": confidence,
                       "method": method, "resolved_key": key},
            provenance=f"{method} resolution of {len(aliases)} values "
                       f"for key '{key}', confidence {confidence:.2f}",
            ts=ts,
        )

        for alias in aliases:
            self.storage._conn.execute(
                "INSERT INTO entity_aliases "
                "(canonical_id, alias, confidence, method, valid_from, valid_to) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (canonical, alias, confidence,
                 method if method in {"exact", "fuzzy", "claude", "manual"} else "fuzzy",
                 ts),
            )

        return Merge(canonical_id=canonical, aliases=aliases,
                     confidence=confidence, method=method, key=key)

    def aliases_for(self, canonical_id: str, at: str) -> list[str]:
        """Aliases believed at instant `at`. A day-40 merge is invisible on day 20.

        >>> resolver.aliases_for("entity_acme", "2026-06-20T00:00:00+00:00")
        ['ACME', 'Acme Corp']
        """
        rows = self.storage._conn.execute(
            "SELECT alias FROM entity_aliases "
            "WHERE canonical_id = ? AND valid_from <= ? "
            "  AND (valid_to IS NULL OR valid_to > ?) "
            "ORDER BY alias",
            (canonical_id, at, at),
        ).fetchall()
        return [r["alias"] for r in rows]

    # -- the expensive part ---------------------------------------------

    def _ask_claude(
        self, ambiguous: list[Candidate]
    ) -> list[tuple[str, str, float, str]]:
        """Batch the ambiguous pairs into one call. This is the only cost."""
        pairs = "\n".join(
            f"{i}. {c.a!r} vs {c.b!r} (string similarity: {c.score:.0f}%)"
            for i, c in enumerate(ambiguous)
        )
        prompt = ENTITY_PROMPT.format(pairs=pairs)

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        text = text.replace("```json", "").replace("```", "").strip()

        try:
            decisions = json.loads(text)
        except json.JSONDecodeError:
            return []                    # bad response -> merge nothing. Safe default.

        out = []
        for d in decisions:
            if d.get("same_entity") and 0.0 <= d.get("confidence", 0) <= 1.0:
                cand = ambiguous[d["index"]]
                out.append((cand.a, cand.b, float(d["confidence"]), "claude"))
        return out


ENTITY_PROMPT = """You are resolving entities in an AI agent's memory.

For each numbered pair below, decide whether the two values refer to the SAME
real-world entity.

Be conservative. A false merge is far worse than a missed one: merging two
distinct entities means the agent now holds a single confused belief about two
different things in the world. When genuinely unsure, say they are different.

Pairs:
{pairs}

Respond with ONLY a JSON array, no prose, no markdown fences:
[
  {{"index": 0, "same_entity": true, "confidence": 0.95, "reason": "abbreviation of the same company"}},
  {{"index": 1, "same_entity": false, "confidence": 0.90, "reason": "different people who share a surname"}}
]

confidence is your certainty in the decision, not the likelihood they match."""