"""Ticket source.

For the POC this is a fake Remedy backed by a JSON file. When real Remedy access
arrives, write a `RemedyTicketSource` with the same two methods (`get`, `list_open`)
and switch to it in `get_ticket_source()`. Nothing else in the agent needs to change.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from .config import Config
from .sla import with_sla
from .timeutil import iso, utcnow

# Fields follow SRS FR-06.
TICKET_FIELDS = [
    "tt_number", "site_id", "cell_id", "description", "priority", "status",
    "assignment_group", "assigned_person", "created_at", "last_update_at",
    "sla_due", "closed_at",
]


class FakeRemedy:
    def __init__(self, path: Path, closed_statuses: list[str], settings: dict | None = None):
        self.path = path
        self.closed_statuses = set(closed_statuses)
        self.settings = settings

    def _load(self) -> list[dict]:
        # Re-read every time so you can edit the JSON file while the agent runs
        # (e.g. change a status to "Resolved" to simulate Back Office progress).
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            tickets = json.load(f)
        # Tickets without their own SLA date get one from the SLA matrix (settings.yaml).
        return [with_sla(t, self.settings) for t in tickets] if self.settings else tickets

    def get(self, tt_number: str) -> dict | None:
        for t in self._load():
            if t["tt_number"].upper() == tt_number.upper():
                return t
        return None

    def list_open(self) -> list[dict]:
        """Tickets that still need monitoring (everything that isn't closed)."""
        closed = {s.lower() for s in self.closed_statuses}
        return [t for t in self._load() if (t.get("status") or "").lower() not in closed]

    def list_all(self) -> list[dict]:
        return self._load()


def get_ticket_source(cfg: Config) -> FakeRemedy:
    return FakeRemedy(cfg.tickets_path, cfg.settings["tickets"]["closed_statuses"], cfg.settings)


def seed_fake_tickets(path: Path) -> list[dict]:
    """Write a fresh set of fake tickets with times relative to *now*, covering each rule.
    They have no sla_due of their own: deadlines come from the SLA matrix (created + resolve_within),
    so changing the matrix changes the deadlines. Comments assume the default matrix values."""
    now = utcnow()

    def t(hours: float) -> str:
        return iso(now + timedelta(hours=hours))

    tickets = [
        {   # High, due in 12h, quiet 9h (> 8h) -> follow-up
            "tt_number": "TT-100234", "site_id": "LHR-0451", "cell_id": "LHR-0451-2",
            "description": "High call drop rate (3.8%) on cell, above 2% threshold for 3 consecutive days.",
            "priority": "High", "status": "Assigned", "assignment_group": "Back Office RF",
            "assigned_person": None, "created_at": t(-12), "last_update_at": t(-9),
            "closed_at": None,
        },
        {   # High, due in 3h (< 4h) -> reminder
            "tt_number": "TT-100235", "site_id": "KHI-1120", "cell_id": "KHI-1120-1",
            "description": "Low throughput on LTE cell during busy hour (avg 4 Mbps vs 15 Mbps baseline).",
            "priority": "High", "status": "In Progress", "assignment_group": "Back Office RF",
            "assigned_person": "Back Office Engineer", "created_at": t(-21), "last_update_at": t(-2),
            "closed_at": None,
        },
        {   # Critical, deadline passed 6h ago -> escalation to team lead, then manager
            "tt_number": "TT-100236", "site_id": "ISB-0077", "cell_id": "ISB-0077-3",
            "description": "Cell unavailable intermittently; availability 91% over last 48 hours.",
            "priority": "Critical", "status": "Pending", "assignment_group": "Back Office Transmission",
            "assigned_person": None, "created_at": t(-10), "last_update_at": t(-8),
            "closed_at": None,
        },
        {   # Resolved 30h ago but not closed (> 24h) -> closure check
            "tt_number": "TT-100237", "site_id": "LHR-0612", "cell_id": "LHR-0612-1",
            "description": "High interference (UL RSSI -95 dBm) on cell.",
            "priority": "Medium", "status": "Resolved", "assignment_group": "Back Office RF",
            "assigned_person": "Back Office Engineer", "created_at": t(-60), "last_update_at": t(-30),
            "closed_at": None,
        },
        {   # Low, updated 1h ago, due in 110h -> nothing to do
            "tt_number": "TT-100238", "site_id": "MUL-0210", "cell_id": "MUL-0210-2",
            "description": "Handover failure rate 6% towards neighbour MUL-0211.",
            "priority": "Low", "status": "In Progress", "assignment_group": "Back Office RF",
            "assigned_person": "Back Office Engineer", "created_at": t(-10), "last_update_at": t(-1),
            "closed_at": None,
        },
        {   # Closed -> stop
            "tt_number": "TT-100239", "site_id": "FSD-0033", "cell_id": "FSD-0033-1",
            "description": "VoLTE call setup failures above threshold.",
            "priority": "Medium", "status": "Closed", "assignment_group": "Back Office RF",
            "assigned_person": "Back Office Engineer", "created_at": t(-100), "last_update_at": t(-50),
            "closed_at": t(-50),
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tickets, f, indent=2)
    return tickets
