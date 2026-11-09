#!/bin/bash
# End-to-end test, no real keys needed:
#   1. runs setup.sh non-interactively against throwaway Hermes and Argus homes and checks every
#      setting, the privacy hook registration and the checksum-verified speech models,
#   2. runs it again to prove re-running is safe,
#   3. starts the real Hermes gateway with a mock model; checks the local API, the key, and that the
#      agent really runs the news script,
#   4. speaks to Argus: synthesised speech -> installed speech models -> listening loop -> Hermes Runs
#      API, including a held-and-denied dangerous command and a website blocked by the privacy hook,
#   5. asks through the iPhone gateway (token required).
# Usage: HERMES_BIN=/path/to/hermes ARGUS_VENV=/path/to/venv ARGUS_MODEL_MIRROR=/dir tests/e2e_hermes.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_BIN="${HERMES_BIN:?set HERMES_BIN to the hermes executable}"
VENV="${ARGUS_VENV:?set ARGUS_VENV to a venv with numpy + sherpa-onnx}"
WORK="$(mktemp -d)"
export HERMES_HOME="$WORK/hermes-home"
export ARGUS_HOME="$WORK/argus-home"
MOCK_PORT="${MOCK_PORT:-9911}"
API_PORT="${API_PORT:-18642}"
PHONE_PORT="${PHONE_PORT:-18765}"
DANGER_DIR="$WORK/precious"
FAILED=0
PIDS=""
cleanup() {  # stop background processes and wait for them, so the next run starts clean
  for p in $PIDS; do kill "$p" 2>/dev/null || true; done
  for p in $PIDS; do
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$p" 2>/dev/null || break; sleep 1; done
    kill -9 "$p" 2>/dev/null || true
  done
}
trap cleanup EXIT

check() {  # check "description" command...
  local desc="$1"; shift
  if "$@" > /dev/null 2>&1; then echo "PASS  $desc"; else echo "FAIL  $desc"; FAILED=1; fi
}
hc() { "$HERMES_BIN" "$@"; }
count_jobs() { hc cron list 2>/dev/null | grep -c "Name: *$1 *\$" || true; }
perm() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }

run_setup() {
  ARGUS_ANTHROPIC_API_KEY="sk-ant-test-key-0000" \
  ARGUS_TELEGRAM_BOT_TOKEN="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" \
  ARGUS_TELEGRAM_USER_ID="987654321" \
  ARGUS_NAME="Sandeep" ARGUS_CITY="Portland, OR" ARGUS_TIMEZONE="America/Los_Angeles" \
  ARGUS_BLOCKED_SITES="MyBank.com, https://www.paypal.com/in/, not a site" ARGUS_TELEGRAM_DESKTOP=no \
  ARGUS_AI_BRIEFING=yes ARGUS_SKIP_VALIDATION=1 ARGUS_SEND_HELLO=no ARGUS_ALLOW_LINUX=1 \
  ARGUS_SKIP_DEPS=1 ARGUS_VENV="$VENV" ARGUS_BIN_DIR="$WORK/bin" \
  ARGUS_WORKSPACE="$WORK/workspace" ARGUS_API_PORT="$API_PORT" HERMES_BIN="$HERMES_BIN" \
    bash "$REPO/setup.sh" --skip-install --no-service < /dev/null
}

echo "== 1. setup.sh (non-interactive)"
run_setup > "$WORK/setup1.log" 2>&1 || { cat "$WORK/setup1.log"; echo "FAIL  setup.sh exited non-zero"; exit 1; }
ENV="$HERMES_HOME/.env"
check ".env is private (600)" test "$(perm "$ENV")" = 600
check "Claude key saved" grep -qx 'ANTHROPIC_API_KEY=sk-ant-test-key-0000' "$ENV"
check "only the owner's Telegram ID is allowed" grep -qx 'TELEGRAM_ALLOWED_USERS=987654321' "$ENV"
check "local API bound to this machine only" grep -qx 'API_SERVER_HOST=127.0.0.1' "$ENV"
check "no browser origins may call the local API" sh -c "! grep -q '^API_SERVER_CORS_ORIGINS=' '$ENV'"
check "local API key generated" grep -Eq '^API_SERVER_KEY=[0-9a-f]{48}$' "$ENV"
check "no secret leaked into config.yaml" sh -c "! grep -q 'sk-ant-test-key' '$HERMES_HOME/config.yaml'"
check "main model is Sonnet 5.5" test "$(hc config get model.default)" = claude-sonnet-5-5
check "risky actions need approval" test "$(hc config get approvals.mode)" = manual
check "Claude Code / Pro login is never borrowed" sh -c "'$HERMES_BIN' config get auth.adopt_external_logins | grep -qi false"
check "voice app may use the screen (computer_use on for the local API)" \
  sh -c "'$HERMES_BIN' tools list --platform api_server | grep -q 'enabled  computer_use'"
check "Telegram may not use the screen" sh -c "'$HERMES_BIN' tools list --platform telegram | grep -q 'disabled  computer_use'"
check "privacy hook registered, allowlisted and fail-closed" sh -c \
  "'$HERMES_BIN' hooks list | grep -q 'argus hook.*allowed' && grep -q 'fail_closed: true' '$HERMES_HOME/config.yaml'"
check "Argus personality installed" grep -q "You are Argus, the personal assistant of Sandeep" "$HERMES_HOME/SOUL.md"
check "free 7:00 headlines job scheduled" test "$(count_jobs argus-morning-headlines)" = 1
check "speech models installed with verified checksums" sh -c \
  "test -f '$ARGUS_HOME/models/silero_vad.onnx' && test -f '$ARGUS_HOME/models/asr/tokens.txt' && test -d '$ARGUS_HOME/models/tts/espeak-ng-data' && test -f '$ARGUS_HOME/models/speaker.onnx.sha256'"
check "Argus home is private (700)" test "$(perm "$ARGUS_HOME")" = 700
check "Argus settings private (600)" test "$(perm "$ARGUS_HOME/config.toml")" = 600
check "blocked sites cleaned up and saved" grep -qx 'blocked_sites = \["mybank.com", "www.paypal.com"\]' "$ARGUS_HOME/config.toml"
check "voice app runs from this folder" sh -c "'$VENV/bin/python3' -I -c 'import argus, sys; sys.exit(not argus.__file__.startswith(\"$REPO\"))'"
check "argusctl installed" test -x "$WORK/bin/argusctl"

echo "== 2. setup.sh again (must be safe to re-run)"
key_before="$(sed -n 's/^API_SERVER_KEY=//p' "$ENV")"
run_setup > "$WORK/setup2.log" 2>&1 || { cat "$WORK/setup2.log"; echo "FAIL  second setup.sh run exited non-zero"; exit 1; }
check "no duplicate headlines job" test "$(count_jobs argus-morning-headlines)" = 1
check "local API key kept" test "$(sed -n 's/^API_SERVER_KEY=//p' "$ENV")" = "$key_before"
check "models not downloaded twice" test "$(grep -c 'model already in place' "$WORK/setup2.log")" = 4
check "settings kept" grep -q 'Kept your settings' "$WORK/setup2.log"
check "hook allowlist has a single Argus entry" test "$(grep -c 'argus hook' "$HERMES_HOME/shell-hooks-allowlist.json")" = 1

echo "== 3. real Hermes agent + mock model, over the local API"
mkdir -p "$DANGER_DIR" && echo "do not delete" > "$DANGER_DIR/keep.txt"
python3 "$REPO/tests/mock_llm.py" "$MOCK_PORT" "$WORK/mock.log" "$HERMES_HOME/scripts/argus_news.py" \
  "$REPO/tests/fixtures/google_news.xml" "$DANGER_DIR" &
PIDS="$PIDS $!"
hc config set model.provider custom > /dev/null
hc config set model.base_url "http://127.0.0.1:$MOCK_PORT/v1" > /dev/null
hc config set model.default mock-model > /dev/null
for task in compression title_generation curator background_review; do
  hc config set "auxiliary.$task.provider" main > /dev/null 2>&1 || hc config unset "auxiliary.$task" > /dev/null
done
hc config set platforms.telegram.enabled false > /dev/null 2>&1 || true
if curl -s -m 2 -o /dev/null "http://127.0.0.1:$API_PORT/health"; then
  echo "FAIL  port $API_PORT is already in use (a gateway from an earlier run?)"; exit 1
fi
"$HERMES_BIN" gateway run > "$WORK/gateway.log" 2>&1 &   # direct exec, so $! is the gateway itself
PIDS="$PIDS $!"
for _ in $(seq 1 90); do
  curl -fsS -m 2 "http://127.0.0.1:$API_PORT/health" > /dev/null 2>&1 && break
  sleep 1
done
check "Hermes local API is up" curl -fsS -m 2 "http://127.0.0.1:$API_PORT/health"
API_KEY="$(sed -n 's/^API_SERVER_KEY=//p' "$ENV")"
check "local API refuses requests without the key" sh -c \
  "test \"\$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:$API_PORT/v1/chat/completions)\" = 401"
curl -sS -m 180 "http://127.0.0.1:$API_PORT/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"Argus","messages":[{"role":"user","content":"What are the top headlines today?"}]}' \
  > "$WORK/response.json" 2>&1 || true
check "agent answered with the first story from the news script" grep -q "Top story: Storm closes Bay Area highways" "$WORK/response.json"
check "the terminal tool really ran the script" grep -q '"role": "tool"' "$WORK/mock.log"

echo "== 4. talking to Argus (real speech models, real Hermes Runs API)"
(cd "$WORK" && "$VENV/bin/python3" "$REPO/tests/e2e_voice.py" "$ENV" "$DANGER_DIR" "$WORK") 2>&1 \
  | grep -v -i "warning" | tee "$WORK/voice.log" | grep -E '^(PASS|FAIL|SKIP|spoken)' || true
grep -q '^FAIL' "$WORK/voice.log" && FAILED=1
grep -q '^PASS' "$WORK/voice.log" || { echo "FAIL  voice test did not run"; tail -20 "$WORK/voice.log"; FAILED=1; }

echo "== 5. iPhone gateway"
sed -i.bak -e "s/^port = 8765$/port = $PHONE_PORT/" -e 's/^enabled = false$/enabled = true/' \
  -e 's/^\[phone\]$/[phone]\nhost = "127.0.0.1"/' "$ARGUS_HOME/config.toml"
"$VENV/bin/python3" -I -m argus phone > "$WORK/phone.log" 2>&1 &
PIDS="$PIDS $!"
TOKEN="$("$VENV/bin/python3" -I -m argus phone-token)"
for _ in $(seq 1 30); do
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$PHONE_PORT/ping" && break
  sleep 0.5
done
check "phone token is private (600)" test "$(perm "$ARGUS_HOME/phone_token")" = 600
check "phone gateway refuses a wrong token" sh -c \
  "test \"\$(curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer nope' -d '{\"text\":\"hi\"}' http://127.0.0.1:$PHONE_PORT/ask)\" = 401"
printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" | curl -sS -m 120 -K - -H 'Content-Type: application/json' \
  -d '{"text":"What are the top headlines today?"}' "http://127.0.0.1:$PHONE_PORT/ask" > "$WORK/phone.json" 2>&1 || true
check "Siri-style request answered through the gateway" grep -q '"reply": "Top story: Storm closes Bay Area highways"' "$WORK/phone.json"

echo
if [ "$FAILED" = 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED (logs in $WORK)"; trap - EXIT; cleanup; exit 1; fi
