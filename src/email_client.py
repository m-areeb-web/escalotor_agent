"""Email clients (SRS FR-01).

  mock    - reads data/mock_inbox.json, "sends" to data/mock_outbox.jsonl. No account needed.
  outlook - Microsoft Graph API with your POC Outlook account (device-code sign-in).

Both return messages as plain dicts:
  {id, conversation_id, subject, body, sender, to, cc, received_at}
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import requests

from .config import Config
from .timeutil import iso, parse, utcnow

GRAPH = "https://graph.microsoft.com/v1.0"
# Least privilege: read the mailbox and send as the signed-in user. Nothing else.
SCOPES = ["Mail.ReadWrite", "Mail.Send"]


class EmailError(RuntimeError):
    pass


class MockEmailClient:
    name = "mock"

    def __init__(self, root: Path):
        self.inbox = root / "data" / "mock_inbox.json"
        self.outbox = root / "data" / "mock_outbox.jsonl"

    def _read_inbox(self) -> list[dict]:
        if not self.inbox.exists():
            return []
        with open(self.inbox, encoding="utf-8") as f:
            return json.load(f)

    def fetch_recent(self, limit: int = 50) -> list[dict]:
        return sorted(self._read_inbox(), key=lambda m: m["received_at"])[-limit:]

    def fetch_since(self, since: str | None, max_messages: int = 1000) -> list[dict]:
        msgs = sorted(self._read_inbox(), key=lambda m: m["received_at"])
        if since:
            cutoff = parse(since)
            msgs = [m for m in msgs if parse(m["received_at"]) >= cutoff]
        return msgs[:max_messages]

    def add_message(self, sender: str, subject: str, body: str, to: list[str] | None = None) -> dict:
        msgs = self._read_inbox()
        msg = {
            "id": f"mock-{uuid.uuid4().hex[:10]}", "conversation_id": None, "subject": subject, "body": body,
            "sender": sender.lower(), "to": to or [], "cc": [], "received_at": iso(utcnow()),
        }
        msgs.append(msg)
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        with open(self.inbox, "w", encoding="utf-8") as f:
            json.dump(msgs, f, indent=2)
        return msg

    def _write_out(self, record: dict) -> str:
        record["id"] = f"mock-out-{uuid.uuid4().hex[:10]}"
        record["sent_at"] = iso(utcnow())
        with open(self.outbox, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return record["id"]

    def reply(self, message_id: str, body: str, to: list[str], cc: list[str]) -> str:
        return self._write_out({"type": "reply", "in_reply_to": message_id, "to": to, "cc": cc, "body": body})

    def send_new(self, subject: str, body: str, to: list[str], cc: list[str]) -> str:
        return self._write_out({"type": "new", "subject": subject, "to": to, "cc": cc, "body": body})


class OutlookEmailClient:
    name = "outlook"

    def __init__(self, client_id: str, root: Path):
        if not client_id:
            raise EmailError("MS_CLIENT_ID is empty. Register an Azure app first (see README).")
        import msal

        self.cache_path = root / "data" / ".msal_token_cache.json"
        self.cache = msal.SerializableTokenCache()
        if self.cache_path.exists():
            self.cache.deserialize(self.cache_path.read_text())
        # "consumers" = personal Microsoft accounts (outlook.com / hotmail). Use the client's
        # tenant ID here when moving to the enterprise mailbox.
        self.app = msal.PublicClientApplication(
            client_id, authority="https://login.microsoftonline.com/consumers", token_cache=self.cache
        )

    def _save_cache(self) -> None:
        if self.cache.has_state_changed:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(self.cache.serialize())
            self.cache_path.chmod(0o600)

    def login(self) -> str:
        """Interactive device-code sign-in. Run once; the token is then refreshed silently."""
        flow = self.app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise EmailError(f"Could not start sign-in: {flow.get('error_description', flow)}")
        print(flow["message"])
        result = self.app.acquire_token_by_device_flow(flow)
        self._save_cache()
        if "access_token" not in result:
            raise EmailError(f"Sign-in failed: {result.get('error_description', result)}")
        return result.get("id_token_claims", {}).get("preferred_username", "signed in")

    def _token(self) -> str:
        accounts = self.app.get_accounts()
        result = self.app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
        self._save_cache()
        if not result or "access_token" not in result:
            raise EmailError("Not signed in to Outlook. Run: python main.py login")
        return result["access_token"]

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._token()}"
        r = requests.request(method, f"{GRAPH}{path}", headers=headers, timeout=60, **kwargs)
        if r.status_code >= 400:
            raise EmailError(f"Graph API {method} {path} failed: {r.status_code} {r.text[:300]}")
        return r

    _SELECT = "id,conversationId,subject,body,from,toRecipients,ccRecipients,receivedDateTime"
    # Ask for plain-text bodies so the AI doesn't have to read HTML.
    _TEXT = {"Prefer": 'outlook.body-content-type="text"'}

    @staticmethod
    def _to_msg(m: dict) -> dict:
        return {
            "id": m["id"],
            "conversation_id": m.get("conversationId"),
            "subject": m.get("subject") or "",
            "body": (m.get("body") or {}).get("content", ""),
            "sender": ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower(),
            "to": [x["emailAddress"]["address"].lower() for x in m.get("toRecipients", [])],
            "cc": [x["emailAddress"]["address"].lower() for x in m.get("ccRecipients", [])],
            "received_at": m["receivedDateTime"],
        }

    def fetch_recent(self, limit: int = 50) -> list[dict]:
        r = self._request("GET", "/me/mailFolders/inbox/messages", headers=dict(self._TEXT), params={
            "$top": str(limit), "$orderby": "receivedDateTime desc", "$select": self._SELECT})
        return list(reversed([self._to_msg(m) for m in r.json().get("value", [])]))  # oldest first

    def fetch_since(self, since: str | None, max_messages: int = 1000) -> list[dict]:
        """Every inbox email received at or after `since`, oldest first, following all result
        pages so nothing is missed even if many emails arrived while the agent was off."""
        if not since:
            return self.fetch_recent(50)
        stamp = parse(since).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"$filter": f"receivedDateTime ge {stamp}", "$orderby": "receivedDateTime asc",
                  "$top": "50", "$select": self._SELECT}
        url, msgs = "/me/mailFolders/inbox/messages", []
        while url and len(msgs) < max_messages:
            r = self._request("GET", url, headers=dict(self._TEXT), params=params)
            data = r.json()
            msgs.extend(self._to_msg(m) for m in data.get("value", []))
            nxt = data.get("@odata.nextLink")
            url, params = (nxt[len(GRAPH):], None) if nxt else (None, None)
        return msgs[:max_messages]

    @staticmethod
    def _addrs(addresses: list[str]) -> list[dict]:
        return [{"emailAddress": {"address": a}} for a in addresses]

    def reply(self, message_id: str, body: str, to: list[str], cc: list[str]) -> str:
        # Replies inside the original conversation thread. Recipients are set explicitly
        # from our allow-list rather than trusting the original email's reply-to headers.
        self._request("POST", f"/me/messages/{message_id}/reply", json={
            "message": {"toRecipients": self._addrs(to), "ccRecipients": self._addrs(cc)},
            "comment": body,
        })
        return f"outlook-out-{uuid.uuid4().hex[:10]}"

    def send_new(self, subject: str, body: str, to: list[str], cc: list[str]) -> str:
        self._request("POST", "/me/sendMail", json={
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": self._addrs(to),
                "ccRecipients": self._addrs(cc),
            },
            "saveToSentItems": True,
        })
        return f"outlook-out-{uuid.uuid4().hex[:10]}"


def get_email_client(cfg: Config):
    if cfg.email_provider == "mock":
        return MockEmailClient(cfg.root)
    if cfg.email_provider == "outlook":
        return OutlookEmailClient(cfg.ms_client_id, cfg.root)
    raise EmailError(f"Unknown EMAIL_PROVIDER '{cfg.email_provider}'. Use mock or outlook.")
