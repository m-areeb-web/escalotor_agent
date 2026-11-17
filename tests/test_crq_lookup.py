"""CRQ check: recent planned work on the ticket's site, from the CRQ Analyst (faked here, offline)."""

import copy
from dataclasses import replace
from datetime import timedelta

import pytest
import requests

from src.agent import Action, Agent
from src.ai_provider import RulesProvider
from src.approval_ui import create_app
from src.config import load_config
from src.crq_lookup import CRQLookup, crq_line, site_of
from src.email_client import MockEmailClient
from src.storage import Storage
from src.ticket_source import get_ticket_source, seed_fake_tickets
from src.timeutil import parse

CRQS = [
    {"crq_id": "CRQ000000000002", "change_type": "TCN", "activity": "Link Re-routing", "status": "Closed",
     "activity_result": "ROLLBACK", "sched_start": "2026-11-10T00:00:00", "sched_end": "2026-11-12T23:59:00"},
    {"crq_id": "CRQ000000000001", "change_type": "MDT", "activity": "SW / Capacity upgrade",
     "status": "Request For Authorization", "activity_result": None,
     "sched_start": "2026-11-12T00:00:00", "sched_end": "2026-11-12T06:00:00"},
]


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self.data


class FakeCRQServer:
    """Stands in for requests.Session against the CRQ Analyst's /api/site_crqs."""

    def __init__(self, crqs=CRQS, fail=False, unknown=12):
        self.crqs, self.fail, self.unknown, self.calls = crqs, fail, unknown, []

    def get(self, url, timeout, params):
        self.calls.append({"url": url, "timeout": timeout, **params})
        if self.fail:
            raise requests.ConnectionError("connection refused")
        return FakeResponse({"site_id": params["site"], "total_matches": len(self.crqs),
                             "crqs_with_unknown_sites_in_window": self.unknown, "crqs": self.crqs})


TICKET = {"tt_number": "TT-100234", "site_id": "W5634", "cell_id": "W5634_2",
          "created_at": "2026-11-17T12:00:00+00:00"}


def settings(**crq):
    return {"crq": {"enabled": True, "lookback_days": 7, "add_note_to_emails": True, **crq}}


# ---- site and window -------------------------------------------------------
def test_site_from_site_or_cell():
    assert site_of({"site_id": "W6102"}) == "W6102"
    assert site_of({"site_id": "w6102_1"}) == "W6102"          # a cell given as site
    assert site_of({"site_id": None, "cell_id": "W6102_3"}) == "W6102"
    assert site_of({"site_id": "6102"}) == "6102"
    assert site_of({"site_id": "LHR-0451"}) is None             # not a CRQ site format
    assert site_of({}) is None


def test_window_is_lookback_before_ticket_until_now():
    server = FakeCRQServer()
    now = parse("2026-11-18T09:00:00+00:00")
    CRQLookup(settings(lookback_days=7), session=server).for_ticket(TICKET, now)
    call = server.calls[0]
    assert call["site"] == "W5634"
    assert call["start"] == "2026-11-10T12:00:00+00:00" and call["end"] == "2026-11-18T09:00:00+00:00"
    assert call["url"] == "http://127.0.0.1:8765/api/site_crqs"


# ---- results, failures, caching -------------------------------------------
def test_result_shape():
    r = CRQLookup(settings(), session=FakeCRQServer()).for_ticket(TICKET)
    assert r["site"] == "W5634" and r["total"] == 2 and r["unknown_site_crqs"] == 12
    assert [c["crq_id"] for c in r["crqs"]] == ["CRQ000000000002", "CRQ000000000001"]


def test_failure_returns_none_and_is_not_retried_immediately():
    server = FakeCRQServer(fail=True)
    lk = CRQLookup(settings(), session=server)
    assert lk.for_ticket(TICKET) is None and "ConnectionError" in lk.last_error
    assert lk.for_ticket(TICKET) is None
    assert len(server.calls) == 1               # second call skipped: CRQ side just failed


def test_repeat_lookups_are_cached():
    server = FakeCRQServer()
    lk = CRQLookup(settings(), session=server)
    now = parse("2026-11-18T09:00:00+00:00")
    lk.for_ticket(TICKET, now), lk.for_ticket(TICKET, now + timedelta(minutes=5))
    assert len(server.calls) == 1


def test_disabled_or_no_site_makes_no_call():
    server = FakeCRQServer()
    assert CRQLookup(settings(enabled=False), session=server).for_ticket(TICKET) is None
    assert CRQLookup(settings(), session=server).for_ticket({**TICKET, "site_id": "", "cell_id": ""}) is None
    assert CRQLookup(settings(), session=server).for_ticket({**TICKET, "created_at": None}) is None
    assert server.calls == []


# ---- email note ------------------------------------------------------------
def test_email_note_lists_crqs_and_caps_length():
    lk = CRQLookup(settings(max_in_email=1), session=FakeCRQServer())
    note = lk.email_note(lk.for_ticket(TICKET))
    assert note.startswith("Planned work check: 2 CRQ(s) on site W5634 since ")
    assert "CRQ000000000002 (TCN, Link Re-routing), 10 Nov 2026 to 12 Nov 2026, result ROLLBACK" in note
    assert "CRQ000000000001" not in note and "- and 1 more" in note
    assert note.endswith("Please check whether any of these changes is related to this issue.")


def test_no_note_when_nothing_found_or_unavailable():
    lk = CRQLookup(settings(), session=FakeCRQServer(crqs=[]))
    assert lk.email_note(lk.for_ticket(TICKET)) is None
    assert lk.email_note(None) is None


def test_crq_line_without_result_shows_status():
    assert crq_line(CRQS[1]) == ("CRQ000000000001 (MDT, SW / Capacity upgrade), 12 Nov 2026, "
                                 "Request For Authorization")


# ---- inside the agent ------------------------------------------------------
@pytest.fixture
def cfg(tmp_path):
    base = load_config()
    s = copy.deepcopy(base.settings)
    s["agent"]["demo_reply_to_any_sender"] = False
    s["crq"] = {**s.get("crq", {}), "enabled": True, "add_note_to_emails": True}
    c = replace(base, settings=s, root=tmp_path, ai_provider="rules", email_provider="mock",
                my_address="team@test.local")
    seed_fake_tickets(c.tickets_path)
    return c


def make_agent(cfg, server):
    return Agent(cfg, storage=Storage(cfg.db_path), ai=RulesProvider(), email=MockEmailClient(cfg.root),
                 tickets=get_ticket_source(cfg), crq=CRQLookup(cfg.settings, session=server))


def test_reply_draft_gets_note_before_signature(cfg):
    a = make_agent(cfg, FakeCRQServer())
    bo = cfg.recipients_for("Back Office RF", 1)[0]
    a.email.add_message(bo, "RE: TT-100234", "We are investigating TT-100234, ETA 2026-11-20.")
    a.process_inbox()
    body = a.store.drafts(status="pending")[0]["body"]
    sig = cfg.settings["agent"]["signature"].strip()
    assert "Planned work check: 2 CRQ(s) on site W5634" in body
    assert body.index("Planned work check") < body.index(sig)


def test_escalation_gets_note_but_closure_check_does_not(cfg):
    a = make_agent(cfg, FakeCRQServer())
    t = a.tickets.get("TT-100236")
    assert a._create_auto_draft(t, Action("escalation", 2, "SLA breached"))
    assert "Planned work check" in a.store.drafts(tt_number="TT-100236")[0]["body"]
    t = a.tickets.get("TT-100237")
    assert a._create_auto_draft(t, Action("closure_check", 1, "Resolved"))
    assert "Planned work check" not in a.store.drafts(tt_number="TT-100237")[0]["body"]


def test_note_off_by_setting(cfg):
    cfg.settings["crq"]["add_note_to_emails"] = False
    server = FakeCRQServer()
    a = make_agent(cfg, server)
    assert a._create_auto_draft(a.tickets.get("TT-100236"), Action("escalation", 2, "SLA breached"))
    assert "Planned work check" not in a.store.drafts(tt_number="TT-100236")[0]["body"]
    assert server.calls == []


def test_crq_side_down_still_drafts_and_audits(cfg):
    a = make_agent(cfg, FakeCRQServer(fail=True))
    assert a._create_auto_draft(a.tickets.get("TT-100236"), Action("escalation", 2, "SLA breached"))
    assert "Planned work check" not in a.store.drafts(tt_number="TT-100236")[0]["body"]
    assert any(e["action"] == "crq_lookup_failed" for e in a.store.audit_entries("TT-100236"))


# ---- on the page -----------------------------------------------------------
def page(cfg, server, path):
    app = create_app(cfg, make_agent(cfg, server), watch=False)
    return app.test_client().get(path).get_data(as_text=True)


def test_ticket_page_shows_crqs_failed_highlighted(cfg):
    html = page(cfg, FakeCRQServer(), "/ticket/TT-100234")
    assert "Recent CRQs on site W5634" in html and "CRQ000000000002" in html
    assert "1 FAILED/ROLLBACK" in html
    # TT-100234 is a call-drop ticket: the SW upgrade CRQ is marked, with its reason
    assert "Reported issue type: Call drops" in html and "1 match the reported issue" in html
    assert "Call drops: sw work" in html
    assert "12 CRQs in this period list their sites only in an attachment" in html


def test_ticket_page_says_when_nothing_found(cfg):
    html = page(cfg, FakeCRQServer(crqs=[], unknown=0), "/ticket/TT-100236")
    assert "No CRQs found on this site in this period." in html


def test_pages_work_when_crq_side_is_down(cfg):
    html = page(cfg, FakeCRQServer(fail=True), "/ticket/TT-100234")
    assert "TT-100234" in html and "CRQ check unavailable" in html


def test_ticket_page_without_box_when_setting_off(cfg):
    cfg.settings["crq"]["show_on_ticket_page"] = False
    server = FakeCRQServer()
    html = page(cfg, server, "/ticket/TT-100234")
    assert "Recent CRQs" not in html and server.calls == []


# ---- "no CRQs" only when certain -------------------------------------------
def none_result(**kw):
    r = {"site": "W6102", "window_start": "2026-11-10T12:00:00+00:00", "window_end": "2026-11-18T09:00:00+00:00",
         "total": 0, "unknown_site_crqs": 0, "data_as_of": "2026-11-20T00:00+03:00", "crqs": []}
    r.update(kw)
    return r


def test_none_line_only_when_certain():
    lk = CRQLookup(settings())
    assert lk.email_note(none_result()) == \
        "Planned work check: no CRQs on site W6102 from 10 Nov 2026 until now in the CRQ records."
    # some CRQs in the window list their sites only in an attachment: the site may be among them
    assert lk.email_note(none_result(unknown_site_crqs=3)) is None
    # the export was taken before "now": work after it is not in the data
    assert lk.email_note(none_result(data_as_of="2026-11-11T23:03+03:00")) is None
    # freshness unknown
    assert lk.email_note(none_result(data_as_of=None)) is None
    # CRQ side down
    assert lk.email_note(None) is None


def test_page_says_when_data_ends_before_now(cfg):
    class Old(FakeCRQServer):
        def get(self, url, timeout, params):
            r = super().get(url, timeout, params)
            r.data["data_as_of"] = "2026-11-11T23:03+03:00"
            return r
    html = page(cfg, Old(crqs=[]), "/ticket/TT-100236")
    assert "CRQ records up to 2026-11-11 23:03: work after that is not in the data yet." in html
