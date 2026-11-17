"""Which CRQs on a site match the ticket's problem (config/crq_relevance.yaml).

Plain word matching against a table the client's engineers can read and correct: no AI and no
model, so it is instant and every mark has a stated reason ("Interference: antenna work").
It only marks and orders CRQs; it never hides one and never claims a CRQ caused the problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


def _words_re(words: list[str]) -> re.Pattern | None:
    words = [str(w).strip().lower() for w in words or [] if str(w).strip()]
    if not words:
        return None
    # Longest first so "call drops" wins over "drop"; whole words only (\b ... \b).
    alts = "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alts})(?![a-z0-9])", re.IGNORECASE)


@dataclass
class IssueType:
    key: str
    label: str
    ticket_re: re.Pattern | None
    work_re: re.Pattern | None


class RelevanceTable:
    def __init__(self, types: list[IssueType]):
        self.types = types

    @classmethod
    def load(cls, path: Path) -> "RelevanceTable":
        """Missing or empty file -> an empty table (nothing is marked)."""
        if not path.exists():
            return cls([])
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        types = []
        for key, spec in (data.get("issue_types") or {}).items():
            spec = spec or {}
            types.append(IssueType(key=key, label=str(spec.get("label") or key),
                                   ticket_re=_words_re(spec.get("ticket_words")),
                                   work_re=_words_re(spec.get("crq_work"))))
        return cls(types)

    def issue_types(self, ticket: dict) -> list[IssueType]:
        """Problem types named in the ticket's description, in table order."""
        text = ticket.get("description") or ""
        return [t for t in self.types if t.ticket_re and t.ticket_re.search(text)]

    def mark(self, crqs: list[dict], ticket: dict) -> tuple[list[str], list[dict]]:
        """(issue labels found in the ticket, CRQs each with a `match` reason or None).

        Order: FAILED/ROLLBACK first (as before), then CRQs matching the issue, then the rest;
        within each group the original order (newest first) is kept.
        """
        found = self.issue_types(ticket)
        out = []
        for c in crqs:
            reason = None
            for t in found:
                m = t.work_re.search(c.get("activity") or "") if t.work_re else None
                if m:
                    reason = f"{t.label}: {m.group(0).lower()} work"
                    break
            out.append({**c, "match": reason})
        risky = lambda c: (c.get("activity_result") or "").upper() in ("FAILED", "ROLLBACK")  # noqa: E731
        out.sort(key=lambda c: (not risky(c), c["match"] is None))
        return [t.label for t in found], out
