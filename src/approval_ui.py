"""Human-in-the-loop approval page (SRS Section 8). Runs locally at http://127.0.0.1:5000

Nothing is ever sent without a person clicking Approve here.
"""

from __future__ import annotations

import os
import secrets
import threading

from flask import Flask, abort, flash, jsonify, redirect, render_template_string, request, url_for

from .agent import Agent
from .config import Config
from .sla import sla_status
from .timeutil import utcnow
from .worker import AgentLock, AgentWorker

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
 .live { display:flex; align-items:center; gap:6px; font-size:13px; color:var(--muted) }
 .dot { width:9px; height:9px; border-radius:50%; background:var(--good); flex:none }
 .live.busy .dot { background:var(--accent); animation:pulse 1s infinite } .live.err .dot { background:var(--bad) }
 @keyframes pulse { 50% { opacity:.25 } }
 .fetchbar { display:flex; gap:12px; align-items:center; background:var(--card); border:1px solid var(--accent);
             border-radius:10px; padding:12px 16px; margin-bottom:16px }
 .fetchbar[hidden] { display:none }
 .spin { width:18px; height:18px; border:3px solid var(--line); border-top-color:var(--accent); border-radius:50%;
         animation:rot .8s linear infinite; flex:none } @keyframes rot { to { transform:rotate(360deg) } }
 .newbar { background:var(--warn-bg); color:var(--warn); border-radius:10px; padding:10px 16px; margin-bottom:16px }
 .newbar[hidden] { display:none } .newbar a { color:inherit; font-weight:600 }
</style></head><body>
<header><a href="{{ url_for('index') }}">AI Escalator Agent</a>
 <a href="{{ url_for('inbox') }}">Inbox</a><a href="{{ url_for('tickets') }}">Tickets</a><a href="{{ url_for('sla') }}">SLA matrix</a><a href="{{ url_for('audit') }}">Audit log</a><span class="sp"></span>
 <form method="post" action="{{ url_for('run_inbox') }}"><input type="hidden" name="csrf" value="{{ csrf_token }}"><button>Check inbox</button></form>
 <form method="post" action="{{ url_for('run_followups') }}"><input type="hidden" name="csrf" value="{{ csrf_token }}"><button>Run follow-up rules</button></form>
 <span class="muted">AI: {{ cfg.ai_provider }} · Email: {{ cfg.email_provider }}</span>
 <span id="live" class="live"><span class="dot"></span><span id="live-text">Connecting…</span></span></header>
<main>
{% if demo_mode %}<div class="warn" style="margin-bottom:12px"><b>Demo mode is ON:</b> replies can go to any sender who
 emails about a ticket, not only the contacts in escalation.yaml. Escalations still go only to listed contacts.
 Turn off with <code>demo_reply_to_any_sender: false</code> in config/settings.yaml before real use.</div>{% endif %}
{% with msgs = get_flashed_messages(with_categories=true) %}{% for cat, m in msgs %}<div class="flash {{cat}}">{{ m }}</div>{% endfor %}{% endwith %}
<div id="fetchbar" class="fetchbar" hidden><div class="spin"></div>
 <div><b id="fetch-title">Fetching new emails…</b><div class="muted" id="fetch-detail"></div></div></div>
<div id="newbar" class="newbar" hidden>New emails were processed. <a href="">Refresh to see them</a>
 (not refreshed automatically because you are editing a draft).</div>
{{ content|safe }}
</main>
<script>
(function () {
  const seen = {{ watch_version }};
  let editing = false;
  document.addEventListener("input", () => { editing = true; });
  const live = document.getElementById("live"), liveText = document.getElementById("live-text");
  const bar = document.getElementById("fetchbar"), newbar = document.getElementById("newbar");
  function ago(ts) {
    if (!ts) return "";
    const s = Math.max(0, Math.round((Date.now() - new Date(ts)) / 1000));
    return s < 60 ? s + "s ago" : Math.round(s / 60) + " min ago";
  }
  function until(ts) {
    const s = Math.max(0, Math.round((new Date(ts) - Date.now()) / 1000));
    return s < 60 ? s + "s" : Math.round(s / 60) + " min";
  }
  async function tick() {
    try {
      const st = await (await fetch("{{ url_for('status') }}", {cache: "no-store"})).json();
      live.className = "live" + (["processing", "checking", "followups"].includes(st.phase) ? " busy" : "")
                              + (st.phase === "error" || st.phase === "off" ? " err" : "");
      if (st.phase === "followups") {
        liveText.textContent = st.message;
        bar.hidden = false;
        document.getElementById("fetch-title").textContent = st.message;
        document.getElementById("fetch-detail").textContent = st.subject || "";
      } else if (st.phase === "processing") {
        liveText.textContent = st.message;
        bar.hidden = false;
        document.getElementById("fetch-title").textContent = st.message;
        document.getElementById("fetch-detail").textContent =
          "Email " + (st.done + 1) + " of " + st.total + ": “" + (st.subject || "") +
          "” — the AI is reading it and drafting a reply";
      } else if (st.phase === "off") {
        bar.hidden = true;
        liveText.textContent = st.message;
      } else {
        bar.hidden = true;
        liveText.textContent = st.phase === "error" ? "Inbox check failed: " + st.error
          : st.phase === "checking" ? "Checking inbox…"
          : "Watching inbox · checked " + ago(st.last_checked) + " · every " + st.interval + "s"
            + (st.waiting_for_ai ? " · " + st.waiting_for_ai + " waiting for AI" : "")
            + (st.next_followups ? " · follow-up rules in " + until(st.next_followups) : "");
      }
      if (st.version > seen) {
        if (editing) newbar.hidden = false; else location.reload();
      }
    } catch (e) { live.className = "live err"; liveText.textContent = "Agent not reachable"; }
  }
  tick(); setInterval(tick, 1500);
})();
</script>
</body></html>"""

INDEX = """
<div class="card stats">
 <div class="stat"><b>{{ stats.pending }}</b><span class="muted">Drafts awaiting approval</span></div>
 <div class="stat"><b>{{ review|length }}</b><span class="muted">Emails needing a decision</span></div>
 <div class="stat"><b>{{ stats.sent }}</b><span class="muted">Emails sent</span></div>
 <div class="stat"><b>{{ waiting_ai }}</b><span class="muted">Waiting for AI (retried automatically)</span></div>
 <div class="stat"><b>{{ stats.approved_without_edits_pct if stats.approved_without_edits_pct is not none else '–' }}{% if stats.approved_without_edits_pct is not none %}%{% endif %}</b>
   <span class="muted">Sent without edits (evidence for auto-send)</span></div>
</div>

<div class="row" style="justify-content:space-between;margin:20px 0 12px">
 <h2 style="margin:0">Drafts awaiting approval</h2>
 {% if drafts %}<form method="post" action="{{ url_for('clear_drafts') }}"
   onsubmit="return confirm('Remove all {{ drafts|length }} waiting drafts? Nothing will be sent. Automated follow-ups for these tickets pause for 12 hours.');">
  <input type="hidden" name="csrf" value="{{ csrf_token }}"><button class="danger">Clear all drafts ({{ drafts|length }})</button></form>{% endif %}
</div>
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
 <form method="post" action="{{ url_for('approve', draft_id=d.id) }}"><input type="hidden" name="csrf" value="{{ csrf_token }}">
  <label>To (must be in escalation.yaml)</label><input type="text" name="to" value="{{ d.to_addrs|join(', ') }}">
  <label>Cc</label><input type="text" name="cc" value="{{ d.cc_addrs|join(', ') }}">
  <label>Subject</label><input type="text" name="subject" value="{{ d.subject }}">
  <label>Body (edit freely)</label><textarea name="body">{{ d.body }}</textarea>
  <div class="row" style="margin-top:10px"><button class="primary">Approve & send</button>
   <button class="danger" formaction="{{ url_for('reject', draft_id=d.id) }}">Reject</button></div>
 </form>
</div>
{% else %}<div class="card muted">No drafts waiting. New emails are picked up automatically.</div>{% endfor %}

<h2>Emails needing a decision</h2>
{% for e in review %}
<div class="card">
 <div class="row"><b>{{ e.tt_number or 'No ticket' }}</b><span class="pill">{{ e.intent or 'unknown' }}</span>
  <span class="muted">from {{ e.sender }} · {{ e.received_at }}</span></div>
 <div class="warn">{{ e.review_reason }}</div>
 <pre>{{ e.subject }}

{{ e.body }}</pre>
 <div class="row">
 {% if e.id in has_draft %}<span class="muted">A reply draft for this email is waiting above.</span>
 {% else %}<form method="post" action="{{ url_for('manual_reply', email_id=e.id) }}"><input type="hidden" name="csrf" value="{{ csrf_token }}"><button class="primary">Write reply</button></form>{% endif %}
 <form method="post" action="{{ url_for('reviewed', email_id=e.id) }}"><input type="hidden" name="csrf" value="{{ csrf_token }}"><button>Mark as reviewed</button></form></div>
</div>
{% else %}<div class="card muted">Nothing needs a decision.</div>{% endfor %}

<h2>Replied emails</h2>
{% for s in sent %}
<div class="card">
 <div class="row"><span class="pill" style="color:var(--good)">Sent</span>
  <b>{% if s.tt_number and s.tt_number != '-' %}<a href="{{ url_for('ticket', tt=s.tt_number) }}">{{ s.tt_number }}</a>{% else %}No ticket{% endif %}</b>
  <span class="pill">{{ kind_names.get(s.kind, s.kind) }}</span>
  <span class="muted">to <b>{{ s.to_addrs|join(', ') }}</b>{% if s.cc_addrs %} · cc {{ s.cc_addrs|join(', ') }}{% endif %}
   · {{ (s.decided_at or '')[:16].replace('T', ' ') }} UTC</span></div>
 {% if s.source %}<div class="muted" style="margin-top:6px">In reply to “{{ s.source.subject }}” from {{ s.source.sender }}
   ({{ (s.source.received_at or '')[:16].replace('T', ' ') }} UTC)</div>{% endif %}
 <details style="margin-top:8px"><summary>What we replied</summary><pre>{{ s.subject }}

{{ s.body }}</pre></details>
 {% if s.source %}<details><summary>Their original email</summary><pre>{{ s.source.subject }}

{{ s.source.body }}</pre></details>{% endif %}
</div>
{% else %}<div class="card muted">Nothing sent yet. Approved drafts appear here.</div>{% endfor %}
"""

TICKETS = """
<h2>Tickets (fake Remedy)</h2>
<div class="card tablewrap"><table><tr><th>TT</th><th>Site / Cell</th><th>Priority</th><th>Status</th><th>SLA due</th><th>SLA</th><th>Escalation</th></tr>
{% for t in tickets %}<tr><td><a href="{{ url_for('ticket', tt=t.tt_number) }}">{{ t.tt_number }}</a></td>
<td>{{ t.site_id }} / {{ t.cell_id }}</td><td>{{ t.priority }}</td><td>{{ t.status }}</td><td>{{ t.sla_due }}<div class="muted">from {{ t.sla_source }}</div></td>
<td>{% set st = sla_state[t.tt_number] %}<span class="pill" {% if st.startswith('Overdue') %}style="color:var(--bad)"{% endif %}>{{ st }}</span></td>
<td>{{ level_names.get(states[t.tt_number].escalation_level, '–') }}{% if states[t.tt_number].closed %} · monitoring stopped{% endif %}</td></tr>{% endfor %}
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

SLA = """
<h2>SLA matrix</h2>
<div class="card">
<p class="muted" style="margin-top:0">What the agent does, and when, for each ticket priority. All values are hours and can be
changed in <code>config/settings.yaml</code> (section <code>sla_matrix</code>); restart the agent after editing.</p>
<div class="tablewrap"><table>
<tr><th>Priority</th><th>Resolve within</th><th>Follow up if no update for</th><th>Reminder before deadline</th>
<th>Escalate to team lead</th><th>Escalate to manager</th><th>Then repeat every</th></tr>
{% for name, r in matrix.items() %}<tr><td><b>{{ name }}</b></td><td>{{ r.resolve_within }}h</td><td>{{ r.followup_if_quiet }}h</td>
<td>{{ r.remind_before }}h</td><td>{{ 'at the deadline' if r.team_lead_after == 0 else r.team_lead_after ~ 'h after deadline' }}</td>
<td>{{ r.manager_after }}h after deadline</td><td>{{ r.repeat_every }}h</td></tr>{% endfor %}
</table></div></div>
<div class="card"><b>Other rules</b><ul>
<li>A resolved ticket that is not closed after {{ rules.resolved_not_closed_hours }}h gets a closure-check email.</li>
<li>Closed tickets stop being followed up. Rejected tickets are left for a person to decide.</li>
<li>At most one automated email per ticket every {{ rules.min_hours_between_followups }}h, and never a new one while a draft for that ticket is waiting for approval.</li>
<li>Tickets with an unknown priority use the {{ rules.default_priority }} row.</li>
<li>The deadline comes from Remedy when the ticket has one; otherwise it is created time + "resolve within".</li>
<li>Who receives each level is set in <code>config/escalation.yaml</code>. Nothing is sent without approval.</li>
</ul></div>
"""

KIND_NAMES = {"reply": "Reply", "manual": "Reply written by a person", "ask_ticket": "Asked which ticket",
              "followup": "Follow-up", "reminder": "SLA reminder", "escalation": "Escalation",
              "closure_check": "Closure check"}

INBOX = """
<h2>Inbox</h2>
<p class="muted">Every email the agent received since the last fresh start, and what it did with it.
Only emails that mention a ticket number like <b>TT-100234</b> are handled; others are ignored.</p>
<div class="card tablewrap"><table>
<tr><th>Received (UTC)</th><th>From</th><th>Subject</th><th>Ticket</th><th>What the agent did</th></tr>
{% for e in emails %}<tr>
<td class="muted">{{ e.received_at[:16].replace('T', ' ') }}</td><td>{{ e.sender }}</td><td>{{ e.subject }}</td>
<td>{% if e.tt_number %}<a href="{{ url_for('ticket', tt=e.tt_number) }}">{{ e.tt_number }}</a>{% else %}–{% endif %}</td>
<td>{% if e.draft_count %}<span class="pill" style="color:var(--good)">Reply draft created</span>
{% elif e.status == 'needs_review' %}<span class="pill" style="color:var(--bad)">Flagged for review</span>
{% elif e.status == 'reviewed' %}<span class="pill">Reviewed by a person</span>
{% elif e.status == 'ai_retry' %}<span class="pill">Waiting for AI</span>
{% elif e.status == 'ignored' %}<span class="pill">Ignored</span>
{% else %}<span class="pill">Read{% if e.intent %} ({{ e.intent }}){% endif %}, no reply needed</span>{% endif %}
{% if e.review_reason %}<div class="muted">{{ e.review_reason }}</div>{% endif %}</td></tr>
{% else %}<tr><td colspan="5" class="muted">No emails received yet. Send one to the team mailbox.</td></tr>{% endfor %}
</table></div>
"""

AUDIT = """
<h2>Audit log</h2><div class="card tablewrap"><table><tr><th>Time (UTC)</th><th>Actor</th><th>Action</th><th>TT</th><th>Details</th></tr>
{% for a in audit %}<tr><td class="muted">{{ a.ts }}</td><td>{{ a.actor }}</td><td>{{ a.action }}</td><td>{{ a.tt_number or '' }}</td><td class="muted">{{ a.details }}</td></tr>{% endfor %}
</table></div>
"""


def create_app(cfg: Config, agent: Agent | None = None, watch: bool = True) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.urandom(24)
    csrf_token = secrets.token_urlsafe(32)   # new on every start; embedded in every form

    @app.context_processor
    def _inject_csrf():
        return {"csrf_token": csrf_token}

    @app.before_request
    def _protect():
        # Only this computer may use the page (blocks DNS-rebinding tricks), and every action must come
        # from one of our own forms (blocks other websites from clicking "Approve" in your browser).
        if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
            abort(403)
        if request.method == "POST" and not secrets.compare_digest(request.form.get("csrf", ""), csrf_token):
            abort(403, "Invalid or missing form token. Reload the page and try again.")
    agent = agent or Agent(cfg)
    lock = threading.Lock()   # one agent action at a time (worker, buttons, approvals)
    watcher = AgentWorker(agent, inbox_seconds=cfg.settings["agent"].get("inbox_watch_seconds", 10),
                          followup_minutes=cfg.settings["agent"]["poll_interval_minutes"], lock=lock)
    if watch:
        if AgentLock(cfg.root / "data" / ".agent.lock").acquire():
            watcher.start()
        else:
            watcher.disabled("Another agent is already running (e.g. main.py run); this page only shows drafts")
    else:
        watcher.disabled("Background checks are off")

    def page(template: str, **ctx) -> str:
        ctx.setdefault("kind_names", KIND_NAMES)
        content = render_template_string(template, **ctx)
        return render_template_string(BASE, content=content, cfg=cfg, watch_version=watcher.status()["version"],
                                      demo_mode=agent.demo_mode)

    def split(value: str) -> list[str]:
        return [a.strip() for a in (value or "").replace(";", ",").split(",") if a.strip()]

    @app.get("/")
    def index():
        drafts = agent.store.drafts(status="pending")
        for d in drafts:
            d["source"] = agent.store.get_email(d["source_email_id"]) if d["source_email_id"] else None
        # Flagged list = only emails that still need a decision. Emails that already have a draft
        # (waiting above) or were answered (listed under "Replied emails") are not repeated here.
        review = [e for e in agent.store.emails_needing_review() if not agent.store.has_draft_for_email(e["id"])]
        return page(INDEX, drafts=drafts, review=review, has_draft=set(), sent=agent.store.sent_messages(),
                    stats=agent.store.approval_stats(), waiting_ai=agent.store.count_waiting_for_ai())

    @app.get("/tickets")
    def tickets():
        ts = agent.tickets.list_all()
        now, closed = utcnow(), cfg.settings["tickets"]["closed_statuses"]
        return page(TICKETS, tickets=ts, states={t["tt_number"]: agent.store.ticket_state(t["tt_number"]) for t in ts},
                    sla_state={t["tt_number"]: sla_status(t, cfg.settings, now, closed) for t in ts},
                    level_names={0: "–", 1: "Back Office", 2: "Team lead", 3: "Manager"})

    @app.get("/sla")
    def sla():
        return page(SLA, matrix=cfg.settings["sla_matrix"], rules=cfg.settings["followup"])

    @app.get("/ticket/<tt>")
    def ticket(tt):
        return page(TICKET, tt=tt, t=agent.tickets.get(tt), history=agent.store.ticket_history(tt),
                    audit=agent.store.audit_entries(tt))

    @app.get("/status")
    def status():
        return jsonify(watcher.status())

    @app.get("/inbox")
    def inbox():
        return page(INBOX, emails=agent.store.inbox_emails())

    @app.get("/audit")
    def audit():
        return page(AUDIT, audit=agent.store.audit_entries())

    @app.post("/draft/<int:draft_id>/approve")
    def approve(draft_id):
        f = request.form
        try:
            with lock:
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

    @app.post("/email/<int:email_id>/reply")
    def manual_reply(email_id):
        try:
            with lock:
                draft_id = agent.create_manual_reply(email_id)
            flash(f"Reply draft {draft_id} created. Write your message below and approve it.", "ok")
        except Exception as e:
            flash(f"Could not create reply: {e}", "error")
        return redirect(url_for("index"))

    @app.post("/drafts/clear")
    def clear_drafts():
        with lock:
            n = agent.clear_pending_drafts()
        flash(f"Cleared {n} draft{'s' if n != 1 else ''}. Nothing was sent." if n else "No drafts to clear.", "ok")
        return redirect(url_for("index"))

    @app.post("/run/inbox")
    def run_inbox():
        if watcher.status()["running"]:
            watcher.check_now()   # the worker does the work; the page shows progress live
            flash("Checking the inbox now…", "ok")
            return redirect(url_for("index"))
        try:
            with lock:
                s = agent.process_inbox()
            flash(f"Inbox checked: {s['new']} new, {s['drafts']} drafts, {s['flagged']} flagged.", "ok")
        except Exception as e:
            flash(f"Inbox check failed: {e}", "error")
        return redirect(url_for("index"))

    @app.post("/run/followups")
    def run_followups():
        if watcher.status()["running"]:
            watcher.followups_now()     # the worker runs them; the page shows progress live
            flash("Running follow-up rules now…", "ok")
            return redirect(url_for("index"))
        try:
            with lock:
                s = agent.run_followups()
            flash(f"Follow-up rules: {s['checked']} tickets checked, {s['drafts']} drafts created, "
                  f"{s['skipped_pending']} skipped (draft already pending), {s['closed']} closed.", "ok")
        except Exception as e:
            flash(f"Follow-up run failed: {e}", "error")
        return redirect(url_for("index"))

    return app
