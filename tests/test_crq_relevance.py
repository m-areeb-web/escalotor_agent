"""Marking CRQs that match the ticket's problem type (config/crq_relevance.yaml)."""

from pathlib import Path

from src.crq_lookup import CRQLookup
from src.crq_relevance import IssueType, RelevanceTable, _words_re

REAL_TABLE = Path(__file__).resolve().parents[1] / "config" / "crq_relevance.yaml"


def crq(cid, activity, result=None):
    return {"crq_id": cid, "change_type": "MDT", "activity": activity, "status": "Closed",
            "activity_result": result, "sched_start": "2026-11-12T00:00:00", "sched_end": None}


def table():
    return RelevanceTable([
        IssueType("interference", "Interference", _words_re(["interference", "rssi"]),
                  _words_re(["antenna", "rru", "new sector"])),
        IssueType("unavailability", "Cell / site unavailable", _words_re(["unavailable", "outage"]),
                  _words_re(["power", "battery", "mw"])),
    ])


def test_whole_words_only():
    r = _words_re(["rru", "ho", "5g"])
    assert r.search("RRU swap") and r.search("5G introduction") and r.search("HO failures")
    assert not r.search("rrus2 upgrade") and not r.search("ho5 link") and not r.search("photos")


def test_issue_type_from_ticket_text():
    t = table()
    assert [x.key for x in t.issue_types({"description": "High interference (UL RSSI -95 dBm)"})] == ["interference"]
    assert t.issue_types({"description": "Something unusual"}) == []
    assert t.issue_types({}) == []


def test_marks_reason_and_order_keeps_failed_first():
    crqs = [crq("A", "Cabinet door replacement"), crq("B", "Antenna tilt change"),
            crq("C", "Link upgrade", result="ROLLBACK"), crq("D", "RRU swap")]
    issues, out = table().mark(crqs, {"description": "High interference on cell"})
    assert issues == ["Interference"]
    assert [c["crq_id"] for c in out] == ["C", "B", "D", "A"]     # ROLLBACK, then matches, then rest
    assert out[1]["match"] == "Interference: antenna work"
    assert out[0]["match"] is None and out[3]["match"] is None
    assert len(out) == len(crqs)                                     # nothing hidden


def test_no_issue_type_marks_nothing_and_keeps_order():
    crqs = [crq("A", "Antenna tilt"), crq("B", "Battery replacement")]
    issues, out = table().mark(crqs, {"description": "Customer complaint"})
    assert issues == [] and [c["crq_id"] for c in out] == ["A", "B"]
    assert all(c["match"] is None for c in out)


def test_missing_table_file_is_harmless(tmp_path):
    t = RelevanceTable.load(tmp_path / "nope.yaml")
    issues, out = t.mark([crq("A", "Antenna tilt")], {"description": "interference"})
    assert issues == [] and out[0]["match"] is None


def test_real_table_recognises_every_test_ticket():
    t = RelevanceTable.load(REAL_TABLE)
    cases = {
        "High call drop rate (3.8%) on cell": "Call drops",
        "Low throughput on LTE cell during busy hour": "Low throughput / capacity",
        "Cell unavailable intermittently; availability 91%": "Cell / site unavailable",
        "High interference (UL RSSI -95 dBm) on cell.": "Interference",
        "Handover failure rate 6% towards neighbour W5374.": "Handover failures",
        "VoLTE call setup failures above threshold.": "Call setup / access failures",
    }
    for text, label in cases.items():
        assert label in [x.label for x in t.issue_types({"description": text})], text


def test_real_table_sensible_pairs():
    t = RelevanceTable.load(REAL_TABLE)
    def matched(issue, activity):
        return t.mark([crq("X", activity)], {"description": issue})[1][0]["match"]
    assert matched("High interference (UL RSSI -95 dBm)", "WR-TCN-Normal-Project-Installation for new sector")
    assert not matched("High interference (UL RSSI -95 dBm)", "Power system battery replacement")
    assert matched("Cell unavailable, availability 91%", "Power system battery replacement")
    assert matched("Handover failure towards neighbour", "Neighbour relation update")
    assert not matched("Handover failure towards neighbour", "Cabinet door replacement")


class OneSite:
    def get(self, url, timeout, params):
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"site_id": params["site"], "total_matches": 2, "crqs_with_unknown_sites_in_window": 0,
                        "crqs": [crq("CRQ1", "Cabinet door replacement"), crq("CRQ2", "RRU swap")]}
        return R()


def test_lookup_marks_per_ticket_and_note_says_why():
    lk = CRQLookup({"crq": {"enabled": True, "add_note_to_emails": True}}, session=OneSite(), relevance=table())
    base = {"tt_number": "TT-1", "site_id": "W1000", "created_at": "2026-11-17T00:00:00+00:00"}
    r = lk.for_ticket({**base, "description": "High interference"})
    assert r["issue_types"] == ["Interference"] and r["matches"] == 1
    assert [c["crq_id"] for c in r["crqs"]] == ["CRQ2", "CRQ1"]
    note = lk.email_note(r)
    assert "CRQ2 (MDT, RRU swap), 12 Nov 2026, Closed - matches the reported issue (Interference: rru work)" in note
    # same site, different problem: cached site data, marks recomputed for this ticket
    r2 = lk.for_ticket({**base, "description": "Cell unavailable"})
    assert r2["matches"] == 0 and [c["crq_id"] for c in r2["crqs"]] == ["CRQ1", "CRQ2"]
