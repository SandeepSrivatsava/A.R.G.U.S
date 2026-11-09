#!/bin/bash
# Argus health check: shows what works and what needs attention. Changes nothing.
# Keys are checked by sending them to Telegram/Anthropic on curl's stdin; they are never printed.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ENV_FILE="$HERMES_HOME/.env"
if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'; else G=; Y=; R=; N=; fi
PROBLEMS=0
pass() { printf '%s✓%s %s\n' "$G" "$N" "$*"; }
note() { printf '%s!%s %s\n' "$Y" "$N" "$*"; }
fail() { printf '%s✗%s %s\n' "$R" "$N" "$*"; PROBLEMS=$((PROBLEMS + 1)); }
env_get() { if [ -f "$ENV_FILE" ]; then sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; fi; }

HERMES="${HERMES_BIN:-$(command -v hermes 2>/dev/null || echo "$HOME/.local/bin/hermes")}"
if [ -x "$HERMES" ]; then
  pass "Hermes: $("$HERMES" --version 2>/dev/null | head -1)"
else
  fail "Hermes is not installed. Run ./setup.sh"
  exit 1
fi

if [ -f "$ENV_FILE" ]; then
  perms="$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE" 2>/dev/null)"
  [ "$perms" = 600 ] && pass "Key file is private ($ENV_FILE)" || fail "Key file permissions are $perms; fix with: chmod 600 $ENV_FILE"
  for key in ANTHROPIC_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_USERS TELEGRAM_HOME_CHANNEL API_SERVER_KEY; do
    [ -n "$(env_get "$key")" ] || fail "$key is missing from $ENV_FILE (run ./setup.sh)"
  done
else
  fail "No key file at $ENV_FILE (run ./setup.sh)"
fi

if "$HERMES" gateway status 2>&1 | grep -qi "not running"; then
  fail "Argus's background service is not running. Start it: hermes gateway start"
else
  pass "Background service is running"
fi

token="$(env_get TELEGRAM_BOT_TOKEN)"
if [ -n "$token" ]; then
  resp="$(printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$token" | curl -fsS -m 15 -K - 2>/dev/null || true)"
  if printf '%s' "$resp" | grep -q '"ok":true'; then
    pass "Telegram bot: @$(printf '%s' "$resp" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
  else
    fail "Telegram didn't accept the bot token (or there's no internet)"
  fi
fi

key="$(env_get ANTHROPIC_API_KEY)"
if [ -n "$key" ]; then
  code="$(printf 'header = "x-api-key: %s"\n' "$key" | curl -sS -m 20 -K - -o /dev/null -w '%{http_code}' \
          -H 'anthropic-version: 2023-06-01' https://api.anthropic.com/v1/models 2>/dev/null || echo 000)"
  case "$code" in
    200) pass "Claude API key works" ;;
    401|403) fail "Claude API rejected the key (HTTP $code). Create a new key at platform.claude.com" ;;
    *) fail "Couldn't reach the Claude API (HTTP $code)" ;;
  esac
fi

port="$(env_get API_SERVER_PORT)"; port="${port:-8642}"
if curl -fsS -m 5 "http://127.0.0.1:$port/health" > /dev/null 2>&1; then
  pass "Local API answers on 127.0.0.1:$port (the voice app and iPhone gateway use it)"
else
  fail "Local API on port $port isn't answering, so Argus can't hear you. Restart: hermes gateway restart"
fi

if "$HERMES" hooks list 2>/dev/null | grep -q "argus hook.*allowed"; then
  pass "Privacy rules are active for desktop and browser actions"
else
  fail "Argus's privacy hook isn't registered. Run ./setup.sh again"
fi
if "$HERMES" tools list --platform telegram 2>/dev/null | grep -q "disabled  computer_use"; then
  pass "Telegram can't use the screen and apps"
else
  note "Telegram is allowed to use the screen and apps (each action still needs your tap)"
fi

ARGUSCTL="$REPO_DIR/bin/argusctl"
if [ -x "$HOME/.argus/venv/bin/python3" ]; then
  echo "-- Argus voice app --"
  "$ARGUSCTL" doctor | sed -e "s/^PASS/${G}✓${N}/" -e "s/^FAIL/${R}✗${N}/" || PROBLEMS=$((PROBLEMS + 1))
else
  fail "The Argus voice app isn't installed. Run ./setup.sh"
fi
if [ "$(uname -s)" = Darwin ]; then
  if "$HERMES" computer-use permissions status 2>/dev/null | grep -qi "denied\|not granted\|missing"; then
    note "Screen control needs Accessibility + Screen Recording for CuaDriver: hermes computer-use permissions grant"
  fi
fi

jobs="$("$HERMES" cron list 2>/dev/null || true)"
if printf '%s' "$jobs" | grep -q "argus-morning-headlines"; then pass "Morning headlines are scheduled"; else note "No morning headlines job (run ./setup.sh to add it)"; fi

if [ -f "$HERMES_HOME/scripts/argus_news.py" ]; then
  if python3 "$HERMES_HOME/scripts/argus_news.py" --top 1 > /dev/null 2>&1; then
    pass "News feeds are reachable"
  else
    fail "The news script couldn't fetch any feed (no internet, or python3 is missing)"
  fi
fi

if pgrep -fil openclaw > /dev/null 2>&1; then
  fail "OpenClaw is running. Quit it (pkill -f openclaw); older versions had serious security holes."
fi

if [ "$(uname -s)" = Darwin ]; then
  if [ "$("$REPO_DIR/scripts/stay-awake.sh" status)" = on ]; then
    pass "Mac stays awake while plugged in"
  else
    note "Mac may sleep, and Argus can't answer while it does (fix: ./scripts/stay-awake.sh on)"
  fi
fi

update="$("$HERMES" update --check 2>&1 | tail -1 || true)"
printf '%s' "$update" | grep -qi "available" && note "Hermes update: $update (run ./scripts/update.sh)"

echo
if [ "$PROBLEMS" = 0 ]; then echo "All good."; else echo "$PROBLEMS problem(s) found."; fi
exit $(( PROBLEMS > 0 ? 1 : 0 ))
