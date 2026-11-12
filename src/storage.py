"""SQLite storage: email history (FR-05), drafts awaiting approval, ticket follow-up
state, and the audit log (NFR-02)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .timeutil import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT UNIQUE NOT NULL,      -- provider message id
    conversation_id TEXT,
    tt_number       TEXT,
    direction       TEXT NOT NULL,             -- in | out
    sender          TEXT,
    recipients      TEXT,                      -- JSON list
    cc              TEXT,                      -- JSON list
    subject         TEXT,
    body            TEXT,
    received_at     TEXT,
    intent          TEXT,
    confidence      REAL,
    extracted       TEXT,                      -- JSON from the classifier
    status          TEXT NOT NULL DEFAULT 'processed',  -- processed | ignored | needs_review | reviewed | ai_retry
    review_reason   TEXT,
    ai_attempts     INTEGER NOT NULL DEFAULT 0,   -- failed AI calls so far (retried automatically)
    retry_after     TEXT                          -- when status=ai_retry: earliest next attempt
);
CREATE INDEX IF NOT EXISTS idx_emails_tt ON emails(tt_number);

CREATE TABLE IF NOT EXISTS drafts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tt_number           TEXT NOT NULL,
    kind                TEXT NOT NULL,         -- reply | followup | reminder | escalation | closure_check
    level               INTEGER NOT NULL DEFAULT 1,
    to_addrs            TEXT NOT NULL,         -- JSON list
    cc_addrs            TEXT NOT NULL,         -- JSON list
    subject             TEXT NOT NULL,
    body                TEXT NOT NULL,
    original_body       TEXT NOT NULL,         -- what the AI wrote, kept to measure edits
    reply_to_message_id TEXT,                  -- set when replying in a thread
    source_email_id     INTEGER,               -- inbound email that triggered it
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | rejected | failed
    needs_attention     TEXT,                  -- warning shown to the reviewer
    created_at          TEXT NOT NULL,
    decided_at          TEXT,
    edited              INTEGER NOT NULL DEFAULT 0,
    error               TEXT
);
CREATE INDEX IF NOT EXISTS idx_drafts_tt ON drafts(tt_number);

CREATE TABLE IF NOT EXISTS ticket_state (
    tt_number          TEXT PRIMARY KEY,
    escalation_level   INTEGER NOT NULL DEFAULT 0,
    reminder_sent      INTEGER NOT NULL DEFAULT 0,
    last_inbound_at    TEXT,
    last_outbound_at   TEXT,
    last_escalation_at TEXT,
    last_status        TEXT,
    closed             INTEGER NOT NULL DEFAULT 0,
    snoozed_until      TEXT                 -- no automated drafts before this (set when a draft is rejected)
);

CREATE TABLE IF NOT EXISTS meta (           -- small key/value store, e.g. inbox position
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,                   -- agent | human
    action    TEXT NOT NULL,
    tt_number TEXT,
    details   TEXT                             -- JSON
);
"""

AUTO_KINDS = ("followup", "reminder", "escalation", "closure_check")


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            # Upgrade databases created by older versions of the POC.
            cols = {r["name"] for r in c.execute("PRAGMA table_info(emails)")}
            if "ai_attempts" not in cols:
                c.execute("ALTER TABLE emails ADD COLUMN ai_attempts INTEGER NOT NULL DEFAULT 0")
            if "retry_after" not in cols:
                c.execute("ALTER TABLE emails ADD COLUMN retry_after TEXT")
            tcols = {r["name"] for r in c.execute("PRAGMA table_info(ticket_state)")}
            if "snoozed_until" not in tcols:
                c.execute("ALTER TABLE ticket_state ADD COLUMN snoozed_until TEXT")

    # ---- meta ----------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- audit ---------------------------------------------------------------
    def audit(self, actor: str, action: str, tt_number: str | None = None, **details) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO audit_log (ts, actor, action, tt_number, details) VALUES (?,?,?,?,?)",
                (iso(utcnow()), actor, action, tt_number, json.dumps(details, default=str)),
            )

    def audit_entries(self, tt_number: str | None = None, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            if tt_number:
                rows = c.execute(
                    "SELECT * FROM audit_log WHERE tt_number=? ORDER BY id DESC LIMIT ?", (tt_number, limit)
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- emails --------------------------------------------------------------
    def email_id_for_message(self, message_id: str) -> int | None:
        with self._conn() as c:
            row = c.execute("SELECT id FROM emails WHERE message_id=?", (message_id,)).fetchone()
        return row["id"] if row else None

    def email_seen(self, message_id: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM emails WHERE message_id=?", (message_id,)).fetchone() is not None

    def save_email(self, *, message_id, direction, sender, recipients, cc, subject, body, received_at,
                   conversation_id=None, tt_number=None, intent=None, confidence=None, extracted=None,
                   status="processed", review_reason=None, ai_attempts=0, retry_after=None) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO emails (message_id, conversation_id, tt_number, direction, sender, recipients,
                   cc, subject, body, received_at, intent, confidence, extracted, status, review_reason,
                   ai_attempts, retry_after)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (message_id, conversation_id, tt_number, direction, sender, json.dumps(recipients or []),
                 json.dumps(cc or []), subject, body, received_at, intent, confidence,
                 json.dumps(extracted) if extracted is not None else None, status, review_reason,
                 ai_attempts, retry_after),
            )
            return cur.lastrowid

    def update_email(self, email_id: int, **fields) -> None:
        if "extracted" in fields and fields["extracted"] is not None:
            fields["extracted"] = json.dumps(fields["extracted"])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE emails SET {cols} WHERE id=?", (*fields.values(), email_id))

    def emails_due_for_retry(self, now_iso: str) -> list[dict]:
        """Emails whose AI step failed earlier and are due for another attempt."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM emails WHERE status='ai_retry' AND (retry_after IS NULL OR retry_after <= ?) "
                "ORDER BY id", (now_iso,)).fetchall()
        return [_email_row(r) for r in rows]

    def count_waiting_for_ai(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM emails WHERE status='ai_retry'").fetchone()[0]

    def ticket_for_conversation(self, conversation_id: str | None) -> str | None:
        """Ticket number already linked to this email thread, if any."""
        if not conversation_id:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT tt_number FROM emails WHERE conversation_id=? AND tt_number IS NOT NULL ORDER BY id DESC LIMIT 1",
                (conversation_id,)).fetchone()
        return row["tt_number"] if row else None

    def get_email(self, email_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
        return _email_row(row) if row else None

    def ticket_history(self, tt_number: str, limit: int | None = None) -> list[dict]:
        """Chronological communication history for a ticket (FR-05)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM emails WHERE tt_number=? ORDER BY received_at ASC, id ASC", (tt_number,)
            ).fetchall()
        history = [_email_row(r) for r in rows]
        return history[-limit:] if limit else history

    def emails_needing_review(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM emails WHERE status='needs_review' ORDER BY id DESC").fetchall()
        return [_email_row(r) for r in rows]

    def mark_email_reviewed(self, email_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE emails SET status='reviewed' WHERE id=?", (email_id,))

    # ---- drafts --------------------------------------------------------------
    def create_draft(self, *, tt_number, kind, level, to_addrs, cc_addrs, subject, body,
                     reply_to_message_id=None, source_email_id=None, needs_attention=None) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO drafts (tt_number, kind, level, to_addrs, cc_addrs, subject, body, original_body,
                   reply_to_message_id, source_email_id, needs_attention, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tt_number, kind, level, json.dumps(to_addrs), json.dumps(cc_addrs), subject, body, body,
                 reply_to_message_id, source_email_id, needs_attention, iso(utcnow())),
            )
            return cur.lastrowid

    def get_draft(self, draft_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        return _draft_row(row) if row else None

    def drafts(self, status: str | None = None, tt_number: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM drafts WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if tt_number:
            sql += " AND tt_number=?"
            args.append(tt_number)
        with self._conn() as c:
            rows = c.execute(sql + " ORDER BY id DESC", args).fetchall()
        return [_draft_row(r) for r in rows]

    def has_pending_draft(self, tt_number: str, kinds: tuple[str, ...] | None = None) -> bool:
        """Duplicate prevention (NFR-07): while a draft for a ticket awaits a human decision,
        the follow-up engine does not create another one. `kinds` limits which drafts count."""
        sql, args = "SELECT 1 FROM drafts WHERE tt_number=? AND status='pending'", [tt_number]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        with self._conn() as c:
            return c.execute(sql, args).fetchone() is not None

    def has_draft_for_email(self, email_id: int) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM drafts WHERE source_email_id=?", (email_id,)).fetchone() is not None

    def update_draft(self, draft_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE drafts SET {cols} WHERE id=?", (*fields.values(), draft_id))

    def approval_stats(self) -> dict:
        with self._conn() as c:
            sent = c.execute("SELECT COUNT(*) FROM drafts WHERE status='sent'").fetchone()[0]
            unedited = c.execute("SELECT COUNT(*) FROM drafts WHERE status='sent' AND edited=0").fetchone()[0]
            rejected = c.execute("SELECT COUNT(*) FROM drafts WHERE status='rejected'").fetchone()[0]
            pending = c.execute("SELECT COUNT(*) FROM drafts WHERE status='pending'").fetchone()[0]
        decided = sent + rejected
        return {
            "sent": sent, "unedited": unedited, "rejected": rejected, "pending": pending,
            # Share of AI drafts sent with no human edits: the evidence for enabling auto-send later.
            "approved_without_edits_pct": round(100 * unedited / decided) if decided else None,
        }

    # ---- ticket follow-up state ---------------------------------------------
    def ticket_state(self, tt_number: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT * FROM ticket_state WHERE tt_number=?", (tt_number,)).fetchone()
            if row is None:
                c.execute("INSERT INTO ticket_state (tt_number) VALUES (?)", (tt_number,))
                row = c.execute("SELECT * FROM ticket_state WHERE tt_number=?", (tt_number,)).fetchone()
        return dict(row)

    def update_ticket_state(self, tt_number: str, **fields) -> None:
        self.ticket_state(tt_number)  # ensure row exists
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE ticket_state SET {cols} WHERE tt_number=?", (*fields.values(), tt_number))


def _email_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["recipients"] = json.loads(d["recipients"] or "[]")
    d["cc"] = json.loads(d["cc"] or "[]")
    d["extracted"] = json.loads(d["extracted"]) if d["extracted"] else None
    return d


def _draft_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["to_addrs"] = json.loads(d["to_addrs"])
    d["cc_addrs"] = json.loads(d["cc_addrs"])
    return d
