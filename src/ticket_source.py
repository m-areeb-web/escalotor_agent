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
from .timeutil import iso, utcnow

# Fields follow SRS FR-06.
TICKET_FIELDS = [
    "tt_number", "site_id", "cell_id", "description", "priority", "status",
    "assignment_group", "assigned_person", "created_at", "last_update_at",
    "sla_due", "closed_at",
]


class FakeRemedy:
    def __init__(self, path: Path, closed_statuses: list[str]):
        self.path = path
        self.closed_statuses = set(closed_statuses)

    def _load(self) -> list[dict]:
        # Re-read every time so you can edit the JSON file while the agent runs
        # (e.g. change a status to "Resolved" to simulate Back Office progress).
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def get(self, tt_number: str) -> dict | None:
        for t in self._load():
            if t["tt_number"].upper() == tt_number.upper():
                return t
        return None

    def list_open(self) -> list[dict]:
        """Tickets that still need monitoring (everything that isn't closed)."""
        return [t for t in self._load() if t["status"] not in self.closed_statuses]

    def list_all(self) -> list[dict]:
        return self._load()


def get_ticket_source(cfg: Config) -> FakeRemedy:
    return FakeRemedy(cfg.tickets_path, cfg.settings["tickets"]["closed_statuses"])


def seed_fake_tickets(path: Path) -> list[dict]:
    """Write a fresh set of fake tickets with times relative to *now*, covering each rule."""
    now = utcnow()

    def t(hours: float) -> str:
        return iso(now + timedelta(hours=hours))

    tickets = [
        {   # Quiet for a long time -> follow-up
            "tt_number": "TT-100234", "site_id": "LHR-0451", "cell_id": "LHR-0451-2",
            "description": "High call drop rate (3.8%) on cell, above 2% threshold for 3 consecutive days.",
            "priority": "High", "status": "Assigned", "assignment_group": "Back Office RF",
            "assigned_person": None, "created_at": t(-30), "last_update_at": t(-20),
            "sla_due": t(18), "closed_at": None,
        },
        {   # SLA close -> reminder
            "tt_number": "TT-100235", "site_id": "KHI-1120", "cell_id": "KHI-1120-1",
            "description": "Low throughput on LTE cell during busy hour (avg 4 Mbps vs 15 Mbps baseline).",
            "priority": "High", "status": "In Progress", "assignment_group": "Back Office RF",
            "assigned_person": "Back Office Engineer", "created_at": t(-20), "last_update_at": t(-2),
            "sla_due": t(3), "closed_at": None,
        },
        {   # SLA breached -> escalation
            "tt_number": "TT-100236", "site_id": "ISB-0077", "cell_id": "ISB-0077-3",
            "description": "Cell unavailable intermittently; availability 91% over last 48 hours.",
            "priority": "Critical", "status": "Pending", "assignment_group": "Back Office Transmission",
            "assigned_person": None, "created_at": t(-14), "last_update_at": t(-10),
            "sla_due": t(-6), "closed_at": None,
        },
        {   # Resolved but not closed -> closure check
            "tt_number": "TT-100237", "site_id": "LHR-0612", "cell_id": "LHR-0612-1",
            "description": "High interference (UL RSSI -95 dBm) on cell.",
            "priority": "Medium", "status": "Resolved", "assignment_group": "Back Office RF",
            "assigned_person": "Back Office Engineer", "created_at": t(-72), "last_update_at": t(-30),
            "sla_due": t(24), "closed_at": None,
        },
        {   # Recently updated, nothing to do
            "tt_number": "TT-100238", "site_id": "MUL-0210", "cell_id": "MUL-0210-2",
            "description": "Handover failure rate 6% towards neighbour MUL-0211.",
            "priority": "Low", "status": "In Progress", "assignment_group": "Back Office RF",
            "assigned_person": "Back Office Engineer", "created_at": t(-10), "last_update_at": t(-1),
            "sla_due": t(100), "closed_at": None,
        },
        {   # Closed -> stop
            "tt_number": "TT-100239", "site_id": "FSD-0033", "cell_id": "FSD-0033-1",
            "description": "VoLTE call setup failures above threshold.",
            "priority": "Medium", "status": "Closed", "assignment_group": "Back Office RF",
            "assigned_person": "Back Office Engineer", "created_at": t(-100), "last_update_at": t(-50),
            "sla_due": t(-40), "closed_at": t(-50),
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tickets, f, indent=2)
    return tickets
