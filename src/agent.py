"""The Escalator Agent: ties together email, tickets, AI and storage.

  process_inbox()   read new emails -> match ticket -> classify -> draft reply
  run_followups()   apply SLA / no-update rules to open tickets -> draft follow-ups / escalations
  approve_draft()   human-approved send (the only path that sends email)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .ai_provider import AIError, get_provider
from .classifier import classify_email
from .config import Config
from .email_client import EmailError, get_email_client
from .email_text import clean_body
from .responder import draft_email
from .ticket_finder import identify_ticket
from .sla import is_closed, sla_deadline, sla_row
from .storage import AUTO_KINDS, Storage
from .ticket_source import get_ticket_source
from .timeutil import hours_between, iso, parse, utcnow

# Intents that do not need a reply from us.
NO_REPLY_INTENTS = {"Acknowledgment", "Unclear"}

_AUTO_REPLY_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto(matic)?[- ]?reply|autoreply|auto:|out of (the )?office|ooo\b)", re.IGNORECASE)
_BOUNCE_SUBJECT = re.compile(
    r"^\s*(undeliverable|undelivered mail|delivery status notification|mail delivery (failed|failure)|"
    r"returned mail|delivery has failed|failure notice)", re.IGNORECASE)
_BOUNCE_SENDER = re.compile(r"^(mailer-daemon|postmaster|microsoftexchange[0-9a-f]*)@", re.IGNORECASE)


def _is_auto_reply(msg: dict) -> bool:
    return bool(_AUTO_REPLY_SUBJECT.match(msg.get("subject") or ""))


def _is_bounce(msg: dict) -> bool:
    return bool(_BOUNCE_SUBJECT.match(msg.get("subject") or "") or _BOUNCE_SENDER.match(msg.get("sender") or ""))


class Agent:
    def __init__(self, cfg: Config, *, storage: Storage | None = None, ai=None, email=None, tickets=None):
        self.cfg = cfg
        self.store = storage or Storage(cfg.db_path)
        self._ai = ai
        self.email = email or get_email_client(cfg)
        self.tickets = tickets or get_ticket_source(cfg)
        self.allowed = cfg.allowed_recipients()
        # DEMO MODE (settings.yaml agent.demo_reply_to_any_sender): anyone who emails about a ticket is
        # treated like the Back Office, and replies may go back to that sender. Escalations still only
        # go to escalation.yaml contacts. Switch off before real use (SRS rule).
        self.demo_mode = bool(cfg.settings["agent"].get("demo_reply_to_any_sender", False))

    def _trusted_sender(self, sender: str) -> bool:
        return sender in self.allowed or (self.demo_mode and bool(sender))

    @property
    def ai(self):
        # Created lazily so the approval UI works even when the AI key is missing.
        if self._ai is None:
            self._ai = get_provider(self.cfg)
        return self._ai

    # =========================================================================
    # Inbound email
    # =========================================================================
    INBOX_MARK = "inbox_checked_until"
    # Re-read this many minutes before the last position: mail servers sometimes list an email a
    # little after its received time. Already-seen emails are skipped, so the overlap is harmless.
    INBOX_OVERLAP_MINUTES = 10

    def process_inbox(self, progress=None) -> dict:
        """Process every email received since the last check, then retry emails whose AI step
        failed earlier. `progress(done, total, subject)` feeds the live status on the approval page."""
        stats = {"new": 0, "ticket_related": 0, "drafts": 0, "flagged": 0, "ignored": 0, "retry": 0,
                 "retried": 0}
        mark = self.store.get_meta(self.INBOX_MARK)
        since = iso(parse(mark) - timedelta(minutes=self.INBOX_OVERLAP_MINUTES)) if mark else None
        new = [m for m in self.email.fetch_since(since) if not self.store.email_seen(m["id"])]
        due = self.store.emails_due_for_retry(iso(utcnow()))
        total = len(new) + len(due)

        for i, msg in enumerate(new):
            if progress:
                progress(i, total, msg["subject"])
            if not self.store.email_seen(msg["id"]):   # another process may have handled it meanwhile
                stats["new"] += 1
                self._count(stats, self._safe_process(msg))
            # Move the position forward only after the email is safely stored.
            if not mark or parse(msg["received_at"]) > parse(mark):
                mark = msg["received_at"]
                self.store.set_meta(self.INBOX_MARK, mark)

        for j, row in enumerate(due):
            if progress:
                progress(len(new) + j, total, row["subject"] + " (retrying AI)")
            stats["retried"] += 1
            msg = {"id": row["message_id"], "conversation_id": row["conversation_id"], "subject": row["subject"],
                   "body": row["body"], "sender": row["sender"], "to": row["recipients"], "cc": row["cc"],
                   "received_at": row["received_at"]}
            self._count(stats, self._safe_process(msg, email_id=row["id"], attempts=row["ai_attempts"]))

        if stats["new"] or stats["retried"]:   # quiet checks are not written to the audit log
            self.store.audit("agent", "inbox_checked", **stats)
        return stats

    def _safe_process(self, msg: dict, email_id: int | None = None, attempts: int = 0) -> str:
        """One broken email must never block the emails after it: unexpected errors put that
        email in the review list and the agent moves on."""
        try:
            return self._process_message(msg, email_id=email_id, attempts=attempts)
        except Exception as e:  # noqa: BLE001 - deliberately broad, see docstring
            reason = f"Agent error while processing this email: {type(e).__name__}: {e}"[:500]
            try:
                existing = email_id or self.store.email_id_for_message(msg["id"])
                self._store_inbound(msg, existing, status="needs_review", review_reason=reason, retry_after=None)
                self.store.audit("agent", "email_failed", email_id=existing, subject=msg.get("subject"),
                                 error=reason)
            except Exception:  # noqa: BLE001
                pass
            return "flagged"

    @staticmethod
    def _count(stats: dict, outcome: str) -> None:
        stats[outcome] = stats.get(outcome, 0) + 1
        if outcome in ("drafts", "flagged"):
            stats["ticket_related"] += 1

    def mark_inbox_start(self) -> None:
        """Only emails arriving from now on will be processed (used by `seed` on a real mailbox)."""
        self.store.set_meta(self.INBOX_MARK, iso(utcnow()))

    def _store_inbound(self, msg: dict, email_id: int | None, **fields) -> int:
        if email_id is None:
            return self.store.save_email(
                message_id=msg["id"], conversation_id=msg.get("conversation_id"), direction="in",
                sender=msg["sender"], recipients=msg.get("to"), cc=msg.get("cc"), subject=msg["subject"],
                body=msg["body"], received_at=msg["received_at"], **fields)
        self.store.update_email(email_id, **fields)
        return email_id

    def _ai_failed(self, msg: dict, email_id: int | None, attempts: int, tt: str, error: str, stage: str):
        """Schedule another AI attempt with growing waits (1, 2, 4, 8 ... min). After the last
        attempt, hand the email to a person. Returns the outcome, or None to continue as flagged."""
        attempts += 1
        max_attempts = self.cfg.settings["agent"].get("ai_max_attempts", 5)
        if attempts < max_attempts:
            wait = 2 ** (attempts - 1)
            self._store_inbound(msg, email_id, tt_number=tt, status="ai_retry", ai_attempts=attempts,
                                retry_after=iso(utcnow() + timedelta(minutes=wait)),
                                review_reason=f"AI unavailable during {stage}; retry {attempts}/{max_attempts - 1} "
                                              f"in {wait} min")
            self.store.audit("agent", "ai_retry_scheduled", tt, stage=stage, attempt=attempts, error=error[:300])
            return "retry", attempts
        return None, attempts

    def _process_message(self, msg: dict, email_id: int | None = None, attempts: int = 0) -> str:
        if self.cfg.my_address and msg["sender"] == self.cfg.my_address:
            self._store_inbound(msg, email_id, status="ignored", review_reason="Sent by us")
            return "ignored"

        # Bounce: one of OUR emails did not arrive. The original (with its ticket number) is quoted
        # inside, so search the raw text. A person must look at this.
        if _is_bounce(msg):
            tts = self.cfg.find_ticket_ids(f"{msg['subject']}\n{msg['body']}")
            self._store_inbound(msg, email_id, tt_number=tts[0] if tts else None, status="needs_review",
                                review_reason="Delivery failure: one of our emails did not reach the recipient")
            self.store.audit("agent", "delivery_failure", tts[0] if tts else None, subject=msg["subject"])
            return "flagged"

        # Only the new part of a reply counts; quoted earlier messages are dropped (old ticket
        # numbers in the quoted text must not be matched).
        msg = {**msg, "body": clean_body(msg["body"])}

        # Out-of-office / automatic replies are not a real update: keep them in the history but do
        # not reply and do not count them as activity (that would silence follow-ups).
        if _is_auto_reply(msg):
            tts = self.cfg.find_ticket_ids(f"{msg['subject']}\n{msg['body']}") or \
                [t for t in [self.store.ticket_for_conversation(msg.get("conversation_id"))] if t]
            self._store_inbound(msg, email_id, tt_number=tts[0] if tts else None, status="ignored",
                                review_reason="Automatic reply (out of office)")
            return "ignored"

        tts = self.cfg.find_ticket_ids(f"{msg['subject']}\n{msg['body']}")
        thread_tt = self.store.ticket_for_conversation(msg.get("conversation_id"))
        if not tts and thread_tt:
            tts = [thread_tt]       # reply in a known ticket thread without the number in it
        reasons = []
        if not tts:
            # No standard ticket number: let the AI decide whether (and which) ticket is meant.
            found = self._identify_with_ai(msg, email_id, attempts)
            if isinstance(found, str):          # final outcome (ignored / flagged / retry)
                return found
            tt, note = found
            tts = [tt]
            reasons.append(note)

        tt = tts[0]
        ticket = self.tickets.get(tt)
        if len(tts) > 1:
            reasons.append(f"Email mentions several tickets ({', '.join(tts)}); matched to {tt}")
        if ticket is None:
            reasons.append(f"{tt} not found in Remedy")
        if not self._trusted_sender(msg["sender"]):
            reasons.append(f"Sender {msg['sender']} is not in the escalation list")
        elif ticket is not None:
            # A reply from the Back Office counts as activity on the ticket (resets the quiet timer).
            # Emails from unknown senders do not, so outsiders cannot silence follow-ups.
            last = parse(self.store.ticket_state(tt)["last_inbound_at"])
            if last is None or parse(msg["received_at"]) > last:
                self.store.update_ticket_state(tt, last_inbound_at=msg["received_at"])

        try:
            result = classify_email(self.ai, msg, ticket, self.cfg.settings["agent"]["min_confidence"])
        except AIError as e:
            outcome, attempts = self._ai_failed(msg, email_id, attempts, tt, str(e), "classification")
            if outcome:
                return outcome
            result = {"intent": None, "confidence": None, "needs_human_review": True,
                      "review_reason": f"AI failed {attempts} times: {e}"}
        if result.get("needs_human_review"):
            reasons.append(result["review_reason"])

        review_reason = "; ".join(r for r in reasons if r) or None
        record = dict(tt_number=tt, intent=result.get("intent"), confidence=result.get("confidence"),
                      extracted=result, status="needs_review" if review_reason else "processed",
                      review_reason=review_reason, ai_attempts=attempts, retry_after=None)

        # Decide whether to draft a reply.
        intent = result.get("intent")
        suspicious = review_reason and "suspicious" in review_reason.lower()
        unknown_sender = not self._trusted_sender(msg["sender"])   # a person decides whether to answer
        if ticket is None or suspicious or unknown_sender or intent is None or intent in NO_REPLY_INTENTS:
            email_id = self._store_inbound(msg, email_id, **record)
            self._audit_classified(tt, email_id, result, review_reason)
            return "flagged" if review_reason else "ignored"

        try:
            draft = draft_email(
                self.ai, ticket=ticket, kind="reply", purpose_key=intent,
                history=self.store.ticket_history(tt, self.cfg.settings["history"]["max_emails_in_prompt"]),
                signature=self.cfg.settings["agent"]["signature"], inbound=msg, classification=result,
            )
        except AIError as e:
            outcome, attempts = self._ai_failed(msg, email_id, attempts, tt, str(e), "drafting")
            if outcome:
                return outcome
            record.update(status="needs_review", ai_attempts=attempts,
                          review_reason="; ".join(filter(None, [review_reason, f"Could not draft a reply: {e}"])))
            email_id = self._store_inbound(msg, email_id, **record)
            self._audit_classified(tt, email_id, result, record["review_reason"])
            return "flagged"

        email_id = self._store_inbound(msg, email_id, **record)
        self._audit_classified(tt, email_id, result, review_reason)
        to = [msg["sender"]]
        demo_note = (f"Demo mode: {msg['sender']} is not in escalation.yaml; replying to them anyway"
                     if msg["sender"] not in self.allowed else None)
        notes = [n for n in [review_reason, demo_note, *draft["warnings"]] if n]
        draft_id = self.store.create_draft(
            tt_number=tt, kind="reply", level=1, to_addrs=to, cc_addrs=[], subject=draft["subject"],
            body=draft["body"], reply_to_message_id=msg["id"], source_email_id=email_id,
            needs_attention="; ".join(notes) or None,
        )
        self.store.audit("agent", "draft_created", tt, draft_id=draft_id, kind="reply", intent=intent)
        return "flagged" if review_reason else "drafts"

    _NO_REPLY_SENDER = re.compile(r"^(no-?reply|do-?not-?reply|notifications?|newsletter|news|marketing)[\w.+-]*@|"
                                  r"@(accountprotection\.microsoft\.com|email\.microsoft\.com)$", re.IGNORECASE)

    def _identify_with_ai(self, msg: dict, email_id: int | None, attempts: int):
        """Returns (ticket_number, note) when the AI finds the ticket, otherwise the final outcome."""
        def ignore(reason: str) -> str:
            self._store_inbound(msg, email_id, status="ignored", review_reason=reason, retry_after=None)
            self.store.audit("agent", "email_ignored", subject=msg["subject"], reason=reason)
            return "ignored"

        if not self.cfg.settings["agent"].get("ai_ticket_detection", True):
            return ignore("No ticket number found")
        if self._NO_REPLY_SENDER.search(msg["sender"] or ""):
            return ignore("Automated sender (no-reply / notifications), no ticket number")
        try:
            r = identify_ticket(self.ai, msg, self.tickets.list_open(), self.cfg.settings["agent"]["min_confidence"])
        except AIError as e:
            outcome, attempts = self._ai_failed(msg, email_id, attempts, None, str(e), "ticket identification")
            if outcome:
                return outcome
            self._store_inbound(msg, email_id, status="needs_review", ai_attempts=attempts, retry_after=None,
                                review_reason=f"No ticket number found and AI failed {attempts} times: {e}")
            return "flagged"

        refs = ", ".join(f"'{x}'" for x in r["references"])
        if r["matched_ticket"]:
            how = "number written differently" if r["basis"] == "number" else "site / cell / problem described"
            note = (f"No standard ticket number in the email; AI matched it to {r['matched_ticket']} "
                    f"({how}{': ' + refs if refs else ''}). Please confirm the ticket.")
            self.store.audit("agent", "ticket_identified_by_ai", r["matched_ticket"], references=r["references"],
                             basis=r["basis"], confidence=r["confidence"])
            return r["matched_ticket"], note
        if r["is_ticket_related"]:
            reason = (f"Mentions ticket {refs} but no matching ticket was found in Remedy" if refs else
                      "About a ticket, but the AI could not tell which one") + f" ({r['reason']})"
            email_id = self._store_inbound(msg, email_id, status="needs_review", review_reason=reason,
                                           retry_after=None, ai_attempts=attempts)
            self.store.audit("agent", "ticket_not_identified", subject=msg["subject"], references=r["references"])
            self._draft_ticket_question(msg, email_id, refs)
            return "flagged"
        return ignore(f"Not about a ticket (AI: {r['reason'] or 'no ticket reference'})")

    def _draft_ticket_question(self, msg: dict, email_id: int, refs: str) -> None:
        """Ask the sender which ticket they mean (fixed text, no AI needed). Only for senders we may
        reply to; nothing is sent before a person approves it."""
        if not self._trusted_sender(msg["sender"]) or self.store.has_draft_for_email(email_id):
            return
        mentioned = f" You mentioned {refs}, but we could not find that ticket in Remedy." if refs else ""
        body = ("Dear Team,\n\n"
                f"Thank you for your email.{mentioned} Could you please share the Remedy ticket number "
                "(for example TT-100234) and the Site / Cell ID this update refers to, so we can link it "
                "to the correct ticket?\n\n" + self.cfg.settings["agent"]["signature"].strip())
        subject = msg["subject"] if msg["subject"].lower().startswith("re:") else f"RE: {msg['subject']}"
        draft_id = self.store.create_draft(
            tt_number="-", kind="ask_ticket", level=1, to_addrs=[msg["sender"]], cc_addrs=[], subject=subject,
            body=body, reply_to_message_id=msg["id"], source_email_id=email_id,
            needs_attention="The agent could not tell which ticket this email is about; this asks the sender.")
        self.store.audit("agent", "draft_created", None, draft_id=draft_id, kind="ask_ticket")

    def _audit_classified(self, tt, email_id, result, review_reason) -> None:
        self.store.audit("agent", "email_classified", tt, email_id=email_id, intent=result.get("intent"),
                         confidence=result.get("confidence"), flagged=review_reason)

    def create_manual_reply(self, email_id: int) -> int:
        """A reviewer wants to answer a flagged email by hand. Creates an editable draft that
        goes through the same approval and safety checks as AI drafts."""
        e = self.store.get_email(email_id)
        if e is None or e["direction"] != "in":
            raise ValueError("Email not found")
        if self.store.has_draft_for_email(email_id):
            raise ValueError("A reply draft for this email already exists")
        ticket = self.tickets.get(e["tt_number"]) if e["tt_number"] else None
        # Never reply to an address outside escalation.yaml; use the ticket's team instead.
        to = [e["sender"]] if self._trusted_sender(e["sender"]) else \
            self.cfg.recipients_for(ticket.get("assignment_group") if ticket else None, 1)
        subject = e["subject"] if e["subject"].lower().startswith("re:") else f"RE: {e['subject']}"
        body = f"Dear Team,\n\n\n\n{self.cfg.settings['agent']['signature'].strip()}"
        note = "Written by a person. Fill in the message before approving."
        if e["sender"] not in self.allowed and not self.demo_mode:
            note += f" The original sender ({e['sender']}) is not in escalation.yaml, so it goes to the ticket's team."
        elif e["sender"] not in self.allowed:
            note += f" Demo mode: replying to {e['sender']} although not in escalation.yaml."
        draft_id = self.store.create_draft(
            tt_number=e["tt_number"] or "-", kind="manual", level=1, to_addrs=to, cc_addrs=[], subject=subject,
            body=body, reply_to_message_id=e["message_id"], source_email_id=email_id, needs_attention=note)
        self.store.mark_email_reviewed(email_id)
        self.store.audit("human", "manual_reply_started", e["tt_number"], email_id=email_id, draft_id=draft_id)
        return draft_id

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
                if state["closed"] and not is_closed(ticket["status"], self.cfg.settings):
                    # Ticket re-opened in Remedy: start monitoring again.
                    self.store.update_ticket_state(tt, closed=0)
                    self.store.audit("agent", "monitoring_resumed", tt, status=ticket["status"])
                    state = self.store.ticket_state(tt)

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
            # A waiting reply to the Back Office must not hold back an SLA escalation to management;
            # only another waiting automated email does. Follow-ups/reminders wait for any draft.
            blocking = AUTO_KINDS if action.kind == "escalation" else None
            if self.store.has_pending_draft(tt, blocking):
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
        # Demo mode: a reply may also go back to the person whose email it answers (nobody else).
        demo_ok = set()
        if self.demo_mode and d["kind"] in ("reply", "manual", "ask_ticket") and d["source_email_id"]:
            src = self.store.get_email(d["source_email_id"])
            if src and src["sender"]:
                demo_ok.add(src["sender"].lower())
        not_allowed = [a for a in to + cc if a not in self.allowed and a not in demo_ok]
        if not to:
            raise ValueError("At least one recipient is required")
        if not_allowed:
            raise ValueError(f"Not in escalation.yaml, refusing to send: {', '.join(not_allowed)}")

        ticket = self.tickets.get(d["tt_number"])
        if d["kind"] in AUTO_KINDS and ticket and is_closed(ticket["status"], self.cfg.settings):
            raise ValueError("Ticket is already closed; follow-up not needed")
        # D. never send an empty message
        signature = self.cfg.settings["agent"]["signature"].strip()
        text = body.replace(signature, "").strip()
        if not text or text.rstrip(",").lower() in ("dear team", "hi", "hello"):
            raise ValueError("The message is empty. Write the email before approving.")
        if d["kind"] == "manual" and body.strip() == d["original_body"].strip():
            raise ValueError("Write your message before approving.")

        edited = int(body.strip() != d["original_body"].strip() or subject != d["subject"]
                     or to != d["to_addrs"] or cc != d["cc_addrs"])
        try:
            if d["reply_to_message_id"]:
                try:
                    out_id = self.email.reply(d["reply_to_message_id"], body, to, cc)
                except EmailError as e:
                    if "404" not in str(e) and "ItemNotFound" not in str(e):
                        raise
                    # The email we were replying to was deleted/moved: send as a new email instead.
                    out_id = self.email.send_new(subject, body, to, cc)
                    self.store.audit("agent", "reply_sent_as_new", d["tt_number"], draft_id=draft_id,
                                     reason="original email no longer in mailbox")
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

    def clear_pending_drafts(self) -> int:
        """Reviewer clears every waiting draft at once. Drafts are marked rejected (kept in the
        audit trail, not deleted), so automated ones do not come straight back."""
        drafts = self.store.drafts(status="pending")
        for d in drafts:
            self.reject_draft(d["id"], "Cleared by reviewer (clear all)")
        # Pause automated emails for every cleared ticket (also those whose cleared draft was a reply),
        # so nothing pops up right after clearing.
        hours = self.cfg.settings["followup"].get("snooze_after_reject_hours", 12)
        until = iso(utcnow() + timedelta(hours=hours))
        for tt in {d["tt_number"] for d in drafts}:
            self.store.update_ticket_state(tt, snoozed_until=until)
        if drafts:
            self.store.audit("human", "drafts_cleared", count=len(drafts))
        return len(drafts)

    def reject_draft(self, draft_id: int, reason: str = "") -> None:
        d = self.store.get_draft(draft_id)
        if d is None or d["status"] != "pending":
            raise ValueError("Draft is not pending")
        self.store.update_draft(draft_id, status="rejected", decided_at=iso(utcnow()), error=reason or None)
        self.store.audit("human", "draft_rejected", d["tt_number"], draft_id=draft_id, reason=reason)
        if d["kind"] in AUTO_KINDS:
            # Otherwise the same follow-up would be drafted again on the next run (every few minutes).
            hours = self.cfg.settings["followup"].get("snooze_after_reject_hours", 12)
            self.store.update_ticket_state(d["tt_number"], snoozed_until=iso(utcnow() + timedelta(hours=hours)))
            self.store.audit("agent", "ticket_snoozed", d["tt_number"], hours=hours, reason="draft rejected")


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
    """Apply the SLA matrix (settings.yaml) to one ticket. Returns what to do next, or None."""
    rules = settings["followup"]
    status = (ticket.get("status") or "").strip()
    row = sla_row(settings, ticket.get("priority"))
    min_gap = rules["min_hours_between_followups"]

    last_out = parse(state.get("last_outbound_at"))
    hours_since_out = hours_between(now, last_out) if last_out else float("inf")

    # Ticket closed -> stop active follow-up
    if is_closed(status, settings):
        return Action("close", reason="Ticket closed")

    # Rejected tickets need a human decision, not automated chasing
    if status.lower() == "rejected":
        return Action("flag", reason="Ticket rejected in Remedy")

    # A reviewer rejected our last automated email: stay quiet until the snooze ends.
    snoozed = parse(state.get("snoozed_until"))
    if snoozed and now < snoozed:
        return None

    # Resolved but not closed -> closure check
    if status.lower() == "resolved":
        resolved_at = parse(ticket.get("last_update_at")) or parse(ticket.get("created_at"))
        if resolved_at is None:
            return None
        waited = hours_between(now, resolved_at)
        if waited >= rules["resolved_not_closed_hours"] and hours_since_out >= min_gap:
            return Action("closure_check", 1, f"Resolved {waited:.0f}h ago but not closed")
        return None

    sla_due = sla_deadline(ticket, settings)

    # SLA breached -> escalate per the matrix: team lead, then manager, then keep chasing the manager.
    if sla_due and now > sla_due:
        overdue = hours_between(now, sla_due)
        level_now = state.get("escalation_level") or 0
        max_level = rules["max_escalation_level"]
        last_esc = parse(state.get("last_escalation_at"))
        since_esc = hours_between(now, last_esc) if last_esc else float("inf")
        if level_now < 2:
            if overdue >= row["team_lead_after"]:
                return Action("escalation", min(2, max_level),
                              f"SLA breached {overdue:.1f}h ago (team lead after {row['team_lead_after']}h)")
        elif level_now < max_level:
            # Next level once the matrix says so, but never right after the previous escalation.
            if overdue >= row["manager_after"] and since_esc >= min_gap:
                return Action("escalation", level_now + 1,
                              f"SLA breached {overdue:.1f}h ago (manager after {row['manager_after']}h)")
        elif since_esc >= row["repeat_every"]:
            return Action("escalation", max_level,
                          f"SLA breached {overdue:.1f}h ago; still open, repeating every {row['repeat_every']}h")
        return None

    # SLA approaching -> one-time reminder
    if sla_due and not state.get("reminder_sent"):
        left = hours_between(sla_due, now)
        if left <= row["remind_before"] and hours_since_out >= min_gap:
            return Action("reminder", 1, f"SLA due in {left:.1f}h (remind {row['remind_before']}h before)")

    # No update within the matrix interval -> follow-up
    activity = [a for a in (parse(ticket.get("created_at")), parse(ticket.get("last_update_at")),
                            parse(state.get("last_inbound_at")), last_out) if a]
    if not activity:
        return None
    last_activity = max(activity)
    quiet = hours_between(now, last_activity)
    if quiet >= row["followup_if_quiet"] and hours_since_out >= min_gap:
        return Action("followup", 1, f"No update for {quiet:.0f}h (follow up after {row['followup_if_quiet']}h)")
    return None
