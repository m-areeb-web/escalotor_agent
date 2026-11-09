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
            tt = f"TT-{m.group(1)}"
            if tt not in seen:
                seen.append(tt)
        return seen

    # ---- escalation recipients ---------------------------------------------
    def _group(self, assignment_group: str | None) -> dict:
        groups = self.escalation.get("assignment_groups", {})
        if assignment_group in groups:
            return groups[assignment_group]
        return groups[self.escalation["default_group"]]

    def recipients_for(self, assignment_group: str | None, level: int) -> list[str]:
        group = self._group(assignment_group)
        return [a.lower() for a in group.get(f"level_{level}", [])]

    def allowed_recipients(self) -> set[str]:
        """Every address in escalation.yaml. The agent may only send to these."""
        allowed: set[str] = set()
        for group in self.escalation.get("assignment_groups", {}).values():
            for addresses in group.values():
                allowed.update(a.lower() for a in addresses)
        return allowed


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
