"""Recent planned work (CRQs) on a ticket's site, from the CRQ Analyst (read-only).

A recent planned change is the most common cause of a new fault, so the agent asks the CRQ
Analyst: "which CRQs were running on this site from N days before the ticket was opened until
now?" and shows the answer on the ticket page and (optionally) in drafted emails.

The CRQ side is optional: if it is off, slow or returns an error, the lookup returns None and
the agent carries on exactly as before, just without the CRQ note. It never raises.

Settings: settings.yaml -> crq. The CRQ Analyst must be running (python -m crq_rag.web).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .crq_relevance import RelevanceTable
from .timeutil import iso, parse, utcnow

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": True,
    "url": "http://127.0.0.1:8765",
    "lookback_days": 7,
    "show_on_ticket_page": True,
    "add_note_to_emails": False,
    "timeout_seconds": 3,
    "max_in_email": 3,
    "cache_minutes": 10,
    # Table of problem types -> CRQ work that can cause them (marks "matches the reported issue").
    "relevance_file": "config/crq_relevance.yaml",
}
_REPO = Path(__file__).resolve().parent.parent
_SITE = re.compile(r"^\s*([A-Za-z]?\d{3,5})(?:[_-]\d{1,2})?\s*$")   # W6102, W6102_1, 6102
_RISKY = ("FAILED", "ROLLBACK")
_FAILURE_MEMORY_SECONDS = 60


def crq_settings(settings: dict) -> dict:
    return {**DEFAULTS, **(settings.get("crq") or {})}


def site_of(ticket: dict) -> str | None:
    """The ticket's site ID. Uses site_id, else derives it from the cell (W6102_1 -> W6102)."""
    for value in (ticket.get("site_id"), ticket.get("cell_id")):
        m = _SITE.match(value or "")
        if m:
            return m.group(1).upper()
    return None


class CRQLookup:
    def __init__(self, settings: dict, session: requests.Session | None = None,
                 relevance: RelevanceTable | None = None):
        self.s = crq_settings(settings)
        self.http = session or requests.Session()
        path = Path(self.s["relevance_file"]) if self.s.get("relevance_file") else None
        if relevance is None:
            relevance = RelevanceTable.load(path if path is None or path.is_absolute() else _REPO / path) \
                if path else RelevanceTable([])
        self.relevance = relevance
        self._cache: dict[tuple, tuple[float, dict | None]] = {}
        self._down_until = 0.0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.s["enabled"])

    def window(self, ticket: dict, now: datetime | None = None) -> tuple[datetime, datetime] | None:
        created = parse(ticket.get("created_at"))
        if not created:
            return None
        return created - timedelta(days=float(self.s["lookback_days"])), now or utcnow()

    def for_ticket(self, ticket: dict | None, now: datetime | None = None) -> dict | None:
        """{site, window_start, window_end, total, unknown_site_crqs, issue_types, matches, crqs}
        or None when disabled, the ticket has no usable site/date, or the CRQ Analyst cannot be
        reached. Each CRQ carries `match`: why it fits the ticket's problem, or None."""
        if not self.enabled or not ticket:
            return None
        site, win = site_of(ticket), self.window(ticket, now)
        if not site or not win:
            return None
        start, end = win
        # Hour resolution keeps the cache useful while "now" moves; the CRQ data is a fixed export.
        key = (site, start.strftime("%Y%m%d%H"), end.strftime("%Y%m%d%H"))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < float(self.s["cache_minutes"]) * 60:
            return self._with_matches(hit[1], ticket)
        if time.monotonic() < self._down_until:
            return None   # failed moments ago: don't make every page wait for the timeout again

        try:
            r = self.http.get(f"{self.s['url'].rstrip('/')}/api/site_crqs", timeout=float(self.s["timeout_seconds"]),
                              params={"site": site, "start": iso(start), "end": iso(end), "limit": 20})
            r.raise_for_status()
            data = r.json()
            result = {
                "site": data.get("site_id") or site,
                "window_start": iso(start), "window_end": iso(end),
                "total": int(data.get("total_matches") or 0),
                "unknown_site_crqs": int(data.get("crqs_with_unknown_sites_in_window") or 0),
                "crqs": [c for c in (data.get("crqs") or []) if isinstance(c, dict) and c.get("crq_id")],
            }
        except Exception as e:  # noqa: BLE001 - the CRQ side must never break the agent
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            self._down_until = time.monotonic() + _FAILURE_MEMORY_SECONDS
            log.warning("CRQ lookup failed for %s: %s", site, self.last_error)
            return None
        self.last_error = None
        self._cache[key] = (time.monotonic(), result)
        return self._with_matches(result, ticket)

    def _with_matches(self, result: dict, ticket: dict) -> dict:
        """Mark CRQs that fit the ticket's problem (done per ticket; the site data is cached)."""
        issues, crqs = self.relevance.mark(result["crqs"], ticket)
        return {**result, "issue_types": issues, "matches": sum(1 for c in crqs if c["match"]), "crqs": crqs}

    def email_note(self, result: dict | None) -> str | None:
        """Plain-text note for a drafted email, written by code (never by the AI), or None.
        Only when CRQs were found: "nothing found" is not news worth an email line."""
        if not result or not result["crqs"]:
            return None
        crqs = result["crqs"]
        shown = crqs[: int(self.s["max_in_email"])]
        since = _day(result["window_start"])
        lines = [f"Planned work check: {result['total']} CRQ(s) on site {result['site']} since {since}:"]
        lines += [f"- {crq_line(c)}" + (f" - matches the reported issue ({c['match']})" if c.get("match") else "")
                  for c in shown]
        if result["total"] > len(shown):
            lines.append(f"- and {result['total'] - len(shown)} more")
        lines.append("Please check whether any of these changes is related to this issue.")
        return "\n".join(lines)


def is_risky(c: dict) -> bool:
    return (c.get("activity_result") or "").upper() in _RISKY


def crq_line(c: dict) -> str:
    """'CRQ000003858436 (MDT, SW / Capacity upgrade), 12 Nov 2026, Request For Authorization'."""
    what = ", ".join(x for x in (c.get("change_type"), c.get("activity")) if x)
    when = _day(c.get("sched_start"))
    if c.get("sched_end") and _day(c["sched_end"]) != when:
        when += f" to {_day(c['sched_end'])}"
    result = c.get("activity_result")
    state = f"result {result}" if result else (c.get("status") or "status unknown")
    return f"{c['crq_id']} ({what}), {when}, {state}"


def _day(value: str | None) -> str:
    """'2026-11-12T00:00:00' -> '12 Nov 2026' (calendar day only: CRQ times are local time)."""
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y") if value else "?"
    except ValueError:
        return value or "?"
