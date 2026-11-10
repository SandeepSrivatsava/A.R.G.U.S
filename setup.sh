#!/bin/bash
# Argus setup: installs Argus (our own voice assistant: brain, ears, voice, iPhone gateway) on
# your Mac and starts it at login. Safe to run again; it keeps what already works.
#
#   ./setup.sh             normal setup (asks a few questions)
#   ./setup.sh --dry-run   show what would happen, change nothing
#
# Keys go only into ~/.argus/secrets.env (readable by you alone). They never go into this repo,
# and they are never passed on a command line where other programs could see them.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
ARGUS_HOME="${ARGUS_HOME:-$HOME/.argus}"
SECRETS="$ARGUS_HOME/secrets.env"
CONFIG="$ARGUS_HOME/config.toml"
VENV="${ARGUS_VENV:-$ARGUS_HOME/venv}"
APP_DIR="${ARGUS_APP_DIR:-$HOME/Applications}/Argus.app"
LAUNCH_AGENTS="${ARGUS_LAUNCH_AGENTS:-$HOME/Library/LaunchAgents}"
BIN_DIR="${ARGUS_BIN_DIR:-$HOME/.local/bin}"
WORKSPACE="${ARGUS_WORKSPACE:-$HOME/argus-workspace}"
MAIN_MODEL="${ARGUS_MAIN_MODEL:-claude-sonnet-5-5}"
ME="${USER:-$(id -un)}"
UID_NUM="$(id -u)"

# uv (Python installer) pinned by version and SHA-256; it fetches a checksum-verified Python 3.12.
UV_VERSION="0.13.7"
UV_SHA256="81b811b57e4624df7ed3aac148e0359100d9962f3d9dce00aec533dc2e086731"
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
NO_SERVICE=0
CHANGE_KEYS=0

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'
Options:
  --dry-run        print the steps without changing anything
  --yes            accept the default answer for yes/no questions
  --no-service     configure only; do not start anything in the background
  --change-keys    ask for the Claude key again
  -h, --help       show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
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

ask() {  # ask VAR "Question" "default": uses $VAR if already set (tests and re-runs skip prompts)
  local var="$1" question="$2" default="${3:-}" answer
  eval "answer=\${$var:-}"
  if [ -z "$answer" ]; then
    is_tty || die "$var is not set and there is no terminal to ask. Set $var and run again."
    if [ -n "$default" ]; then read -r -p "  $question [$default]: " answer; else read -r -p "  $question: " answer; fi
    answer="${answer:-$default}"
  fi
  eval "$var=\$answer"
}

ask_secret() {  # like ask, but typing is hidden; empty allowed when $3 = optional
  local var="$1" question="$2" optional="${3:-}" answer
  eval "answer=\${$var:-}"
  if [ -z "$answer" ]; then
    if is_tty; then read -r -s -p "  $question: " answer; echo
    elif [ -z "$optional" ]; then die "$var is not set and there is no terminal to ask. Set $var and run again."; fi
  fi
  eval "$var=\$answer"
}

confirm() {  # confirm "Question" y|n : returns 0 for yes
  local question="$1" default="$2" answer
  if [ "$ASSUME_YES" = 1 ] || ! is_tty; then [ "$default" = y ]; return; fi
  if [ "$default" = y ]; then read -r -p "  $question [Y/n]: " answer; else read -r -p "  $question [y/N]: " answer; fi
  answer="${answer:-$default}"
  case "$answer" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

sha256() { if command -v shasum > /dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi; }
esc() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }
toml_str() { printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"; }

# ---------- secrets go into ~/.argus/secrets.env through stdin, never argv ----------
secret_get() { if [ -f "$SECRETS" ]; then sed -n "s/^$1=//p" "$SECRETS" | tail -1; fi; }
secret_set() {  # usage: printf '%s' "$value" | secret_set KEY
  local key="$1" value tmp
  IFS= read -r value || true
  if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] save %s in %s\n' "$key" "$SECRETS"; return; fi
  mkdir -p "$ARGUS_HOME" && chmod 700 "$ARGUS_HOME"
  touch "$SECRETS" && chmod 600 "$SECRETS"
  tmp="$(mktemp "$ARGUS_HOME/.secrets.XXXXXX")"
  grep -v "^${key}=" "$SECRETS" > "$tmp" || true
  [ -z "$value" ] || printf '%s=%s\n' "$key" "$value" >> "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$SECRETS"
}

# ---------- live checks (keys are fed to curl on stdin, not argv) ----------
anthropic_check() {
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
step "1/9  Checking this Mac"
IS_MAC=0
case "$(uname -s)" in
  Darwin)
    [ "$(uname -m)" = arm64 ] || die "Argus needs an Apple Silicon Mac (this one reports $(uname -m))."
    IS_MAC=1
    macos="$(sw_vers -productVersion 2>/dev/null || echo 0)"
    [ "${macos%%.*}" -ge 13 ] 2>/dev/null || warn "macOS $macos is older than Ventura (13); Argus is tested on 13 and newer."
    xcode-select -p > /dev/null 2>&1 || die "Apple's Command Line Tools are needed: run  xcode-select --install  then ./setup.sh again."
    ok "macOS $macos on Apple Silicon" ;;
  Linux)
    [ "${ARGUS_ALLOW_LINUX:-0}" = 1 ] || die "Argus runs on macOS. (Tests set ARGUS_ALLOW_LINUX=1.)"
    NO_SERVICE=1
    warn "Linux test mode: the Mac app, permissions and login items are skipped" ;;
  *) die "Unsupported system: $(uname -s)" ;;
esac
command -v curl > /dev/null || die "curl is missing"
if [ "${ARGUS_SKIP_VALIDATION:-0}" != 1 ]; then
  if curl -sS -m 10 -o /dev/null https://api.anthropic.com 2>/dev/null; then
    ok "Internet connection works"
  else
    die "Cannot reach api.anthropic.com. Check the internet connection and try again."
  fi
fi
if pgrep -fil openclaw > /dev/null 2>&1; then
  warn "OpenClaw is running. Older versions had serious security holes; consider removing it."
  confirm "Continue anyway?" n || exit 1
fi

# =====================================================================
step "2/9  Your details (keys stay on this Mac, in $SECRETS)"
ANTHROPIC_KEY="${ARGUS_ANTHROPIC_API_KEY:-}"
if [ "$CHANGE_KEYS" = 0 ]; then
  ANTHROPIC_KEY="${ANTHROPIC_KEY:-$(secret_get ANTHROPIC_API_KEY)}"
fi
[ -n "$ANTHROPIC_KEY" ] && ok "Claude API key: found (ending ...$(printf '%s' "$ANTHROPIC_KEY" | tail -c 4))"
ask_secret ANTHROPIC_KEY "Claude API key (from platform.claude.com, starts with sk-ant-)"
case "$ANTHROPIC_KEY" in sk-ant-*) ;; *) die "That doesn't look like a Claude API key (it should start with sk-ant-)." ;; esac

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
  if is_tty && [ ! -f "$CONFIG" ]; then
    echo "  Argus will never open, read or type into websites you list here (your bank, payments, etc.)."
    read -r -p "  Sites to block, comma-separated (e.g. mybank.com, paypal.com) [none]: " SITES
  fi
fi
SITES_TOML="$(printf '%s' "$SITES" | tr ',' '\n' | sed -e 's/^ *//' -e 's/ *$//' -e 's#^https\{0,1\}://##' -e 's#/.*$##' \
  | grep -E '^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$' | tr '[:upper:]' '[:lower:]' | sed 's/.*/"&"/' | paste -sd, - | sed 's/,/, /g' || true)"
[ -n "$SITES_TOML" ] && ok "Never-touch sites: $SITES_TOML"

if [ "${ARGUS_SKIP_VALIDATION:-0}" = 1 ]; then
  warn "Skipping live key checks (ARGUS_SKIP_VALIDATION=1)"
else
  if anthropic_check "$ANTHROPIC_KEY"; then
    ok "Claude API key works"
    printf '%s' "$ANTHROPIC_MODELS" | grep -q "\"$MAIN_MODEL\"" || warn "Model $MAIN_MODEL wasn't in your account's list; set model in $CONFIG"
  else
    die "The Claude API rejected that key (check it, and that your account has credit)."
  fi
fi

# =====================================================================
step "3/9  Python for Argus (hash-checked packages)"
find_uv() {
  if command -v uv > /dev/null 2>&1; then command -v uv; return 0; fi
  for c in "$ARGUS_HOME/bin/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do [ -x "$c" ] && { echo "$c"; return 0; }; done
  return 1
}
if [ "$DRY_RUN" = 1 ]; then
  run curl -fsSL "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz" "(sha256 ${UV_SHA256:0:12}...)"
  run uv venv --python 3.12 --seed "$VENV"
  run "$VENV/bin/python3" -m pip install --require-hashes -r "$REPO_DIR/requirements-macos.lock"
else
  mkdir -p "$ARGUS_HOME"/{bin,models,logs,run}
  chmod 700 "$ARGUS_HOME" "$ARGUS_HOME"/{bin,models,logs,run}
  if [ ! -x "$VENV/bin/python3" ]; then
    if ! UV="$(find_uv)"; then
      [ "$IS_MAC" = 1 ] || die "uv is needed to create the Python environment"
      tmp="$(mktemp -d)"
      curl -fsSL -o "$tmp/uv.tgz" "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz" \
        || die "Couldn't download uv"
      [ "$(sha256 "$tmp/uv.tgz")" = "$UV_SHA256" ] || die "uv download failed its checksum; not using it."
      tar -xzf "$tmp/uv.tgz" -C "$tmp" && mv "$tmp/uv-aarch64-apple-darwin/uv" "$ARGUS_HOME/bin/uv" && rm -rf "$tmp"
      UV="$ARGUS_HOME/bin/uv"
      ok "uv $UV_VERSION installed (checksum verified)"
    fi
    "$UV" venv --python 3.12 --seed -q "$VENV" || die "Couldn't create the Python environment for Argus."
  fi
  if [ "${ARGUS_SKIP_DEPS:-0}" = 1 ]; then
    warn "Skipping Python packages (ARGUS_SKIP_DEPS=1)"
  else
    "$VENV/bin/python3" -m pip install -q --disable-pip-version-check --require-hashes --only-binary :all: \
      -r "$REPO_DIR/requirements-macos.lock" || die "Installing Argus's Python packages failed."
    ok "Python packages installed (every file checked against its pinned hash)"
  fi
  site="$("$VENV/bin/python3" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  printf '%s\n' "$REPO_DIR" > "$site/argus-source.pth"     # Argus runs straight from this folder
fi

# =====================================================================
step "4/9  Speech models (on-device; checksums verified)"
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

# =====================================================================
step "5/9  Saving keys and settings"
printf '%s' "$ANTHROPIC_KEY" | secret_set ANTHROPIC_API_KEY
printf '' | secret_set TELEGRAM_BOT_TOKEN        # Telegram was removed: drop any token an older setup saved
ok "Keys saved to $SECRETS (owner-only)"
PHONE_IP=""
if PHONE_IP="$(find_tailscale_ip)"; then PHONE_ENABLED=true; else PHONE_ENABLED=false; fi
BRIEF="${ARGUS_AI_BRIEFING:-}"
if [ -z "$BRIEF" ] && [ ! -f "$CONFIG" ]; then
  if confirm "Add an AI-written weekday briefing at 7:30 (weather + news; roughly \$1-3 a month)?" y; then BRIEF=yes; else BRIEF=no; fi
fi
BRIEF_CRON=""; [ "$BRIEF" = yes ] && BRIEF_CRON="30 7 * * 1-5"
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
city = $(toml_str "$CITY")
timezone = $(toml_str "$TZ_NAME")

[brain]
model = "$MAIN_MODEL"           # conversations and tasks
small_model = "claude-haiku-5-5" # summarising long conversations
web_search = true               # Claude's web search (\$10 per 1,000 searches)
workspace = $(toml_str "$WORKSPACE")
max_turn_tokens = 400000        # stop a single request before it gets expensive

[listening]
followup_seconds = 8            # after a reply, you can answer without saying "Argus"
conversation_idle_seconds = 120 # conversation mode ends after this much quiet
clap = "double"                 # "double", "single" or "off": clapping wakes Argus
end_of_speech_seconds = 0.6     # how long a pause ends what you're saying (lower = snappier, 0.3 to 2)
voice_lock = false              # true = only answer your voice (record it first: argusctl enroll)
voice_lock_threshold = 0.45
mic_device = ""                 # "" = the Mac's current microphone

[voice]
engine = "piper"                # "piper" = on-device neural voice, "say" = built-in macOS voices
say_voice = "Daniel"
rate = 190
speed = 1.0
greeting = "Online and listening."

[security]
approval_needs_touch_id = true  # "Allow" buttons also need Touch ID or your Mac password
pause_when_locked = true        # microphone closes while the Mac is locked
screen_control = true           # let Argus see and use the screen (each action needs your OK)
blocked_sites = [$SITES_TOML]
# blocked_apps defaults to password managers, Keychain Access and System Settings.
# To change it, uncomment and list every app (names or bundle IDs):
# blocked_apps = ["1Password", "Bitwarden", "Passwords", "Keychain Access", "System Settings"]

[phone]
enabled = $PHONE_ENABLED                 # iPhone shortcuts and the Argus iPhone app, over Tailscale only
port = 8765

[glasses]
enabled = true                  # Meta glasses camera through the Argus iPhone app

[schedule]
morning_headlines = "0 7 * * *" # free headlines, spoken at the Mac (when you're back, if away; "" = off)
morning_briefing = "$BRIEF_CRON"  # AI briefing ("" = off)
EOF
  umask 022
  ok "Settings written to $CONFIG"
fi
run mkdir -p "$WORKSPACE"

# =====================================================================
step "6/9  Building Argus.app"
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
else
  ok "Skipped (not a Mac)"
fi
if [ "$DRY_RUN" = 0 ]; then
  mkdir -p "$BIN_DIR"
  ln -sf "$REPO_DIR/bin/argusctl" "$BIN_DIR/argusctl"
fi
ok "Command-line control: argusctl (try: argusctl status)"

# =====================================================================
step "7/9  Starting Argus at login"
if [ "$NO_SERVICE" = 1 ]; then
  warn "Skipped (--no-service or test mode)."
elif [ "$DRY_RUN" = 1 ]; then
  run launchctl bootstrap "gui/$UID_NUM" "$LAUNCH_AGENTS/com.argus.voice.plist"
else
  mkdir -p "$LAUNCH_AGENTS"
  sed -e "s|@APP@|$(esc "$APP_DIR")|g" -e "s|@LOGS@|$(esc "$ARGUS_HOME/logs")|g" \
    "$REPO_DIR/macos/com.argus.voice.plist" > "$LAUNCH_AGENTS/com.argus.voice.plist"
  launchctl bootout "gui/$UID_NUM/com.argus.voice" > /dev/null 2>&1 || true
  launchctl bootout "gui/$UID_NUM/com.argus.phone" > /dev/null 2>&1 || true   # older versions ran it separately
  rm -f "$LAUNCH_AGENTS/com.argus.phone.plist"
  launchctl bootstrap "gui/$UID_NUM" "$LAUNCH_AGENTS/com.argus.voice.plist" || die "Couldn't start Argus"
  ok "Argus is running (menu bar ◉) and starts when you log in"
  if [ "$PHONE_ENABLED" = true ]; then
    ok "iPhone gateway on Tailscale: http://$PHONE_IP:8765"
  else
    warn "Tailscale isn't connected, so the iPhone connection is off for now (see docs/IPHONE.md)."
  fi
  want_awake="${ARGUS_STAY_AWAKE:-}"
  if [ -z "$want_awake" ]; then
    if confirm "Keep this Mac awake while it's plugged in, so Argus can always answer?" y; then want_awake=yes; else want_awake=no; fi
  fi
  [ "$want_awake" = yes ] && run "$REPO_DIR/scripts/stay-awake.sh" on
fi

# =====================================================================
step "8/9  Permissions (macOS asks once; click Allow)"
if [ "$IS_MAC" = 1 ] && [ "$NO_SERVICE" = 0 ] && [ "$DRY_RUN" = 0 ]; then
  echo "  • Microphone: \"Argus would like to access the microphone\" → Allow."
  echo "    Speech is processed on this Mac; only what you say to Argus is sent to Claude."
  echo "  • Screen Recording and Accessibility: lets Argus see and use apps when you ask. macOS opens"
  echo "    System Settings; switch Argus on in both lists, then run: argusctl restart"
  echo "  • Approvals: when Argus wants to click, type or delete, a box appears; Allow asks for Touch ID."
else
  ok "Skipped here"
fi

# =====================================================================
step "9/9  Saying hello"
if [ "$DRY_RUN" = 1 ] || [ "$NO_SERVICE" = 1 ] || [ "${ARGUS_SEND_HELLO:-yes}" = no ]; then
  ok "Skipped"
else
  for _ in $(seq 1 30); do "$BIN_DIR/argusctl" status > /dev/null 2>&1 && break; sleep 1; done
  if "$BIN_DIR/argusctl" say "Good day, $NAME. I'm ready. Say my name, or clap twice." > /dev/null 2>&1; then
    ok "Argus said hello out loud"
  else
    warn "Argus isn't answering yet. Give it a minute, or run: argusctl restart"
  fi
fi

cat <<EOF

${B}Done.${N} Say "Argus, what's the news today?" near your Mac (or clap twice, then ask).
  - Menu bar ◉: microphone, conversation mode, screen access, stop, activity log
  - Teach it your voice (recommended): argusctl enroll
  - iPhone and Meta glasses: docs/IPHONE.md$( [ "$PHONE_ENABLED" = true ] && printf ' (address http://%s:8765, token: argusctl phone-token)' "$PHONE_IP" )
  - Spend so far: argusctl usage    Health check: ./scripts/doctor.sh
EOF
