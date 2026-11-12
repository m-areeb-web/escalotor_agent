"""Background worker used by `python main.py start` (and `ui`).

  - checks the inbox every few seconds (agent.inbox_watch_seconds) and processes new emails
  - runs the follow-up / SLA / escalation rules every agent.poll_interval_minutes
  - publishes a live status that the approval page shows

Only one agent process may do this at a time (see AgentLock), so two windows can never
process the same email twice.

Note: true push (Microsoft Graph webhooks) needs a public HTTPS server; for the POC a short
polling interval gives practically the same result.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import timedelta
from pathlib import Path

from .timeutil import iso, utcnow


class AgentLock:
    """Cross-platform "only one running agent" lock on data/.agent.lock.
    Released automatically by the OS if the process exits or crashes."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        _HELD_LOCKS.append(self)   # keep the file open for the life of the process
        return True


_HELD_LOCKS: list[AgentLock] = []


class AgentWorker:
    def __init__(self, agent, *, inbox_seconds: int, followup_minutes: float | None, lock: threading.Lock):
        self.agent = agent
        self.inbox_interval = max(3, int(inbox_seconds))
        self.followup_interval = followup_minutes * 60 if followup_minutes else None
        self.lock = lock                  # shared with buttons / approvals in the UI
        self._wake = threading.Event()
        self._followups_now = threading.Event()
        self._last_followups = 0.0
        self._state_lock = threading.Lock()
        self._state = {
            "running": False,
            "phase": "starting",          # starting | checking | processing | followups | idle | error | off
            "message": "Starting…",
            "done": 0, "total": 0, "subject": None,
            "last_checked": None, "last_result": None, "error": None,
            "last_followups": None, "next_followups": None, "followups_result": None,
            "waiting_for_ai": 0,
            "version": 0,                 # goes up whenever something new appears on the page
            "interval": self.inbox_interval,
            "followups_enabled": self.followup_interval is not None,
        }

    # ---- status for the page ------------------------------------------------
    def _set(self, **kw) -> None:
        with self._state_lock:
            self._state.update(kw)

    def _bump(self) -> None:
        with self._state_lock:
            self._state["version"] += 1

    def status(self) -> dict:
        with self._state_lock:
            return dict(self._state)

    # ---- control ------------------------------------------------------------
    def start(self) -> None:
        self._set(running=True)
        threading.Thread(target=self._loop, name="agent-worker", daemon=True).start()

    def disabled(self, reason: str) -> None:
        self._set(running=False, phase="off", message=reason)

    def check_now(self) -> None:
        self._wake.set()

    def followups_now(self) -> None:
        self._followups_now.set()
        self._wake.set()

    def _progress(self, done: int, total: int, subject: str) -> None:
        self._set(phase="processing", done=done, total=total, subject=subject,
                  message=f"Fetching {total} new email{'s' if total != 1 else ''}…")

    # ---- loop ---------------------------------------------------------------
    def _loop(self) -> None:
        while True:
            self._check_inbox()
            if self.followup_interval is not None or self._followups_now.is_set():
                due = time.monotonic() - self._last_followups >= (self.followup_interval or float("inf"))
                if due or self._followups_now.is_set() or not self._last_followups:
                    self._followups_now.clear()
                    self._run_followups()
            self._wake.wait(self.inbox_interval)
            self._wake.clear()

    def _check_inbox(self) -> None:
        self._set(phase="checking", message="Checking inbox…")
        try:
            with self.lock:
                stats = self.agent.process_inbox(progress=self._progress)
            changed = stats["new"] or stats["retried"]
            self._set(phase="idle", error=None, last_checked=iso(utcnow()), done=0, total=0, subject=None,
                      message="Watching inbox", waiting_for_ai=self.agent.store.count_waiting_for_ai(),
                      **({"last_result": stats} if changed else {}))
            if changed:
                self._bump()
        except Exception as e:   # keep watching through network / sign-in hiccups
            self._set(phase="error", error=str(e)[:300], last_checked=iso(utcnow()),
                      message="Inbox check failed, retrying")
            self.agent.store.audit("agent", "inbox_check_failed", error=str(e)[:300])

    def _run_followups(self) -> None:
        self._set(phase="followups", message="Running follow-up rules…", done=0, total=0,
                  subject="Checking open tickets for missing updates and SLA deadlines")
        try:
            with self.lock:
                stats = self.agent.run_followups()
            nxt = iso(utcnow() + timedelta(seconds=self.followup_interval)) if self.followup_interval else None
            self._set(phase="idle", message="Watching inbox", error=None, subject=None,
                      last_followups=iso(utcnow()), next_followups=nxt, followups_result=stats)
            if stats["drafts"] or stats["closed"]:
                self._bump()
        except Exception as e:
            self._set(phase="error", error=str(e)[:300], message="Follow-up rules failed, retrying")
            self.agent.store.audit("agent", "followups_failed", error=str(e)[:300])
        finally:
            self._last_followups = time.monotonic()
