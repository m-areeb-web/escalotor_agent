#!/usr/bin/env bash
# One-step launcher for the AI Escalator Agent POC.
#
#   ./run.sh           start the agent (keeps today's drafts and history) and open the browser
#   ./run.sh fresh     reset fake tickets + database first (only emails arriving from now are read)
#   ./run.sh test      run all automated tests
#   ./run.sh stop      stop a running agent
#
# Stop the agent at any time with Ctrl+C (or ./run.sh stop from another window).

set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-start}"
PY=".venv/bin/python"
URL_FILE="data/.agent_url"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

agent_pids() {
  # Python processes running main.py start/ui/run from THIS folder only.
  local pid
  for pid in $(pgrep -f "main.py (start|ui|run)" 2>/dev/null || true); do
    if [ "$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" = "$PWD" ]; then
      echo "$pid"
    fi
  done
}

# ---- 1. Python environment ------------------------------------------------------
setup() {
  if [ ! -x "$PY" ]; then
    say "Step 1/4: creating Python environment (first run only)"
    python3 -m venv .venv
    "$PY" -m pip install -q --upgrade pip
    "$PY" -m pip install -q -r requirements.txt
  else
    say "Step 1/4: Python environment OK"
  fi
  [ -f .env ] || fail ".env is missing. Copy .env.example to .env and fill in your keys."
}

case "$MODE" in
  test)
    setup
    say "Running all tests"
    exec "$PY" -m pytest -q
    ;;
  stop)
    pids="$(agent_pids)"
    [ -n "$pids" ] || { echo "No agent is running."; exit 0; }
    kill $pids && echo "Agent stopped."
    exit 0
    ;;
  start|fresh) ;;
  *) fail "Unknown option '$MODE'. Use: ./run.sh [fresh|test|stop]" ;;
esac

setup

# Already running? Just open it again instead of starting a second copy.
if [ -n "$(agent_pids)" ]; then
  url="$(cat "$URL_FILE" 2>/dev/null || echo http://127.0.0.1:5000)"
  say "The agent is already running: $url"
  [ -n "${NO_BROWSER:-}" ] || open "$url" 2>/dev/null || true
  echo "To restart it: ./run.sh stop, then ./run.sh"
  exit 0
fi

# ---- 2. Outlook sign-in ------------------------------------------------------------
EMAIL_PROVIDER="$("$PY" -c 'from src.config import load_config; print(load_config().email_provider)')"
if [ "$EMAIL_PROVIDER" = "outlook" ]; then
  if "$PY" -c 'from src.config import load_config; from src.email_client import OutlookEmailClient as O; c=load_config(); O(c.ms_client_id, c.root)._token()' 2>/dev/null; then
    say "Step 2/4: Outlook sign-in OK"
  else
    say "Step 2/4: Outlook sign-in needed - open the link below and enter the code"
    "$PY" main.py login
  fi
else
  say "Step 2/4: using the practice mailbox (EMAIL_PROVIDER=$EMAIL_PROVIDER), no sign-in needed"
fi

# ---- 3. Fresh start (optional) -------------------------------------------------------
if [ "$MODE" = "fresh" ]; then
  say "Step 3/4: fresh start - new fake tickets, empty database"
  "$PY" main.py seed
else
  say "Step 3/4: keeping existing tickets, drafts and history (use './run.sh fresh' to reset)"
  [ -f data/fake_tickets.json ] || "$PY" main.py seed
fi

# ---- 4. Start the agent and open the browser --------------------------------------
PORT=5000
# macOS uses port 5000 for AirPlay Receiver; pick the next free port if it is taken.
while lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do PORT=$((PORT + 1)); done
URL="http://127.0.0.1:$PORT"
mkdir -p data && echo "$URL" > "$URL_FILE"

say "Step 4/4: starting the agent at $URL  (stop with Ctrl+C)"
(
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null "$URL/status"; then [ -n "${NO_BROWSER:-}" ] || open "$URL" 2>/dev/null || true; exit 0; fi
    sleep 0.5
  done
) &
exec "$PY" main.py start --port "$PORT"
