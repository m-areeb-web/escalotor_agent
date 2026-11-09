"""Human-in-the-loop approval page (SRS Section 8). Runs locally at http://127.0.0.1:5000

Nothing is ever sent without a person clicking Approve here.
"""

from __future__ import annotations

import os

from flask import Flask, flash, redirect, render_template_string, request, url_for

from .agent import Agent
from .config import Config

BASE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Escalator Agent</title>
<style>
 :root { --bg:#f6f7f9; --card:#fff; --text:#1c1f24; --muted:#5f6673; --line:#dde1e7; --accent:#2457d6;
         --warn-bg:#fff6e0; --warn:#8a5a00; --bad:#b42318; --good:#137333; }
 @media (prefers-color-scheme: dark) { :root { --bg:#15171b; --card:#1f2227; --text:#e7e9ee; --muted:#9aa1ad;
         --line:#33373e; --accent:#7aa2ff; --warn-bg:#3a2f12; --warn:#f2c66d; --bad:#ff8a80; --good:#7fd69a; } }
 * { box-sizing:border-box } body { margin:0; font:15px/1.5 system-ui,sans-serif; background:var(--bg); color:var(--text) }
 header { padding:12px 16px; border-bottom:1px solid var(--line); background:var(--card); display:flex; gap:16px; align-items:center; flex-wrap:wrap }
 header a { color:var(--text); text-decoration:none; font-weight:600 } header .sp { flex:1 }
 main { max-width:1000px; margin:0 auto; padding:16px }
 .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px }
 .muted { color:var(--muted); font-size:13px } .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center }
 .pill { display:inline-block; padding:1px 8px; border-radius:99px; border:1px solid var(--line); font-size:12px }
 .warn { background:var(--warn-bg); color:var(--warn); padding:8px 10px; border-radius:6px; font-size:13px; margin:8px 0 }
 .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px }
 .stat b { display:block; font-size:22px }
 input[type=text], textarea { width:100%; padding:8px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--text); font:inherit }
 textarea { min-height:220px; font-family:ui-monospace,monospace; font-size:13px }
 label { font-size:13px; color:var(--muted); display:block; margin-top:8px }
 button { padding:8px 14px; border-radius:6px; border:1px solid var(--line); background:var(--card); color:var(--text); font:inherit; cursor:pointer }
 button.primary { background:var(--accent); color:#fff; border-color:var(--accent) } button.danger { color:var(--bad) }
 pre { white-space:pre-wrap; background:var(--bg); padding:10px; border-radius:6px; font-size:13px; margin:6px 0 }
 table { width:100%; border-collapse:collapse; font-size:13px } td,th { text-align:left; padding:6px; border-bottom:1px solid var(--line); vertical-align:top }
 .flash { padding:10px; border-radius:6px; margin-bottom:12px; background:var(--card); border:1px solid var(--line) }
 .flash.error { color:var(--bad) } .flash.ok { color:var(--good) }
 details summary { cursor:pointer; color:var(--muted); font-size:13px }
 .tablewrap { overflow-x:auto }
</style></head><body>
<header><a href="{{ url_for('index') }}">AI Escalator Agent</a>
 <a href="{{ url_for('tickets') }}">Tickets</a><a href="{{ url_for('audit') }}">Audit log</a><span class="sp"></span>
 <form method="post" action="{{ url_for('run_inbox') }}"><button>Check inbox</button></form>
 <form method="post" action="{{ url_for('run_followups') }}"><button>Run follow-up rules</button></form>
 <span class="muted">AI: {{ cfg.ai_provider }} · Email: {{ cfg.email_provider }}</span></header>
<main>
{% with msgs = get_flashed_messages(with_categories=true) %}{% for cat, m in msgs %}<div class="flash {{cat}}">{{ m }}</div>{% endfor %}{% endwith %}
{{ content|safe }}
</main></body></html>"""

INDEX = """
<div class="card stats">
 <div class="stat"><b>{{ stats.pending }}</b><span class="muted">Drafts awaiting approval</span></div>
 <div class="stat"><b>{{ review|length }}</b><span class="muted">Emails flagged for review</span></div>
 <div class="stat"><b>{{ stats.sent }}</b><span class="muted">Emails sent</span></div>
 <div class="stat"><b>{{ stats.approved_without_edits_pct if stats.approved_without_edits_pct is not none else '–' }}{% if stats.approved_without_edits_pct is not none %}%{% endif %}</b>
   <span class="muted">Sent without edits (evidence for auto-send)</span></div>
</div>

<h2>Drafts awaiting approval</h2>
{% for d in drafts %}
<div class="card">
 <div class="row"><b><a href="{{ url_for('ticket', tt=d.tt_number) }}">{{ d.tt_number }}</a></b>
  <span class="pill">{{ d.kind }}</span>{% if d.level > 1 %}<span class="pill">level {{ d.level }}</span>{% endif %}
  <span class="muted">created {{ d.created_at }}</span></div>
 {% if d.needs_attention %}<div class="warn">{{ d.needs_attention }}</div>{% endif %}
 {% if d.error %}<div class="warn">Last send attempt failed: {{ d.error }}</div>{% endif %}
 {% if d.source %}<details><summary>Email this replies to (from {{ d.source.sender }}, intent: {{ d.source.intent }})</summary>
   <pre>{{ d.source.subject }}

{{ d.source.body }}</pre></details>{% endif %}
 <form method="post" action="{{ url_for('approve', draft_id=d.id) }}">
  <label>To (must be in escalation.yaml)</label><input type="text" name="to" value="{{ d.to_addrs|join(', ') }}">
  <label>Cc</label><input type="text" name="cc" value="{{ d.cc_addrs|join(', ') }}">
  <label>Subject</label><input type="text" name="subject" value="{{ d.subject }}">
  <label>Body (edit freely)</label><textarea name="body">{{ d.body }}</textarea>
  <div class="row" style="margin-top:10px"><button class="primary">Approve & send</button>
   <button class="danger" formaction="{{ url_for('reject', draft_id=d.id) }}">Reject</button></div>
 </form>
</div>
{% else %}<div class="card muted">No drafts waiting. Use “Check inbox” or “Run follow-up rules”.</div>{% endfor %}

<h2>Emails flagged for human review</h2>
{% for e in review %}
<div class="card">
 <div class="row"><b>{{ e.tt_number or 'No ticket' }}</b><span class="pill">{{ e.intent or 'unknown' }}</span>
  <span class="muted">from {{ e.sender }} · {{ e.received_at }}</span></div>
 <div class="warn">{{ e.review_reason }}</div>
 <pre>{{ e.subject }}

{{ e.body }}</pre>
 <form method="post" action="{{ url_for('reviewed', email_id=e.id) }}"><button>Mark as reviewed</button></form>
</div>
{% else %}<div class="card muted">Nothing flagged.</div>{% endfor %}
"""

TICKETS = """
<h2>Tickets (fake Remedy)</h2>
<div class="card tablewrap"><table><tr><th>TT</th><th>Site / Cell</th><th>Priority</th><th>Status</th><th>SLA due</th><th>Escalation</th></tr>
{% for t in tickets %}<tr><td><a href="{{ url_for('ticket', tt=t.tt_number) }}">{{ t.tt_number }}</a></td>
<td>{{ t.site_id }} / {{ t.cell_id }}</td><td>{{ t.priority }}</td><td>{{ t.status }}</td><td>{{ t.sla_due }}</td>
<td>{{ states[t.tt_number].escalation_level }}{% if states[t.tt_number].closed %} · monitoring stopped{% endif %}</td></tr>{% endfor %}
</table></div>
"""

TICKET = """
<h2>{{ t.tt_number if t else tt }}</h2>
{% if t %}<div class="card"><div class="row"><span class="pill">{{ t.status }}</span><span class="pill">{{ t.priority }}</span>
<span class="muted">{{ t.assignment_group }} · SLA due {{ t.sla_due }}</span></div>
<p>{{ t.description }}</p><div class="muted">Site {{ t.site_id }} · Cell {{ t.cell_id }} · created {{ t.created_at }}</div></div>
{% else %}<div class="card warn">Not found in Remedy.</div>{% endif %}
<h3>Communication history</h3>
{% for e in history %}<div class="card"><div class="row"><span class="pill">{{ 'sent' if e.direction == 'out' else 'received' }}</span>
<b>{{ e.sender }}</b><span class="muted">{{ e.received_at }}</span>{% if e.intent %}<span class="pill">{{ e.intent }}</span>{% endif %}</div>
<pre>{{ e.subject }}

{{ e.body }}</pre>{% if e.extracted %}<details><summary>Extracted details</summary><pre>{{ e.extracted|tojson(indent=2) }}</pre></details>{% endif %}</div>
{% else %}<div class="card muted">No emails yet.</div>{% endfor %}
<h3>Audit trail</h3><div class="card tablewrap"><table>{% for a in audit %}<tr><td class="muted">{{ a.ts }}</td><td>{{ a.actor }}</td><td>{{ a.action }}</td><td class="muted">{{ a.details }}</td></tr>{% endfor %}</table></div>
"""

AUDIT = """
<h2>Audit log</h2><div class="card tablewrap"><table><tr><th>Time (UTC)</th><th>Actor</th><th>Action</th><th>TT</th><th>Details</th></tr>
{% for a in audit %}<tr><td class="muted">{{ a.ts }}</td><td>{{ a.actor }}</td><td>{{ a.action }}</td><td>{{ a.tt_number or '' }}</td><td class="muted">{{ a.details }}</td></tr>{% endfor %}
</table></div>
"""


def create_app(cfg: Config, agent: Agent | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.urandom(24)
    agent = agent or Agent(cfg)

    def page(template: str, **ctx) -> str:
        content = render_template_string(template, **ctx)
        return render_template_string(BASE, content=content, cfg=cfg)

    def split(value: str) -> list[str]:
        return [a.strip() for a in (value or "").replace(";", ",").split(",") if a.strip()]

    @app.get("/")
    def index():
        drafts = agent.store.drafts(status="pending")
        for d in drafts:
            d["source"] = agent.store.get_email(d["source_email_id"]) if d["source_email_id"] else None
        return page(INDEX, drafts=drafts, review=agent.store.emails_needing_review(),
                    stats=agent.store.approval_stats())

    @app.get("/tickets")
    def tickets():
        ts = agent.tickets.list_all()
        return page(TICKETS, tickets=ts, states={t["tt_number"]: agent.store.ticket_state(t["tt_number"]) for t in ts})

    @app.get("/ticket/<tt>")
    def ticket(tt):
        return page(TICKET, tt=tt, t=agent.tickets.get(tt), history=agent.store.ticket_history(tt),
                    audit=agent.store.audit_entries(tt))

    @app.get("/audit")
    def audit():
        return page(AUDIT, audit=agent.store.audit_entries())

    @app.post("/draft/<int:draft_id>/approve")
    def approve(draft_id):
        f = request.form
        try:
            agent.approve_draft(draft_id, subject=f["subject"].strip(), body=f["body"],
                                to=split(f.get("to")), cc=split(f.get("cc")))
            flash(f"Draft {draft_id} sent.", "ok")
        except Exception as e:
            flash(f"Not sent: {e}", "error")
        return redirect(url_for("index"))

    @app.post("/draft/<int:draft_id>/reject")
    def reject(draft_id):
        try:
            agent.reject_draft(draft_id, "Rejected by reviewer")
            flash(f"Draft {draft_id} rejected.", "ok")
        except Exception as e:
            flash(str(e), "error")
        return redirect(url_for("index"))

    @app.post("/email/<int:email_id>/reviewed")
    def reviewed(email_id):
        agent.store.mark_email_reviewed(email_id)
        agent.store.audit("human", "email_reviewed", email_id=email_id)
        return redirect(url_for("index"))

    @app.post("/run/inbox")
    def run_inbox():
        try:
            s = agent.process_inbox()
            flash(f"Inbox checked: {s['new']} new, {s.get('drafts', 0)} drafts, {s.get('flagged', 0)} flagged, "
                  f"{s.get('ignored', 0)} ignored.", "ok")
        except Exception as e:
            flash(f"Inbox check failed: {e}", "error")
        return redirect(url_for("index"))

    @app.post("/run/followups")
    def run_followups():
        try:
            s = agent.run_followups()
            flash(f"Follow-up rules: {s['checked']} tickets checked, {s['drafts']} drafts created, "
                  f"{s['skipped_pending']} skipped (draft already pending), {s['closed']} closed.", "ok")
        except Exception as e:
            flash(f"Follow-up run failed: {e}", "error")
        return redirect(url_for("index"))

    return app
