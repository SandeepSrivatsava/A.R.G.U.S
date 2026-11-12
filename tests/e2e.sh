#!/bin/bash
# End-to-end test, no real keys needed:
#   1. runs setup.sh non-interactively into a throwaway home and checks every setting, the private
#      key file and the checksum-verified speech models; then again, to prove re-running is safe;
#   2. starts a mock Claude API, then runs the real Argus app (tests/e2e_voice.py): synthesised speech
#      through the installed models, real tools, approvals, privacy rules, reminders and
#      the iPhone gateway.
# Usage: ARGUS_VENV=/path/to/venv ARGUS_MODEL_MIRROR=/dir tests/e2e.sh
#        (ARGUS_COVERAGE=/path/.coverage.e2e also measures which code the run exercised; needs coverage.py)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${ARGUS_VENV:?set ARGUS_VENV to a venv with numpy + sherpa-onnx}"
WORK="$(mktemp -d)"
export ARGUS_HOME="$WORK/argus-home"
MOCK_PORT="${MOCK_PORT:-9921}"
PHONE_PORT="${PHONE_PORT:-18765}"
export DANGER_DIR="$WORK/precious"
FAILED=0
PIDS=""
cleanup() { for p in $PIDS; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

check() {  # check "description" command...
  local desc="$1"; shift
  if "$@" > /dev/null 2>&1; then echo "PASS  $desc"; else echo "FAIL  $desc"; FAILED=1; fi
}
perm() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }

run_setup() {
  ARGUS_ANTHROPIC_API_KEY="sk-ant-test" \
  ARGUS_NAME="Sandeep" ARGUS_CITY="Portland, OR" ARGUS_TIMEZONE="America/Los_Angeles" \
  ARGUS_BLOCKED_SITES="MyBank.com, https://www.paypal.com/in/, not a site" ARGUS_AI_BRIEFING=yes \
  ARGUS_SKIP_VALIDATION=1 ARGUS_SEND_HELLO=no ARGUS_ALLOW_LINUX=1 ARGUS_SKIP_DEPS=1 ARGUS_VENV="$VENV" \
  ARGUS_BIN_DIR="$WORK/bin" ARGUS_WORKSPACE="$WORK/workspace" ARGUS_PHONE=iphone \
    bash "$REPO/setup.sh" < /dev/null
}

echo "== 1. setup.sh (non-interactive)"
run_setup > "$WORK/setup1.log" 2>&1 || { cat "$WORK/setup1.log"; echo "FAIL  setup.sh exited non-zero"; exit 1; }
S="$ARGUS_HOME/secrets.env"; C="$ARGUS_HOME/config.toml"
check "Argus home is private (700)" test "$(perm "$ARGUS_HOME")" = 700
check "keys file is private (600)" test "$(perm "$S")" = 600
check "Claude key saved" grep -qx 'ANTHROPIC_API_KEY=sk-ant-test' "$S"
check "no Telegram token kept" sh -c "! grep -q '^TELEGRAM' '$S'"
check "settings are private (600)" test "$(perm "$C")" = 600
check "no secret in the settings file" sh -c "! grep -q 'sk-ant' '$C'"
check "no Telegram settings" sh -c "! grep -qi 'telegram' '$C'"
check "blocked sites cleaned up and saved" grep -qx 'blocked_sites = \["mybank.com", "www.paypal.com"\]' "$C"
check "AI briefing scheduled for weekdays" grep -q 'morning_briefing = "30 7 \* \* 1-5"' "$C"
check "set up for an iPhone, with Apple Maps links" sh -c "grep -q '^type = \"iphone\"' '$C' && grep -q '^app = \"apple\"' '$C'"
check "settings load cleanly" "$VENV/bin/python3" -I -c "from argus import config; s = config.load(); assert s.city == 'Portland, OR'"
check "speech models installed with verified checksums" sh -c \
  "test -f '$ARGUS_HOME/models/silero_vad.onnx' && test -f '$ARGUS_HOME/models/asr/tokens.txt' && test -d '$ARGUS_HOME/models/tts/espeak-ng-data' && test -f '$ARGUS_HOME/models/kokoro/voices.bin' && test -f '$ARGUS_HOME/models/speaker.onnx.sha256'"
check "Argus runs from this folder" sh -c "'$VENV/bin/python3' -I -c 'import argus, sys; sys.exit(not argus.__file__.startswith(\"$REPO\"))'"
check "argusctl installed" test -x "$WORK/bin/argusctl"

echo "== 2. setup.sh again (must be safe to re-run)"
run_setup > "$WORK/setup2.log" 2>&1 || { cat "$WORK/setup2.log"; echo "FAIL  second setup.sh run exited non-zero"; exit 1; }
check "models not downloaded twice" test "$(grep -c 'model already in place' "$WORK/setup2.log")" = 5
check "settings kept" grep -q 'Kept your settings' "$WORK/setup2.log"
check "no duplicate keys" test "$(grep -c '^ANTHROPIC_API_KEY=' "$S")" = 1

echo "== 3. talking to Argus (installed models, real brain and tools, mock Claude)"
mkdir -p "$DANGER_DIR" && echo "do not delete" > "$DANGER_DIR/keep.txt"
MOCK_KEY="sk-ant-test" python3 "$REPO/tests/mock_claude.py" "$MOCK_PORT" "$WORK/mock.log" > "$WORK/mock.out" 2>&1 &
PIDS="$PIDS $!"
sed -i.bak -e "s|^\[brain\]$|[brain]\napi_base = \"http://127.0.0.1:$MOCK_PORT\"|" \
  -e "s/^enabled = false                 # iPhone/enabled = true                 # iPhone/" \
  -e "s/^port = 8765$/port = $PHONE_PORT\nhost = \"127.0.0.1\"/" "$C"
grep -q '^enabled = true                 # iPhone' "$C" || sed -i.bak2 -e '/^\[phone\]$/,/^\[/{s/^enabled = .*/enabled = true/}' "$C"
for _ in $(seq 1 50); do curl -s -m 1 -o /dev/null "http://127.0.0.1:$MOCK_PORT/" && break; sleep 0.1; done
PYRUN=("$VENV/bin/python3")
if [ -n "${ARGUS_COVERAGE:-}" ]; then
  PYRUN+=(-m coverage run --branch --source="$REPO/argus" --data-file="$ARGUS_COVERAGE")
fi
(cd "$WORK" && ARGUS_NEWS_OFFLINE="$REPO/tests/fixtures/google_news.xml" "${PYRUN[@]}" "$REPO/tests/e2e_voice.py" "$DANGER_DIR" "$WORK") 2>&1 \
  | grep -v -i "warning" | tee "$WORK/voice.log" | grep -E '^(PASS|FAIL|SKIP|spoken)' || true
grep -q '^FAIL' "$WORK/voice.log" && FAILED=1
grep -q '^PASS' "$WORK/voice.log" || { echo "FAIL  voice test did not run"; tail -30 "$WORK/voice.log"; FAILED=1; }

echo
if [ "$FAILED" = 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED (logs in $WORK)"; exit 1; fi
