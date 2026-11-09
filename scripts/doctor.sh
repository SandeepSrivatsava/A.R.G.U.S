#!/bin/bash
# Argus health check: shows what works and what needs attention. Changes nothing.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARGUS_HOME="${ARGUS_HOME:-$HOME/.argus}"
if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'; else G=; Y=; R=; N=; fi
PROBLEMS=0
pass() { printf '%s✓%s %s\n' "$G" "$N" "$*"; }
note() { printf '%s!%s %s\n' "$Y" "$N" "$*"; }
fail() { printf '%s✗%s %s\n' "$R" "$N" "$*"; PROBLEMS=$((PROBLEMS + 1)); }
perm() { stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1" 2>/dev/null; }

[ -x "$ARGUS_HOME/venv/bin/python3" ] || { fail "Argus isn't installed. Run ./setup.sh"; exit 1; }
for f in "$ARGUS_HOME/secrets.env" "$ARGUS_HOME/config.toml"; do
  if [ -f "$f" ]; then
    if [ "$(perm "$f")" = 600 ]; then pass "$(basename "$f") is private"; else fail "$f is readable by others: chmod 600 $f"; fi
  else
    fail "$f is missing (run ./setup.sh)"
  fi
done
if [ "$(perm "$ARGUS_HOME")" = 700 ]; then pass "$ARGUS_HOME is private"; else fail "$ARGUS_HOME should be private: chmod 700 $ARGUS_HOME"; fi

"$REPO_DIR/bin/argusctl" doctor | sed -e "s/^PASS  /${G}✓${N} /" -e "s/^FAIL  /${R}✗${N} /" || PROBLEMS=$((PROBLEMS + 1))

if [ "$(uname -s)" = Darwin ]; then
  if launchctl print "gui/$(id -u)/com.argus.voice" > /dev/null 2>&1; then pass "Argus starts at login"
  else fail "Argus isn't set to start at login (run ./setup.sh)"; fi
  if [ "$("$REPO_DIR/scripts/stay-awake.sh" status)" = on ]; then pass "Mac stays awake while plugged in"
  else note "Mac may sleep, and Argus can't answer while it does (fix: ./scripts/stay-awake.sh on)"; fi
fi
if pgrep -fil openclaw > /dev/null 2>&1; then
  fail "OpenClaw is running. Quit it (pkill -f openclaw); older versions had serious security holes."
fi
echo
[ "$PROBLEMS" = 0 ] && echo "All good." || echo "$PROBLEMS problem(s) above."
exit $(( PROBLEMS > 0 ))
