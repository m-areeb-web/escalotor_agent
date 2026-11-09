# AI Escalator Agent: POC

Reads Remedy-ticket emails, works out what they mean, and drafts replies, follow-ups and
escalations. **A human approves every email before it is sent.**

This POC covers SRS Part 1 (Outlook Email Intelligence). Remedy is simulated with
`data/fake_tickets.json` until real access is available.

## 1. Install (once)

```bash
cd escalator_agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then edit .env
```

## 2. Add the Gemini key

1. Go to https://aistudio.google.com → **Get API key** → create a key.
2. In `.env` set `AI_API_KEY=<your key>`.
3. Check it works: `.venv/bin/python main.py test-ai`

If `test-ai` reports the model name is not found, set `AI_MODEL` in `.env` to a current Gemini
Flash model name from the AI Studio model list.

> Free-tier Gemini may use your data to improve Google's products. **Use only test emails.**

## 3. Try it with the mock inbox (no Outlook needed)

```bash
.venv/bin/python main.py seed          # fake tickets + 6 sample emails, resets the database
.venv/bin/python main.py check-email   # read, classify, draft replies
.venv/bin/python main.py followups     # SLA / no-update / escalation rules
.venv/bin/python main.py ui            # open http://127.0.0.1:5000 to approve / edit / reject
```

The sample emails include a normal update, a resolution, an information request, a
**prompt-injection attempt** and an email from an **unknown sender**. The last two should be
flagged, not answered. "Sent" mock emails go to `data/mock_outbox.jsonl`.

Add your own test email:
```bash
.venv/bin/python main.py mock-email --sender backoffice.test@example.com \
  --subject "RE: TT-100234" --body "Still investigating, ETA 2026-11-15"
```

Simulate Back Office progress by editing `data/fake_tickets.json` (e.g. set a status to
`Resolved` or `Closed`), then run `followups` again.

## 4. Connect your POC Outlook account

1. Create a new Outlook.com account for the POC, and set `MY_EMAIL_ADDRESS` in `.env`.
2. Sign in to https://portal.azure.com with it (a free Azure account is enough).
3. **Microsoft Entra ID → App registrations → New registration**
   - Name: `Escalator Agent POC`
   - Supported account types: **Personal Microsoft accounts only**
   - Redirect URI: leave empty → **Register**
4. Copy the **Application (client) ID** into `.env` as `MS_CLIENT_ID`.
5. **Authentication → Advanced settings → Allow public client flows: Yes** → Save.
6. **API permissions → Add → Microsoft Graph → Delegated** → add `Mail.ReadWrite` and `Mail.Send`.
7. In `.env` set `EMAIL_PROVIDER=outlook`, then run:
   ```bash
   .venv/bin/python main.py login
   ```
   Open the link shown, enter the code, and sign in with the POC account.
8. In `config/escalation.yaml`, replace the example addresses with your **second test account**
   (playing the Back Office). Then send emails from that account mentioning a ticket number
   like `TT-100234`, and run `check-email`.

## 5. Run continuously

```bash
.venv/bin/python main.py run    # checks inbox + rules every 5 minutes (settings.yaml)
.venv/bin/python main.py ui     # in a second terminal, to approve drafts
```

## Configuration

| File | What it controls |
|---|---|
| `.env` | AI provider, model, API key, email provider, Azure client ID |
| `config/settings.yaml` | Follow-up timings, SLA reminder windows, escalation repeat time, confidence threshold |
| `config/escalation.yaml` | Level 1/2/3 recipients per assignment group. **The agent can only send to these addresses.** |
| `data/fake_tickets.json` | Fake Remedy tickets (`main.py seed` regenerates them) |

## Safety built in

- Nothing is sent without human approval in the web page.
- Recipients come only from `escalation.yaml`; addresses inside emails are never used.
- Email content is treated as untrusted data, and injection attempts are flagged (AI instruction plus a code check).
- Unknown senders, unclear intent, low confidence and tickets missing from Remedy are flagged for review.
- One pending draft per ticket, so there are no duplicate follow-ups.
- Full audit log of every agent and human action (`/audit` page).
- Keys stay in `.env`, which is git-ignored.

## Swapping later

| When | Change |
|---|---|
| Client gets a paid AI key | `.env`: `AI_PROVIDER` / `AI_MODEL` / `AI_API_KEY` (add a provider class in `src/ai_provider.py` if new) |
| Free local model | Install Ollama, `ollama pull qwen3:8b`, set `AI_PROVIDER=ollama`, `AI_MODEL=qwen3:8b` |
| Enterprise mailbox | Same Graph code; change the authority in `src/email_client.py` to the client's tenant ID |
| Real Remedy | Write `RemedyTicketSource` with `get()` / `list_all()` / `list_open()` in `src/ticket_source.py` |

## Tests

```bash
.venv/bin/python -m pytest -q     # runs offline, no keys needed
```
