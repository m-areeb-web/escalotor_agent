"""AI provider layer. The rest of the agent only calls `generate_json(...)`.

Switch providers with AI_PROVIDER in .env:
  gemini - Google Gemini API (POC default)
  ollama - local open-source model via Ollama (free, data stays on the machine)
  rules  - offline keyword rules, no AI at all. Only for tests and demos without a key.

To add another provider later (Claude, Azure, ...), write a class with
`generate_json` and register it in `get_provider()`.
"""

from __future__ import annotations

import json
import re
import time

import requests

from .config import Config


class AIError(RuntimeError):
    pass


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    # Some models wrap JSON in ```json fences even when asked not to.
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise AIError(f"Model did not return valid JSON: {text[:200]!r}") from e
    if not isinstance(data, dict):
        raise AIError("Model returned JSON that is not an object")
    return data


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str, fallback_models: list[str] | None = None):
        if not api_key:
            raise AIError("AI_API_KEY is empty. Add your Gemini key to .env")
        from google import genai  # imported here so other providers don't need it
        from google.genai import types

        # 60s per request so a busy model can never hang the agent.
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=60_000))
        # Try the main model first, then fallbacks when a model is overloaded (503) or rate limited (429).
        self.models = [model] + [m for m in (fallback_models or []) if m and m != model]

    def generate_json(self, system: str, prompt: str, *, task: str, context: dict | None = None) -> dict:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            temperature=0.2,
        )
        errors: list[str] = []
        for attempt in range(2):          # two rounds over the model list
            for model in self.models:
                try:
                    resp = self.client.models.generate_content(model=model, contents=prompt, config=config)
                    return _parse_json(resp.text)
                except AIError as e:      # bad JSON: try the next model / round
                    errors.append(f"{model}: {e}")
                except Exception as e:    # network / rate limit / overloaded
                    code = getattr(e, "code", None)
                    if code not in (408, 429, 500, 502, 503, 504) and not isinstance(e, requests.RequestException):
                        raise AIError(f"Gemini error ({model}): {e}") from e
                    errors.append(f"{model}: {code or type(e).__name__}")
            if attempt == 0:
                time.sleep(5)
        raise AIError("All Gemini models failed: " + "; ".join(errors[-4:]))


class OllamaProvider:
    name = "ollama"

    def __init__(self, url: str, model: str):
        self.url = url.rstrip("/")
        self.model = model

    def generate_json(self, system: str, prompt: str, *, task: str, context: dict | None = None) -> dict:
        try:
            r = requests.post(
                f"{self.url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.2},
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                },
                timeout=300,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise AIError(f"Ollama request failed (is `ollama serve` running?): {e}") from e
        return _parse_json(r.json()["message"]["content"])


class RulesProvider:
    """Offline stand-in with keyword rules. Lets the whole pipeline run without any AI key.
    Quality is intentionally basic; use a real model for anything you show the client."""

    name = "rules"

    KEYWORDS = [
        ("Ticket Resolution", ["resolved", "fixed", "rectified", "issue is cleared", "has been restored"]),
        ("Rejection", ["reject", "not our domain", "wrong team", "not valid", "returning the ticket"]),
        ("Information Required", ["please share", "please provide", "need more", "require", "could you send"]),
        ("Pending Investigation", ["investigating", "under investigation", "checking", "looking into", "in progress"]),
        ("Escalation Required", ["sla breach", "breached", "escalate"]),
        ("Acknowledgment", ["noted", "received", "acknowledged", "thanks for the update", "will check"]),
        ("Ticket Update", ["update", "changed", "replaced", "tilt", "parameter"]),
    ]

    def generate_json(self, system: str, prompt: str, *, task: str, context: dict | None = None) -> dict:
        ctx = context or {}
        if task == "classify":
            body = (ctx.get("body") or "").lower()
            intent, conf = "Unclear", 0.3
            for name, words in self.KEYWORDS:
                if any(w in body for w in words):
                    intent, conf = name, 0.75
                    break
            date = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", body)
            return {
                "intent": intent, "confidence": conf, "tt_number": None, "site_id": None, "cell_id": None,
                "issue_description": None, "investigation_status": None, "requested_action": None,
                "responsible_team": None, "expected_resolution_date": date.group(1) if date else None,
                "summary": (ctx.get("body") or "")[:160], "needs_human_review": intent == "Unclear",
                "review_reason": "Could not determine intent" if intent == "Unclear" else None,
            }
        if task == "draft":
            t = ctx.get("ticket", {})
            purpose = ctx.get("purpose", "")
            return {
                "subject": f"{t.get('tt_number', '')} | {t.get('site_id', '')} | {ctx.get('kind', 'update')}",
                "body": (
                    "Dear Team,\n\n"
                    f"{purpose}\n\n"
                    f"Ticket: {t.get('tt_number')}\nSite / Cell: {t.get('site_id')} / {t.get('cell_id')}\n"
                    f"Issue: {t.get('description')}\nCurrent status: {t.get('status')}\n"
                    f"SLA due: {t.get('sla_due')}"
                ),
            }
        raise AIError(f"Unknown task {task}")


def get_provider(cfg: Config):
    if cfg.ai_provider == "gemini":
        return GeminiProvider(cfg.ai_api_key, cfg.ai_model, cfg.ai_fallback_models)
    if cfg.ai_provider == "ollama":
        return OllamaProvider(cfg.ollama_url, cfg.ai_model)
    if cfg.ai_provider == "rules":
        return RulesProvider()
    raise AIError(f"Unknown AI_PROVIDER '{cfg.ai_provider}'. Use gemini, ollama or rules.")
