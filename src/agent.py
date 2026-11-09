"""The Escalator Agent: ties together email, tickets, AI and storage.

  process_inbox()   read new emails -> match ticket -> classify -> draft reply
  run_followups()   apply SLA / no-update rules to open tickets -> draft follow-ups / escalations
  approve_draft()   human-approved send (the only path that sends email)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .ai_provider import AIError, get_provider
from .classifier import classify_email
from .config import Config
from .email_client import get_email_client
from .responder import draft_email
from .storage import AUTO_KINDS, Storage
from .ticket_source import get_ticket_source
from .timeutil import hours_between, iso, parse, utcnow

# Intents that do not need a reply from us.
NO_REPLY_INTENTS = {"Acknowledgment", "Unclear"}


class Agent:
    def __init__(self, cfg: Config, *, storage: Storage | None = None, ai=None, email=None, tickets=None):
        self.cfg = cfg
        self.store = storage or Storage(cfg.db_path)
        self._ai = ai
        self.email = email or get_email_client(cfg)
        self.tickets = tickets or get_ticket_source(cfg)
        self.allowed = cfg.allowed_recipients()

    @property
    def ai(self):
        # Created lazily so the approval UI works even when the AI key is missing.
        if self._ai is None:
            self._ai = get_provider(self.cfg)
        return self._ai

    # =========================================================================
    # Inbound email
    # =========================================================================
    def process_inbox(self) -> dict:
        stats = {"new": 0, "ticket_related": 0, "drafts": 0, "flagged": 0, "ignored": 0}
        for msg in self.email.fetch_recent():
            if self.store.email_seen(msg["id"]):
                continue
            stats["new"] += 1
            outcome = self._process_message(msg)
            stats[outcome] = stats.get(outcome, 0) + 1
            if outcome in ("drafts", "flagged"):
                stats["ticket_related"] += 1
        self.store.audit("agent", "inbox_checked", **stats)
        return stats

    def _save_inbound(self, msg: dict, **extra) -> int:
        return self.store.save_email(
            message_id=msg["id"], conversation_id=msg.get("conversation_id"), direction="in",
            sender=msg["sender"], recipients=msg.get("to"), cc=msg.get("cc"), subject=msg["subject"],
            body=msg["body"], received_at=msg["received_at"], **extra,
        )

    def _process_message(self, msg: dict) -> str:
        if self.cfg.my_address and msg["sender"] == self.cfg.my_address:
            self._save_inbound(msg, status="ignored", review_reason="Sent by us")
            return "ignored"

        tts = self.cfg.find_ticket_ids(f"{msg['subject']}\n{msg['body']}")
        if not tts:
            self._save_inbound(msg, status="ignored", review_reason="No ticket number found")
            self.store.audit("agent", "email_ignored", subject=msg["subject"], reason="no ticket number")
            return "ignored"

        tt = tts[0]
        ticket = self.tickets.get(tt)
        reasons = []
        if len(tts) > 1:
            reasons.append(f"Email mentions several tickets ({', '.join(tts)}); matched to {tt}")
        if ticket is None:
            reasons.append(f"{tt} not found in Remedy")
        if msg["sender"] not in self.allowed:
            reasons.append(f"Sender {msg['sender']} is not in the escalation list")

        try:
            result = classify_email(self.ai, msg, ticket, self.cfg.settings["agent"]["min_confidence"])
        except AIError as e:
            result = {"intent": None, "confidence": None, "needs_human_review": True,
                      "review_reason": f"AI error: {e}"}
        if result.get("needs_human_review"):
            reasons.append(result["review_reason"])

        review_reason = "; ".join(r for r in reasons if r) or None
        email_id = self._save_inbound(
            msg, tt_number=tt, intent=result.get("intent"), confidence=result.get("confidence"),
            extracted=result, status="needs_review" if review_reason else "processed",
            review_reason=review_reason,
        )
        self.store.update_ticket_state(tt, last_inbound_at=msg["received_at"])
        self.store.audit("agent", "email_classified", tt, email_id=email_id, intent=result.get("intent"),
                         confidence=result.get("confidence"), flagged=review_reason)

        # Decide whether to draft a reply.
        intent = result.get("intent")
        suspicious = review_reason and "suspicious" in review_reason.lower()
        if ticket is None or suspicious or intent is None or intent in NO_REPLY_INTENTS:
            return "flagged" if review_reason else "ignored"

        to = [msg["sender"]] if msg["sender"] in self.allowed else \
            self.cfg.recipients_for(ticket.get("assignment_group"), 1)
        try:
            draft = draft_email(
                self.ai, ticket=ticket, kind="reply", purpose_key=intent,
                history=self.store.ticket_history(tt, self.cfg.settings["history"]["max_emails_in_prompt"]),
                signature=self.cfg.settings["agent"]["signature"], inbound=msg, classification=result,
            )
        except AIError as e:
            self.store.audit("agent", "draft_failed", tt, email_id=email_id, error=str(e))
            return "flagged"

        notes = [n for n in [review_reason, *draft["warnings"]] if n]
        draft_id = self.store.create_draft(
            tt_number=tt, kind="reply", level=1, to_addrs=to, cc_addrs=[], subject=draft["subject"],
            body=draft["body"], reply_to_message_id=msg["id"], source_email_id=email_id,
            needs_attention="; ".join(notes) or None,
        )
        self.store.audit("agent", "draft_created", tt, draft_id=draft_id, kind="reply", intent=intent)
        return "flagged" if review_reason else "drafts"

    # =========================================================================
    # Follow-up / SLA / escalation rules (FR-07, FR-08, FR-09, FR-10)
    # =========================================================================
    def run_followups(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        stats = {"checked": 0, "drafts": 0, "closed": 0, "skipped_pending": 0}
        for ticket in self.tickets.list_all():
            tt = ticket["tt_number"]
            state = self.store.ticket_state(tt)
            stats["checked"] += 1

            if state["last_status"] != ticket["status"]:
                if state["last_status"] is not None:
                    self.store.audit("agent", "status_changed", tt, old=state["last_status"], new=ticket["status"])
                self.store.update_ticket_state(tt, last_status=ticket["status"])

            action = decide_action(ticket, state, now, self.cfg.settings)
            if action is None:
                continue
            if action.kind == "close":
                if not state["closed"]:
                    self._stop_monitoring(tt)
                    stats["closed"] += 1
                continue
            if action.kind == "flag":
                continue
            if self.store.has_pending_draft(tt):
                stats["skipped_pending"] += 1   # duplicate prevention: human hasn't acted on the last one
                continue
            if self._create_auto_draft(ticket, action):
                stats["drafts"] += 1
        self.store.audit("agent", "followups_checked", now=iso(now), **stats)
        return stats

    def _stop_monitoring(self, tt: str) -> None:
        self.store.update_ticket_state(tt, closed=1)
        for d in self.store.drafts(status="pending", tt_number=tt):
            if d["kind"] in AUTO_KINDS:
                self.store.update_draft(d["id"], status="rejected", decided_at=iso(utcnow()),
                                        error="Ticket closed before approval")
        self.store.audit("agent", "monitoring_stopped", tt, reason="ticket closed")

    def _create_auto_draft(self, ticket: dict, action: "Action") -> bool:
        tt, group = ticket["tt_number"], ticket.get("assignment_group")
        to = self.cfg.recipients_for(group, action.level)
        cc = [a for a in self.cfg.recipients_for(group, 1) if a not in to] if action.level > 1 else []
        if not to:
            self.store.audit("agent", "draft_skipped", tt, reason=f"No level {action.level} recipients configured")
            return False
        try:
            draft = draft_email(
                self.ai, ticket=ticket, kind=action.kind, purpose_key=action.kind, level=action.level,
                history=self.store.ticket_history(tt, self.cfg.settings["history"]["max_emails_in_prompt"]),
                signature=self.cfg.settings["agent"]["signature"],
            )
        except AIError as e:
            self.store.audit("agent", "draft_failed", tt, kind=action.kind, error=str(e))
            return False
        notes = [action.reason, *draft["warnings"]]
        if action.level >= 2:
            notes.insert(0, f"High-impact: level {action.level} escalation to management")
        draft_id = self.store.create_draft(
            tt_number=tt, kind=action.kind, level=action.level, to_addrs=to, cc_addrs=cc,
            subject=draft["subject"], body=draft["body"], needs_attention="; ".join(notes),
        )
        self.store.audit("agent", "draft_created", tt, draft_id=draft_id, kind=action.kind,
                         level=action.level, reason=action.reason)
        return True

    # =========================================================================
    # Human approval -> send (the only code path that sends email)
    # =========================================================================
    def approve_draft(self, draft_id: int, *, subject: str, body: str, to: list[str], cc: list[str]) -> None:
        d = self.store.get_draft(draft_id)
        if d is None or d["status"] != "pending":
            raise ValueError("Draft is not pending")

        to = [a.strip().lower() for a in to if a.strip()]
        cc = [a.strip().lower() for a in cc if a.strip()]
        not_allowed = [a for a in to + cc if a not in self.allowed]
        if not to:
            raise ValueError("At least one recipient is required")
        if not_allowed:
            raise ValueError(f"Not in escalation.yaml, refusing to send: {', '.join(not_allowed)}")

        ticket = self.tickets.get(d["tt_number"])
        if d["kind"] in AUTO_KINDS and ticket and ticket["status"] in self.cfg.settings["tickets"]["closed_statuses"]:
            raise ValueError("Ticket is already closed; follow-up not needed")

        edited = int(body.strip() != d["original_body"].strip() or subject != d["subject"]
                     or to != d["to_addrs"] or cc != d["cc_addrs"])
        try:
            if d["reply_to_message_id"]:
                out_id = self.email.reply(d["reply_to_message_id"], body, to, cc)
            else:
                out_id = self.email.send_new(subject, body, to, cc)
        except Exception as e:
            self.store.update_draft(draft_id, error=str(e))
            self.store.audit("agent", "send_failed", d["tt_number"], draft_id=draft_id, error=str(e))
            raise

        now = iso(utcnow())
        self.store.update_draft(draft_id, status="sent", decided_at=now, subject=subject, body=body,
                                to_addrs=_json(to), cc_addrs=_json(cc), edited=edited, error=None)
        self.store.save_email(message_id=out_id, direction="out", sender=self.cfg.my_address or "agent",
                              recipients=to, cc=cc, subject=subject, body=body, received_at=now,
                              tt_number=d["tt_number"])
        state_update = {"last_outbound_at": now}
        if d["kind"] == "escalation":
            state_update.update(escalation_level=d["level"], last_escalation_at=now)
        if d["kind"] == "reminder":
            state_update["reminder_sent"] = 1
        self.store.update_ticket_state(d["tt_number"], **state_update)
        self.store.audit("human", "draft_approved", d["tt_number"], draft_id=draft_id, edited=bool(edited))
        self.store.audit("agent", "email_sent", d["tt_number"], draft_id=draft_id, kind=d["kind"], to=to, cc=cc)

    def reject_draft(self, draft_id: int, reason: str = "") -> None:
        d = self.store.get_draft(draft_id)
        if d is None or d["status"] != "pending":
            raise ValueError("Draft is not pending")
        self.store.update_draft(draft_id, status="rejected", decided_at=iso(utcnow()), error=reason or None)
        self.store.audit("human", "draft_rejected", d["tt_number"], draft_id=draft_id, reason=reason)


def _json(v) -> str:
    import json
    return json.dumps(v)


# =============================================================================
# Pure rule engine: easy to unit-test and to explain to the client (SRS FR-08 table)
# =============================================================================
@dataclass
class Action:
    kind: str          # followup | reminder | escalation | closure_check | close | flag
    level: int = 1
    reason: str = ""


def decide_action(ticket: dict, state: dict, now: datetime, settings: dict) -> Action | None:
    rules = settings["followup"]
    status = ticket["status"]
    priority = ticket.get("priority") or "Medium"
    min_gap = rules["min_hours_between_followups"]

    last_out = parse(state.get("last_outbound_at"))
    hours_since_out = hours_between(now, last_out) if last_out else float("inf")

    # Ticket closed -> stop active follow-up
    if status in settings["tickets"]["closed_statuses"]:
        return Action("close", reason="Ticket closed")

    # Rejected tickets need a human decision, not automated chasing
    if status == "Rejected":
        return Action("flag", reason="Ticket rejected in Remedy")

    # Resolved but not closed -> closure check
    if status == "Resolved":
        resolved_at = parse(ticket.get("last_update_at")) or parse(ticket["created_at"])
        waited = hours_between(now, resolved_at)
        if waited >= rules["resolved_not_closed_hours"] and hours_since_out >= min_gap:
            return Action("closure_check", 1, f"Resolved {waited:.0f}h ago but not closed")
        return None

    sla_due = parse(ticket.get("sla_due"))

    # SLA breached -> escalate one level at a time
    if sla_due and now > sla_due:
        level_now = state.get("escalation_level") or 0
        max_level = rules["max_escalation_level"]
        last_esc = parse(state.get("last_escalation_at"))
        since_esc = hours_between(now, last_esc) if last_esc else float("inf")
        overdue = hours_between(now, sla_due)
        if last_esc is None or since_esc >= rules["escalation_repeat_hours"]:
            # Level 1 is normal follow-up to the Back Office (FR-09). A breach goes to the
            # team lead (level 2) first, then the manager (level 3), then keeps chasing level 3.
            next_level = min(max(level_now + 1, 2), max_level)
            if last_esc is None or hours_since_out >= min_gap:
                return Action("escalation", next_level, f"SLA breached {overdue:.1f}h ago")
        return None

    # SLA approaching -> one-time reminder
    if sla_due and not state.get("reminder_sent"):
        left = hours_between(sla_due, now)
        if left <= rules["reminder_before_sla_hours"].get(priority, 4) and hours_since_out >= min_gap:
            return Action("reminder", 1, f"SLA due in {left:.1f}h")

    # No update within the configured interval -> follow-up
    activity = [parse(ticket["created_at"]), parse(ticket.get("last_update_at")),
                parse(state.get("last_inbound_at")), last_out]
    last_activity = max(a for a in activity if a)
    quiet = hours_between(now, last_activity)
    if quiet >= rules["no_update_hours"].get(priority, 24) and hours_since_out >= min_gap:
        return Action("followup", 1, f"No update for {quiet:.0f}h")
    return None
