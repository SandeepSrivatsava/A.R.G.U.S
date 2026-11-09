#!/bin/bash
# Argus setup: installs Hermes Agent (the brain) and Argus (the always-listening voice) on your Mac,
# plus Telegram and iPhone access. Safe to run again; it keeps what already works.
#
#   ./setup.sh             normal setup (asks a few questions)
#   ./setup.sh --dry-run   show what would happen, change nothing
#
# Your keys are written only to ~/.hermes/.env (readable by you alone). They never go into
# this repo, and they are never passed on a command line where other programs could see them.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ENV_FILE="$HERMES_HOME/.env"
ARGUS_HOME="${ARGUS_HOME:-$HOME/.argus}"
VENV="${ARGUS_VENV:-$ARGUS_HOME/venv}"
APP_DIR="${ARGUS_APP_DIR:-$HOME/Applications}/Argus.app"
LAUNCH_AGENTS="${ARGUS_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
BIN_DIR="${ARGUS_BIN_DIR:-$HOME/.local/bin}"
WORKSPACE="${ARGUS_WORKSPACE:-$HOME/argus-workspace}"
MAIN_MODEL="${ARGUS_MAIN_MODEL:-claude-sonnet-5-5}"
SIDE_MODEL="${ARGUS_SIDE_MODEL:-claude-haiku-5-5}"
INSTALLER_URL="https://hermes-agent.nousresearch.com/install.sh"
API_PORT="${ARGUS_API_PORT:-8642}"
PHONE_PORT="${ARGUS_PHONE_PORT:-8765}"
ME="${USER:-$(id -un)}"
UID_NUM="$(id -u)"

# On-device speech models (sherpa-onnx releases), pinned by SHA-256.
MODEL_BASE="https://github.com/k2-fsa/sherpa-onnx/releases/download"
MODELS="
asr-models/silero_vad.onnx 9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6 silero_vad.onnx
asr-models/sherpa-onnx-moonshine-base-en-int8.tar.bz2 21870cecaa2e44e4e2bf63e02d1072bed183ccd10284871353bd9d24dad14e5e asr
tts-models/vits-piper-en_GB-alan-medium.tar.bz2 a48d4017da0f77668b27bed63fe6e04dd64c6397e1fadad4f460efb0ef7c9012 tts
speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012 speaker.onnx
"

DRY_RUN=0
ASSUME_YES=0
SKIP_INSTALL=0
NO_SERVICE=0
CHANGE_KEYS=0

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'
Options:
  --dry-run        print the steps without changing anything
  --yes            accept the default answer for yes/no questions
  --skip-install   do not install Hermes (it must already be installed)
  --no-service     configure only; do not start anything in the background
  --change-keys    ask for the Claude key, bot token and Telegram ID again
  -h, --help       show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --skip-install) SKIP_INSTALL=1 ;;
    --no-service) NO_SERVICE=1 ;;
    --change-keys) CHANGE_KEYS=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# ---------- output helpers ----------
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'; else B=; G=; Y=; R=; N=; fi
step() { printf '\n%s%s%s\n' "$B" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '%sError:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

is_tty() { [ -t 0 ]; }

# ask VAR "Question" "default" : uses $VAR if already set (lets tests and re-runs skip prompts)
ask() {
  local var="$1" question="$2" default="${3:-}" answer
  eval "answer=\${$var:-}"
  if [ -z "$answer" ]; then
    is_tty || die "$var is not set and there is no terminal to ask. Set $var and run again."
    if [ -n "$default" ]; then read -r -p "  $question [$default]: " answer; else read -r -p "  $question: " answer; fi
    answer="${answer:-$default}"
  fi
  eval "$var=\$answer"
}

# ask_secret VAR "Question" : like ask, but input is hidden
ask_secret() {
  local var="$1" question="$2" answer
  eval "answer=\${$var:-}"
  if [ -z "$answer" ]; then
    is_tty || die "$var is not set and there is no terminal to ask. Set $var and run again."
    read -r -s -p "  $question: " answer; echo
  fi
  eval "$var=\$answer"
}

# confirm "Question" y|n : returns 0 for yes
confirm() {
  local question="$1" default="$2" answer
  if [ "$ASSUME_YES" = 1 ] || ! is_tty; then [ "$default" = y ]; return; fi
  if [ "$default" = y ]; then read -r -p "  $question [Y/n]: " answer; else read -r -p "  $question [y/N]: " answer; fi
  answer="${answer:-$default}"
  case "$answer" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

sha256() { if command -v shasum > /dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi; }
esc() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }
toml_str() { printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"; }

# ---------- secrets go into ~/.hermes/.env through stdin, never argv ----------
env_get() { if [ -f "$ENV_FILE" ]; then sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; fi; }

env_set() {  # usage: printf '%s' "$value" | env_set KEY
  local key="$1" value tmp
  IFS= read -r value || true
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] set %s in %s\n' "$key" "$ENV_FILE"; return; fi
  mkdir -p "$HERMES_HOME"
  touch "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  tmp="$(mktemp "$HERMES_HOME/.env.XXXXXX")"
  grep -v "^${key}=" "$ENV_FILE" > "$tmp" || true
  [ -z "$value" ] || printf '%s=%s\n' "$key" "$value" >> "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

random_hex() {  # 48 hex characters
  openssl rand -hex 24 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(24))'
}

# ---------- Hermes helpers ----------
find_hermes() {
  if [ -n "${HERMES_BIN:-}" ]; then echo "$HERMES_BIN"; return 0; fi
  if command -v hermes >/dev/null 2>&1; then command -v hermes; return 0; fi
  if [ -x "$HOME/.local/bin/hermes" ]; then echo "$HOME/.local/bin/hermes"; return 0; fi
  return 1
}

h() { HERMES_HOME="$HERMES_HOME" "$HERMES" "$@"; }

cfg() {  # cfg key value  -> hermes config set (non-secret settings only)
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] hermes config set %s %s\n' "$1" "$2"; return; fi
  h config set "$1" "$2" > /dev/null || die "could not set $1"
}

# ---------- live checks (keys are fed to curl on stdin, not argv) ----------
telegram_bot_name() {  # prints @username of the bot for a token, or fails
  local resp
  resp="$(printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$1" | curl -fsS -m 15 -K - 2>/dev/null)" || return 1
  printf '%s' "$resp" | grep -q '"ok":true' || return 1
  printf '%s' "$resp" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p'
}

anthropic_check() {  # 0 = key works; sets ANTHROPIC_MODELS to the model list response
  local code out
  out="$(mktemp)"
  code="$(printf 'header = "x-api-key: %s"\n' "$1" | curl -sS -m 20 -K - -o "$out" -w '%{http_code}' \
          -H 'anthropic-version: 2023-06-01' 'https://api.anthropic.com/v1/models?limit=1000' 2>/dev/null)" || code=000
  ANTHROPIC_MODELS="$(cat "$out")"
  rm -f "$out"
  [ "$code" = 200 ]
}

find_tailscale_ip() {
  local exe ip
  for exe in tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale /opt/homebrew/bin/tailscale /usr/local/bin/tailscale; do
    ip="$("$exe" ip -4 2>/dev/null | head -1)" || continue
    case "$ip" in 100.*) printf '%s' "$ip"; return 0 ;; esac
  done
  return 1
}

# =====================================================================
step "1/10  Checking this Mac"
IS_MAC=0
case "$(uname -s)" in
  Darwin)
    [ "$(uname -m)" = arm64 ] || die "Argus needs an Apple Silicon Mac (this one reports $(uname -m))."
    IS_MAC=1
    macos="$(sw_vers -productVersion 2>/dev/null || echo 0)"
    [ "${macos%%.*}" -ge 13 ] 2>/dev/null || warn "macOS $macos is older than Ventura (13); Argus is tested on 13 and newer."
    ok "macOS $macos on Apple Silicon" ;;
  Linux)
    [ "${ARGUS_ALLOW_LINUX:-0}" = 1 ] || die "Argus's voice app runs on macOS. (Tests set ARGUS_ALLOW_LINUX=1.)"
    NO_SERVICE=1
    warn "Linux test mode: the Mac app, permissions and background services are skipped" ;;
  *) die "Unsupported system: $(uname -s)" ;;
esac
command -v curl > /dev/null || die "curl is missing"
if [ "$IS_MAC" = 1 ] && ! xcode-select -p > /dev/null 2>&1; then
  die "Apple's Command Line Tools are needed. Install them with: xcode-select --install   then run ./setup.sh again."
fi
if [ "${ARGUS_SKIP_VALIDATION:-0}" != 1 ]; then
  curl -fsS -m 10 -o /dev/null https://api.telegram.org 2>/dev/null && ok "Internet connection works" \
    || die "Cannot reach api.telegram.org. Check the internet connection and try again."
fi

if pgrep -fil openclaw > /dev/null 2>&1; then
  warn "OpenClaw looks like it is running:"
  pgrep -fil openclaw | sed 's/^/      /' >&2 || true
  warn "Older OpenClaw versions had serious security holes, and two agents on one bot will fight."
  warn "Quit it (or run: pkill -f openclaw) before continuing."
  confirm "Continue anyway?" n || exit 1
fi

# =====================================================================
step "2/10  Your details (keys stay on this Mac, in $ENV_FILE)"
ANTHROPIC_KEY="${ARGUS_ANTHROPIC_API_KEY:-}"
TG_TOKEN="${ARGUS_TELEGRAM_BOT_TOKEN:-}"
TG_USER="${ARGUS_TELEGRAM_USER_ID:-}"
if [ "$CHANGE_KEYS" = 0 ]; then   # re-runs keep the keys you already saved
  ANTHROPIC_KEY="${ANTHROPIC_KEY:-$(env_get ANTHROPIC_API_KEY)}"
  TG_TOKEN="${TG_TOKEN:-$(env_get TELEGRAM_BOT_TOKEN)}"
  TG_USER="${TG_USER:-$(env_get TELEGRAM_ALLOWED_USERS)}"
fi
[ -n "$ANTHROPIC_KEY" ] && ok "Claude API key: found (ending ...$(printf '%s' "$ANTHROPIC_KEY" | tail -c 4))"
[ -n "$TG_TOKEN" ] && ok "Telegram bot token: found"
[ -n "$TG_USER" ] && ok "Telegram user ID: $TG_USER"

ask_secret ANTHROPIC_KEY "Claude API key (from platform.claude.com, starts with sk-ant-)"
case "$ANTHROPIC_KEY" in sk-ant-*) ;; *) die "That doesn't look like a Claude API key (it should start with sk-ant-)." ;; esac

ask_secret TG_TOKEN "Telegram bot token (from @BotFather, looks like 123456:ABC...)"
printf '%s' "$TG_TOKEN" | grep -Eq '^[0-9]{5,15}:[A-Za-z0-9_-]{30,}$' \
  || die "That doesn't look like a Telegram bot token (numbers, a colon, then a long code)."

ask TG_USER "Your numeric Telegram user ID (from @userinfobot)"
printf '%s' "$TG_USER" | grep -Eq '^[0-9]{5,15}$' || die "The Telegram user ID should be digits only."

default_name="$( { id -F 2>/dev/null || getent passwd "$ME" 2>/dev/null | cut -d: -f5 | cut -d, -f1; } | awk '{print $1}' || true)"
NAME="${ARGUS_NAME:-}"
ask NAME "What should Argus call you?" "${default_name:-$ME}"
CITY="${ARGUS_CITY:-}"
ask CITY "Your city, for weather (e.g. Portland, OR)" ""
TZ_NAME="${ARGUS_TIMEZONE:-$(readlink /etc/localtime 2>/dev/null | sed 's#.*zoneinfo/##' || true)}"
[ -n "$TZ_NAME" ] || TZ_NAME="UTC"
ok "Time zone: $TZ_NAME"

SITES="${ARGUS_BLOCKED_SITES-unset}"
if [ "$SITES" = unset ]; then
  SITES=""
  if is_tty && [ ! -f "$ARGUS_HOME/config.toml" ]; then
    echo "  Argus will never open, read or type into websites you list here (your bank, payments, etc.)."
    read -r -p "  Sites to block, comma-separated (e.g. mybank.com, paypal.com) [none]: " SITES
  fi
fi
SITES_TOML="$(printf '%s' "$SITES" | tr ',' '\n' | sed -e 's/^ *//' -e 's/ *$//' -e 's#^https\{0,1\}://##' -e 's#/.*$##' \
  | grep -E '^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$' | tr 'A-Z' 'a-z' | sed 's/.*/"&"/' | paste -sd, - | sed 's/,/, /g' || true)"
[ -n "$SITES_TOML" ] && ok "Never-touch sites: $SITES_TOML"

TG_DESKTOP="${ARGUS_TELEGRAM_DESKTOP:-}"
if [ -z "$TG_DESKTOP" ]; then
  if confirm "Allow Telegram messages to use this Mac's screen and apps too (each action still needs your tap)?" n; then
    TG_DESKTOP=yes; else TG_DESKTOP=no; fi
fi

if [ "${ARGUS_SKIP_VALIDATION:-0}" = 1 ]; then
  warn "Skipping live key checks (ARGUS_SKIP_VALIDATION=1)"
else
  if bot="$(telegram_bot_name "$TG_TOKEN")"; then ok "Telegram bot works: @$bot"; else die "Telegram rejected that bot token."; fi
  if anthropic_check "$ANTHROPIC_KEY"; then
    ok "Claude API key works"
    if printf '%s' "$ANTHROPIC_MODELS" | grep -q "\"$MAIN_MODEL\""; then ok "Model $MAIN_MODEL is available"
    else warn "Model $MAIN_MODEL was not in your account's model list; Hermes may fail. Set ARGUS_MAIN_MODEL to one that is."; fi
  else
    die "The Claude API rejected that key (check it, and that your account has credit)."
  fi
fi

# =====================================================================
step "3/10  Installing Hermes Agent (the brain)"
if HERMES="$(find_hermes)"; then
  ok "Already installed: $("$HERMES" --version 2>/dev/null | head -1)"
elif [ "$SKIP_INSTALL" = 1 ]; then
  die "Hermes is not installed and --skip-install was given."
elif [ "$DRY_RUN" = 1 ]; then
  run curl -fsSL "$INSTALLER_URL" -o /tmp/hermes-install.sh
  run bash /tmp/hermes-install.sh --non-interactive
  HERMES=hermes
else
  installer="$(mktemp -t hermes-install.XXXXXX)"
  curl -fsSL "$INSTALLER_URL" -o "$installer" || die "Could not download the Hermes installer."
  ok "Downloaded the official installer (SHA-256 $(sha256 "$installer" | cut -c1-16)...)"
  # Includes cua-driver, which lets Argus see and use apps (every action is approval-gated).
  bash "$installer" --non-interactive || die "The Hermes installer failed (see ~/.hermes/logs/install.log)."
  rm -f "$installer"
  export PATH="$HOME/.local/bin:$PATH"
  HERMES="$(find_hermes)" || die "Hermes installed, but the 'hermes' command was not found. Open a new terminal and run ./setup.sh again."
  ok "Installed: $("$HERMES" --version 2>/dev/null | head -1)"
fi

# =====================================================================
step "4/10  Saving keys and connection settings"
printf '%s' "$ANTHROPIC_KEY" | env_set ANTHROPIC_API_KEY
printf '%s' "$TG_TOKEN" | env_set TELEGRAM_BOT_TOKEN
printf '%s' "$TG_USER" | env_set TELEGRAM_ALLOWED_USERS     # only you can talk to the bot
printf '%s' "$TG_USER" | env_set TELEGRAM_HOME_CHANNEL      # scheduled messages go to your chat
printf '%s' "true" | env_set API_SERVER_ENABLED             # local API the voice app and phone gateway use
printf '%s' "127.0.0.1" | env_set API_SERVER_HOST           # this Mac only, never the network
printf '%s' "$API_PORT" | env_set API_SERVER_PORT
printf '%s' "Argus" | env_set API_SERVER_MODEL_NAME
printf '' | env_set API_SERVER_CORS_ORIGINS                 # no browser pages may call it
API_KEY="$(env_get API_SERVER_KEY)"
if [ -z "$API_KEY" ]; then API_KEY="$(random_hex 48)"; printf '%s' "$API_KEY" | env_set API_SERVER_KEY; fi
ok "Saved to $ENV_FILE (permissions: owner only)"

# =====================================================================
step "5/10  Configuring the brain"
cfg model.provider anthropic
cfg model.default "$MAIN_MODEL"
for task in compression title_generation curator background_review; do   # cheap model for background chores
  cfg "auxiliary.$task.provider" anthropic
  cfg "auxiliary.$task.model" "$SIDE_MODEL"
done
cfg auth.adopt_external_logins false   # never borrow your Claude Code / Pro login
cfg approvals.mode manual              # risky actions always ask you first
cfg timezone "$TZ_NAME"
cfg terminal.cwd "$WORKSPACE"
cfg skills.external_dirs "[\"$REPO_DIR/skills\"]"
cfg tts.provider edge                  # spoken replies to Telegram voice notes
cfg tts.edge.voice en-GB-RyanNeural
cfg stt.enabled true                   # voice notes are transcribed on this Mac, for free
cfg stt.local.model base
ok "Main model $MAIN_MODEL, background tasks on $SIDE_MODEL, risky actions need your OK"

# Desktop control: on for the voice app (approval + Touch ID per action), off for Telegram unless you said yes.
run h tools enable computer_use --platform api_server > /dev/null
if [ "$TG_DESKTOP" = yes ]; then
  run h tools enable computer_use --platform telegram > /dev/null; ok "Telegram may use the screen (each action needs your tap)"
else
  run h tools disable computer_use --platform telegram > /dev/null; ok "Telegram can't use the screen and apps (voice at the Mac can)"
fi

# Privacy rules: Hermes asks Argus before every desktop/browser action; if the check fails, the action is blocked.
HOOK_CMD="$VENV/bin/python3 -I -m argus hook"
HOOK_JSON="{\"matcher\": \"computer_use|browser_.*|web_extract\", \"command\": \"$HOOK_CMD\", \"timeout\": 10, \"fail_closed\": true}"
existing_hooks="$( [ "$DRY_RUN" = 1 ] || h config get hooks.pre_tool_call 2>/dev/null || true)"
if [ -z "$existing_hooks" ] || printf '%s' "$existing_hooks" | grep -q -- "-m argus hook" && \
   [ "$(printf '%s\n' "$existing_hooks" | grep -c 'command:')" -le 1 ]; then
  cfg hooks.pre_tool_call "[$HOOK_JSON]"
else
  warn "You already have other pre_tool_call hooks in $HERMES_HOME/config.yaml, so setup didn't touch them."
  warn "Add this entry to hooks.pre_tool_call yourself: $HOOK_JSON"
fi
if [ "$DRY_RUN" = 0 ]; then
  python3 - "$HERMES_HOME/shell-hooks-allowlist.json" "$HOOK_CMD" <<'PY'
import datetime, json, os, sys
path, command = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path))
except (OSError, ValueError):
    data = {}
approvals = [a for a in data.get("approvals", []) if not (a.get("event") == "pre_tool_call" and "-m argus hook" in a.get("command", ""))]
approvals.append({"event": "pre_tool_call", "command": command,
                  "approved_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
data["approvals"] = approvals
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
fi
ok "Privacy rules wired in: password apps, Keychain, System Settings${SITES_TOML:+ and your blocked sites} are off-limits"

run mkdir -p "$WORKSPACE" "$HERMES_HOME/scripts"
run install -m 755 "$REPO_DIR/skills/news-briefing/scripts/news.py" "$HERMES_HOME/scripts/argus_news.py"
if [ "$DRY_RUN" = 1 ]; then
  printf '  [dry-run] write the Argus personality to %s/SOUL.md\n' "$HERMES_HOME"
else
  if [ -f "$HERMES_HOME/SOUL.md" ] && [ ! -f "$HERMES_HOME/SOUL.md.before-argus" ] \
     && ! grep -q "You are Argus" "$HERMES_HOME/SOUL.md"; then
    cp "$HERMES_HOME/SOUL.md" "$HERMES_HOME/SOUL.md.before-argus"
  fi
  sed -e "s|{{NAME}}|$(esc "$NAME")|g" -e "s|{{CITY}}|$(esc "${CITY:-an unspecified city}")|g" \
      -e "s|{{TIMEZONE}}|$(esc "$TZ_NAME")|g" "$REPO_DIR/hermes/SOUL.md.template" > "$HERMES_HOME/SOUL.md"
  chmod 600 "$HERMES_HOME/SOUL.md"
fi
ok "Personality, news skill and workspace ($WORKSPACE) in place"

# =====================================================================
step "6/10  Scheduling briefings"
add_job() {  # add_job NAME <hermes cron create arguments...>
  local name="$1" existing; shift
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] hermes cron create %s --name %s\n' "$*" "$name"; return; fi
  existing="$(h cron list 2>/dev/null || true)"
  if printf '%s\n' "$existing" | grep -q "Name: *$name *\$"; then ok "'$name' already scheduled"; return; fi
  h cron create "$@" --name "$name" > /dev/null || die "could not schedule $name"
  ok "Scheduled '$name'"
}
add_job argus-morning-headlines "0 7 * * *" --no-agent --script argus_news.py --deliver telegram
ok "Free headlines every day at 7:00 (no AI cost)"
want_brief="${ARGUS_AI_BRIEFING:-}"
if [ -z "$want_brief" ]; then
  if confirm "Also add an AI-written weekday briefing at 7:30 (weather + news; roughly \$1-3 a month)?" y; then want_brief=yes; else want_brief=no; fi
fi
if [ "$want_brief" = yes ]; then
  add_job argus-morning-brief "30 7 * * 1-5" \
    "Morning briefing for $NAME: today's weather in ${CITY:-my city}, the 5 most important headlines (use the news-briefing skill), and any reminders I asked you to keep. Under 150 words, friendly, no preamble." \
    --skill news-briefing --deliver telegram
fi

# =====================================================================
step "7/10  Installing the Argus voice app"
find_uv() {
  if command -v uv > /dev/null 2>&1; then command -v uv; return 0; fi
  for c in "$HERMES_HOME/bin/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do [ -x "$c" ] && { echo "$c"; return 0; }; done
  return 1
}
if [ "$DRY_RUN" = 1 ]; then
  run uv venv --python 3.12 --seed "$VENV"
  run "$VENV/bin/python3" -m pip install --require-hashes -r "$REPO_DIR/requirements-macos.lock"
else
  mkdir -p "$ARGUS_HOME" "$ARGUS_HOME/models" "$ARGUS_HOME/logs" "$ARGUS_HOME/run"
  chmod 700 "$ARGUS_HOME" "$ARGUS_HOME/models" "$ARGUS_HOME/logs" "$ARGUS_HOME/run"
  if [ ! -x "$VENV/bin/python3" ]; then
    if UV="$(find_uv)"; then
      "$UV" venv --python 3.12 --seed -q "$VENV" || die "Could not create the Python environment for Argus."
    else
      python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
        || die "Argus needs Python 3.11+ (Hermes's installer normally provides it through uv). Run the Hermes installer, then ./setup.sh again."
      python3 -m venv "$VENV" || die "Could not create the Python environment for Argus."
    fi
  fi
  if [ "${ARGUS_SKIP_DEPS:-0}" = 1 ]; then
    warn "Skipping Python packages (ARGUS_SKIP_DEPS=1)"
  else
    "$VENV/bin/python3" -m pip install -q --disable-pip-version-check --require-hashes --only-binary :all: \
      -r "$REPO_DIR/requirements-macos.lock" || die "Installing Argus's Python packages failed."
  fi
  site="$("$VENV/bin/python3" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  printf '%s\n' "$REPO_DIR" > "$site/argus-source.pth"     # the voice app runs straight from this folder
  ok "Python packages installed (hash-checked)"
fi

fetch_model() {  # fetch_model RELEASE_PATH SHA256 DEST
  local path="$1" want="$2" dest="$ARGUS_HOME/models/$3" tmp got unpack
  if [ -e "$dest" ] && [ "$(cat "$dest.sha256" 2>/dev/null)" = "$want" ]; then ok "$3 model already in place"; return; fi
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] download %s (sha256 %s...)\n' "$path" "${want:0:12}"; return; fi
  tmp="$(mktemp "$ARGUS_HOME/models/.download.XXXXXX")"
  if [ -n "${ARGUS_MODEL_MIRROR:-}" ]; then
    cp "$ARGUS_MODEL_MIRROR/$(basename "$path")" "$tmp" || die "missing $(basename "$path") in $ARGUS_MODEL_MIRROR"
  else
    curl -fL --retry 3 -m 1800 -s -o "$tmp" "$MODEL_BASE/$path" || { rm -f "$tmp"; die "Could not download $path"; }
  fi
  got="$(sha256 "$tmp")"
  [ "$got" = "$want" ] || { rm -f "$tmp"; die "Checksum mismatch for $path (got $got). Not installing it."; }
  rm -rf "$dest" "$dest.sha256"
  case "$path" in
    *.tar.bz2)
      unpack="$(mktemp -d "$ARGUS_HOME/models/.unpack.XXXXXX")"
      tar -xjf "$tmp" -C "$unpack" || die "Could not unpack $path"
      mv "$unpack"/*/ "$dest" && rm -rf "$unpack" "$dest/test_wavs" "$tmp" ;;
    *) mv "$tmp" "$dest" ;;
  esac
  printf '%s\n' "$want" > "$dest.sha256"
  ok "$3 model installed (checksum verified)"
}
while read -r path sum dest; do
  [ -n "$path" ] || continue
  fetch_model "$path" "$sum" "$dest"
done <<< "$MODELS"

CONFIG="$ARGUS_HOME/config.toml"
PHONE_IP=""
if PHONE_IP="$(find_tailscale_ip)"; then PHONE_ENABLED=true; else PHONE_ENABLED=false; fi
if [ "$DRY_RUN" = 1 ]; then
  printf '  [dry-run] write %s\n' "$CONFIG"
elif [ -f "$CONFIG" ] && [ "${ARGUS_REWRITE_CONFIG:-0}" != 1 ]; then
  ok "Kept your settings in $CONFIG"
  [ -z "$SITES_TOML" ] || warn "To change blocked sites, edit blocked_sites in $CONFIG, then: argusctl restart"
else
  umask 077
  cat > "$CONFIG" <<EOF
# Argus settings. Edit, then run: argusctl restart
name = "Argus"
user_name = $(toml_str "$NAME")

[listening]
followup_seconds = 8            # after a reply, you can answer without saying "Argus"
conversation_idle_seconds = 120 # conversation mode ends after this much quiet
voice_lock = false              # true = only answer your voice (record it first: argusctl enroll)
voice_lock_threshold = 0.45
mic_device = ""                 # "" = the Mac's current microphone

[voice]
engine = "piper"                # "piper" = on-device neural voice, "say" = built-in macOS voices
say_voice = "Daniel"
rate = 190
speed = 1.0
greeting = "Online and listening."

[brain]
session_idle_minutes = 30       # a new conversation thread starts after this much quiet

[security]
approval_needs_touch_id = true  # "Allow" buttons also need Touch ID or your Mac password
pause_when_locked = true        # microphone closes while the Mac is locked
blocked_sites = [$SITES_TOML]
# blocked_apps defaults to password managers, Keychain Access and System Settings.
# To change it, uncomment and list every app (names or bundle IDs):
# blocked_apps = ["1Password", "Bitwarden", "Passwords", "Keychain Access", "System Settings"]

[phone]
enabled = $PHONE_ENABLED
port = $PHONE_PORT
EOF
  umask 022
  ok "Settings written to $CONFIG"
fi

if [ "$IS_MAC" = 1 ]; then
  build_sum="$(cat "$REPO_DIR/macos/launcher.c" "$REPO_DIR/macos/Info.plist" | shasum -a 256 | cut -d' ' -f1)"
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] build %s\n' "$APP_DIR"
  elif [ "$(cat "$APP_DIR/Contents/Resources/build.sha256" 2>/dev/null)" = "$build_sum" ]; then
    ok "Argus.app is up to date ($APP_DIR)"
  else
    mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
    cp "$REPO_DIR/macos/Info.plist" "$APP_DIR/Contents/Info.plist"
    clang -O2 -Wall -o "$APP_DIR/Contents/MacOS/Argus" "$REPO_DIR/macos/launcher.c" \
      || die "Couldn't build Argus.app. Install Apple's Command Line Tools (xcode-select --install) and try again."
    printf '%s\n' "$build_sum" > "$APP_DIR/Contents/Resources/build.sha256"
    codesign --force --sign - "$APP_DIR" > /dev/null 2>&1 || warn "Couldn't sign Argus.app locally (it should still run)"
    ok "Built Argus.app in $APP_DIR"
  fi
fi
if [ "$DRY_RUN" = 0 ]; then
  mkdir -p "$BIN_DIR"
  ln -sf "$REPO_DIR/bin/argusctl" "$BIN_DIR/argusctl"
fi
ok "Command-line control: argusctl (try: argusctl status)"

# =====================================================================
step "8/10  Starting everything in the background"
install_agent() {  # install_agent LABEL
  local label="$1" plist="$LAUNCH_AGENTS/$1.plist"
  mkdir -p "$LAUNCH_AGENTS"
  sed -e "s|@APP@|$(esc "$APP_DIR")|g" -e "s|@LOGS@|$(esc "$ARGUS_HOME/logs")|g" \
      -e "s|@PYTHON@|$(esc "$VENV/bin/python3")|g" "$REPO_DIR/macos/$label.plist" > "$plist"
  launchctl bootout "gui/$UID_NUM/$label" > /dev/null 2>&1 || true
  launchctl bootstrap "gui/$UID_NUM" "$plist" || die "Couldn't start $label"
}
if [ "$NO_SERVICE" = 1 ]; then
  warn "Skipped (--no-service or test mode)."
else
  run h gateway install --start-now --start-on-login
  if [ "$DRY_RUN" = 0 ]; then h gateway restart > /dev/null 2>&1 || true; fi
  ok "Hermes (brain, Telegram, schedules) runs in the background and starts at login"
  if [ "$DRY_RUN" = 1 ]; then
    run launchctl bootstrap "gui/$UID_NUM" "$LAUNCH_AGENTS/com.argus.voice.plist"
  else
    install_agent com.argus.voice
    ok "Argus voice app starts at login (menu bar ◉)"
    if [ "$PHONE_ENABLED" = true ]; then
      install_agent com.argus.phone
      ok "iPhone gateway listening on Tailscale ($PHONE_IP:$PHONE_PORT)"
    else
      launchctl bootout "gui/$UID_NUM/com.argus.phone" > /dev/null 2>&1 || true
      warn "Tailscale isn't connected, so the iPhone Siri shortcut is off for now (see docs/IPHONE.md)."
    fi
  fi
  want_awake="${ARGUS_STAY_AWAKE:-}"
  if [ -z "$want_awake" ]; then
    if confirm "Keep this Mac awake while it's plugged in, so Argus can always answer?" y; then want_awake=yes; else want_awake=no; fi
  fi
  if [ "$want_awake" = yes ]; then run "$REPO_DIR/scripts/stay-awake.sh" on; fi
fi

# =====================================================================
step "9/10  Permissions (macOS will ask; click Allow)"
if [ "$IS_MAC" = 1 ] && [ "$NO_SERVICE" = 0 ] && [ "$DRY_RUN" = 0 ]; then
  echo "  • Microphone: a box saying \"Argus would like to access the microphone\" appears now. Click Allow."
  echo "    (Speech is processed on this Mac. Only what you say to Argus is sent to Claude.)"
  echo "  • Screen and apps: macOS will ask you to allow CuaDriver in Accessibility and Screen Recording."
  if is_tty && confirm "Open those Accessibility / Screen Recording prompts now?" y; then
    h computer-use permissions grant || warn "Run later: hermes computer-use permissions grant"
  else
    warn "Later, run: hermes computer-use permissions grant"
  fi
  echo "  • Approvals: when Argus wants to click or type, a box appears; Allow asks for Touch ID."
else
  ok "Skipped here"
fi

# =====================================================================
step "10/10  Saying hello"
if [ "$DRY_RUN" = 1 ]; then
  run h send -t telegram "Argus is online."
elif [ "${ARGUS_SEND_HELLO:-yes}" = no ]; then
  ok "Skipped (ARGUS_SEND_HELLO=no)"
elif h send -t telegram "Good day, $NAME. Argus is online: talk to me at your Mac (just say \"Argus\"), through Siri, or here." > /dev/null 2>&1; then
  ok "Sent you a hello on Telegram"
else
  warn "Couldn't send the hello message. Open your bot in Telegram, tap Start, then run: hermes send -t telegram hello"
fi

cat <<EOF

${B}Done.${N} Say "Argus, what's the news today?" near your Mac.
  - Menu bar ◉: microphone, conversation mode, screen access, stop, activity log
  - Teach it your voice (recommended): argusctl enroll
  - iPhone (Siri / Back Tap): docs/IPHONE.md$( [ "$PHONE_ENABLED" = true ] && printf ' (address http://%s:%s, token: argusctl phone-token)' "$PHONE_IP" "$PHONE_PORT" )
  - Telegram: message your bot any time
  - Health check: ./scripts/doctor.sh
EOF
