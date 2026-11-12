"""SLA matrix helpers (settings.yaml -> sla_matrix). Pure functions, easy to test."""

from __future__ import annotations

from datetime import datetime, timedelta

from .timeutil import hours_between, iso, parse

FIELDS = ["resolve_within", "followup_if_quiet", "remind_before", "team_lead_after", "manager_after",
          "repeat_every"]


def sla_row(settings: dict, priority: str | None) -> dict:
    """The SLA matrix row for a priority (case-insensitive), falling back to default_priority."""
    matrix = settings["sla_matrix"]
    by_name = {k.lower(): v for k, v in matrix.items()}
    row = by_name.get((priority or "").lower())
    if row is None:
        row = by_name[settings["followup"].get("default_priority", "Medium").lower()]
    missing = [f for f in FIELDS if f not in row]
    if missing:
        raise ValueError(f"sla_matrix row for {priority!r} is missing: {', '.join(missing)}")
    return row


def sla_deadline(ticket: dict, settings: dict) -> datetime | None:
    """Ticket's own SLA date if Remedy gives one, otherwise created + resolve_within."""
    own = parse(ticket.get("sla_due"))
    if own:
        return own
    created = parse(ticket.get("created_at"))
    if not created:
        return None
    return created + timedelta(hours=sla_row(settings, ticket.get("priority"))["resolve_within"])


def with_sla(ticket: dict, settings: dict) -> dict:
    """Copy of the ticket with sla_due filled in and where it came from."""
    t = dict(ticket)
    if not parse(t.get("sla_due")):
        t["sla_due"] = iso(sla_deadline(t, settings))
        t["sla_source"] = "SLA matrix"
    else:
        t.setdefault("sla_source", "Remedy")
    return t


def is_closed(status: str | None, settings: dict) -> bool:
    """True for statuses that mean "stop following up" (case-insensitive)."""
    closed = {s.strip().lower() for s in settings["tickets"]["closed_statuses"]}
    return (status or "").strip().lower() in closed


def sla_status(ticket: dict, settings: dict, now: datetime, closed_statuses=None) -> str:
    """Short human text for the tickets page, e.g. '3.5h left' or 'Overdue 6h'."""
    if is_closed(ticket.get("status"), settings):
        return "Closed"
    if (ticket.get("status") or "").strip().lower() == "resolved":
        return "Resolved"
    due = sla_deadline(ticket, settings)
    if not due:
        return "No SLA"
    h = hours_between(due, now)
    return f"{h:.1f}h left" if h >= 0 else f"Overdue {-h:.1f}h"
