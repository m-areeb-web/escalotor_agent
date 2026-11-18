"""Loads settings.yaml, escalation.yaml and .env into one Config object."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass
class Config:
    settings: dict
    escalation: dict
    ai_provider: str
    ai_model: str
    ai_api_key: str
    ai_fallback_models: list[str]
    ollama_url: str
    email_provider: str
    ms_client_id: str
    my_address: str
    root: Path = ROOT
    _ticket_re: re.Pattern | None = field(default=None, repr=False)

    # ---- paths -------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.root / "data" / "escalator.db"

    @property
    def tickets_path(self) -> Path:
        return self.root / self.settings["tickets"]["file"]

    # ---- ticket numbers ----------------------------------------------------
    @property
    def ticket_re(self) -> re.Pattern:
        if self._ticket_re is None:
            self._ticket_re = re.compile(self.settings["agent"]["ticket_id_pattern"], re.IGNORECASE)
        return self._ticket_re

    def find_ticket_ids(self, text: str) -> list[str]:
        """Return normalised ticket IDs (TT-123456) found in text, in order, no duplicates."""
        seen: list[str] = []
        for m in self.ticket_re.finditer(text or ""):
            digits = m.group(1) if m.re.groups else re.sub(r"\D", "", m.group(0))
            tt = f"TT-{digits}"
            if tt not in seen:
                seen.append(tt)
        return seen

    # ---- escalation recipients ---------------------------------------------
    def _group(self, assignment_group: str | None) -> dict:
        groups = self.escalation.get("assignment_groups", {})
        if assignment_group in groups:
            return groups[assignment_group] or {}
        default = self.escalation.get("default_group")
        if default not in groups:
            raise ValueError(f"escalation.yaml: no group '{assignment_group}' and default_group '{default}' is not defined")
        return groups[default] or {}

    def recipients_for(self, assignment_group: str | None, level: int) -> list[str]:
        group = self._group(assignment_group)
        return [a.strip().lower() for a in (group.get(f"level_{level}") or []) if a and a.strip()]

    def allowed_recipients(self) -> set[str]:
        """Every address in escalation.yaml. The agent may only send to these."""
        allowed: set[str] = set()
        for group in self.escalation.get("assignment_groups", {}).values():
            for key, addresses in (group or {}).items():
                if key.startswith("level_"):
                    allowed.update(a.strip().lower() for a in (addresses or []) if a and a.strip())
        for p in self._people():
            allowed.add(p["email"])
        return allowed

    # ---- handovers: who is the ticket waiting on? ------------------------------
    def _people(self) -> list[dict]:
        out = []
        for p in self.escalation.get("people") or []:
            email = (p.get("email") or "").strip().lower()
            if p.get("name") and email:
                out.append({**p, "email": email})
        return out

    def owner(self, key: str | None) -> Owner | None:
        """The team or person stored as `team:<name>` / `person:<name>` in the ticket state."""
        if not key or ":" not in key:
            return None
        kind, name = key.split(":", 1)
        if kind == "team" and name in self.escalation.get("assignment_groups", {}):
            return Owner("team", key, name, name)
        if kind == "person":
            for p in self._people():
                if p["name"] == name:
                    return Owner("person", key, name, p.get("team"), p["email"])
        return None

    def find_owner(self, text: str | None) -> Owner | None:
        """Match a free-text name from an email ('the performance team', 'Ali') to a team or person
        in escalation.yaml. Returns None when nothing matches or two different entries match equally."""
        name = _norm(text)
        if not name:
            return None
        candidates: list[tuple[str, Owner]] = []
        for p in self._people():
            o = Owner("person", f"person:{p['name']}", p["name"], p.get("team"), p["email"])
            candidates += [(_norm(n), o) for n in [p["name"], *(p.get("aliases") or [])]]
        for g, spec in self.escalation.get("assignment_groups", {}).items():
            o = Owner("team", f"team:{g}", g, g)
            candidates += [(_norm(n), o) for n in [g, *((spec or {}).get("aliases") or [])]]

        def hit(c: str) -> bool:   # same words, or the contact's words appear whole inside the text
            return bool(c) and (c == name or f" {c} " in f" {name} ")

        hits = [(len(c), o) for c, o in candidates if hit(c)]
        if any(o.kind == "person" for _, o in hits):   # "Ali from Field Ops" means Ali, not the whole team
            hits = [(n, o) for n, o in hits if o.kind == "person"]
        if not hits:
            return None
        best = max(n for n, _ in hits)
        owners = {o.key: o for n, o in hits if n == best}
        return next(iter(owners.values())) if len(owners) == 1 else None

    def owner_recipients(self, owner: Owner, level: int, ticket_group: str | None) -> list[str]:
        """Who to email at a given level for a handed-over ticket."""
        if owner.kind == "person" and level == 1:
            return [owner.email]
        return self.recipients_for(owner.team or ticket_group, level)


@dataclass(frozen=True)
class Owner:
    kind: str                 # team | person
    key: str                  # stored in ticket_state.waiting_on_key
    label: str                # shown to people and to the AI
    team: str | None          # escalation ladder to use (a group in escalation.yaml)
    email: str | None = None  # person only


_FILLER = {"the", "team", "group", "dept", "department", "mr", "ms", "sir", "from", "of"}


def _norm(text: str | None) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()
    return " ".join(w for w in words if w not in _FILLER)


def load_config(root: Path = ROOT) -> Config:
    load_dotenv(root / ".env")
    return Config(
        settings=_load_yaml(root / "config" / "settings.yaml"),
        escalation=_load_yaml(root / "config" / "escalation.yaml"),
        ai_provider=os.getenv("AI_PROVIDER", "gemini").strip().lower(),
        ai_model=os.getenv("AI_MODEL", "gemini-3.5-flash").strip(),
        ai_api_key=os.getenv("AI_API_KEY", "").strip(),
        ai_fallback_models=[m.strip() for m in os.getenv("AI_FALLBACK_MODELS", "").split(",") if m.strip()],
        ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434").strip(),
        email_provider=os.getenv("EMAIL_PROVIDER", "mock").strip().lower(),
        ms_client_id=os.getenv("MS_CLIENT_ID", "").strip(),
        my_address=os.getenv("MY_EMAIL_ADDRESS", "").strip().lower(),
        root=root,
    )
