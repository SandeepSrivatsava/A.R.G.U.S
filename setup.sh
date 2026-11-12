#!/bin/bash
# Argus setup: installs Argus (our own voice assistant: brain, ears, voice, phone gateway) on your
# Mac and starts it at login. It asks first which phone you use (iPhone or Android) and sets
# everything up for that phone. Safe to run again; it keeps what already works.
#
#   ./setup.sh                   normal setup (asks a few questions)
#   ./setup.sh --phone android   skip the phone question
#   ./setup.sh --dry-run         show what would happen, change nothing
#
# Run in Termux on an Android phone, this same command installs Argus on the phone instead
# (scripts/setup_android.sh). For an always-on Linux server: scripts/setup_server.sh.
#
# Keys go only into ~/.argus/secrets.env (readable by you alone). They never go into this repo,
# and they are never passed on a command line where other programs could see them.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${TERMUX_VERSION:-}" ] || [[ "${PREFIX:-}" == *com.termux* ]]; then
  exec bash "$REPO_DIR/scripts/setup_android.sh" "$@"      # we're on an Android phone, in Termux
fi
# shellcheck source=scripts/lib.sh
. "$REPO_DIR/scripts/lib.sh"
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
PHONE_FLAG=""

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'
Options:
  --phone iphone|android   which phone you use (otherwise setup asks)
  --dry-run                print the steps without changing anything
  --yes                    accept the default answer for yes/no questions
  --no-service             configure only; do not start anything in the background
  --change-keys            ask for the Claude key (and the maps keys) again
  -h, --help               show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --no-service) NO_SERVICE=1 ;;
    --change-keys) CHANGE_KEYS=1 ;;
    --phone) [ $# -ge 2 ] || { echo "--phone needs iphone or android" >&2; exit 2; }; PHONE_FLAG="$2"; shift ;;
    --phone=*) PHONE_FLAG="${1#--phone=}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

# Helpers (output, questions, the secrets file): scripts/lib.sh, sourced above.

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

# Which computer this is: setup never asks, it looks. Argus itself runs on this computer (a Mac); the phone
# is how you reach it when you're away. ARGUS_TEST_SYSTEM stands in for uname in the tests.
SYSTEM="${ARGUS_TEST_SYSTEM:-$(uname -s)}"
case "$SYSTEM" in
  Darwin)
    model="$(sysctl -n hw.model 2>/dev/null || true)"
    case "$model" in MacBookAir*) model="MacBook Air" ;; MacBookPro*) model="MacBook Pro" ;; Macmini*) model="Mac mini" ;;
      iMac*) model="iMac" ;; Mac1*|Mac2*) model="Mac" ;; *) model="Mac" ;; esac
    echo
    echo "  This computer: a $model (macOS). Argus will run on it: listening, speaking and using the Mac."
    echo "  Nothing to choose for the computer. The next question is only about your phone."
    ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT)
    die "This is Windows. Argus runs on a Mac: it needs macOS for listening all the time, Touch ID approvals,
  using apps on the screen and Apple Maps, and there is no Windows version yet. What works today:
    * a Mac (an Apple Silicon one, like a MacBook Air): run ./setup.sh there;
    * an Android phone on its own: Argus runs on the phone (docs/ANDROID.md);
    * an always-on Linux server for an iPhone or Android phone (docs/SERVER.md).
  A Windows version is on the roadmap (docs/ROADMAP.md)." ;;
  Linux)
    if [ "${ARGUS_ALLOW_LINUX:-0}" != 1 ]; then
      if grep -qi microsoft /proc/version 2>/dev/null; then
        die "This is Windows (WSL). Argus runs on a Mac; there is no Windows version yet (see docs/ROADMAP.md).
  Without a Mac: Argus can run on an Android phone (docs/ANDROID.md) or on an always-on Linux server
  (docs/SERVER.md)."
      fi
      die "This is a Linux computer. ./setup.sh is for a Mac. On an always-on Linux server, run:
  sudo scripts/setup_server.sh   (see docs/SERVER.md)"
    fi ;;
  *) die "Unsupported system: $SYSTEM. Argus runs on a Mac (and on Android phones and Linux servers with their own setup)." ;;
esac

# ---- running setup again: show the saved answers, and offer to keep them --------------------------------------
ANSWERS="$ARGUS_HOME/setup-answers"   # which questions were answered, including keys you chose to skip
KEEP=0                                # 1 = keep every saved answer and ask only questions not answered yet
saved_line() { printf '    %-18s %s\n' "$1" "$2"; }
saved_key() {  # saved_key SECRET_NAME ANSWER_ID
  local value; value="$(secret_get "$1")"
  if [ -n "$value" ]; then printf 'saved (ending ...%s)' "$(printf '%s' "$value" | tail -c 4)"
  elif [ "$(answer_get "$2")" = skipped ]; then printf 'skipped'
  else printf 'not asked yet (new question)'; fi
}
show_saved_answers() {
  local apps="" word sites brief awake
  for word in $(toml_get "$CONFIG" maps app) $(toml_get "$CONFIG" maps apps | tr -d '[],'); do
    apps="${apps:+$apps, }$(app_name "$word")"
  done
  sites="$(toml_get "$CONFIG" security blocked_sites | tr -d '[]')"
  brief="$(toml_get "$CONFIG" schedule morning_briefing)"
  awake="$(answer_get stay_awake)"
  echo
  echo "  You've set up Argus on this Mac before. Your saved answers:"
  saved_line "Phone:" "$(if [ "$(toml_get "$CONFIG" phone type)" = android ]; then echo Android; else echo iPhone; fi)"
  saved_line "Maps apps:" "${apps:-Apple Maps}"
  saved_line "Claude API key:" "$(saved_key ANTHROPIC_API_KEY claude_key)"
  saved_line "Your name:" "$(toml_get "$CONFIG" "" user_name)"
  saved_line "Home city:" "$(toml_get "$CONFIG" "" city)"
  saved_line "Blocked sites:" "${sites:-none}"
  saved_line "Google Maps key:" "$(saved_key GOOGLE_MAPS_API_KEY google_key)"
  saved_line "TomTom key:" "$(saved_key TOMTOM_API_KEY tomtom_key)"
  saved_line "Weekday briefing:" "$(if [ -n "$brief" ]; then echo on; else echo off; fi)"
  saved_line "Keep Mac awake:" "${awake:-not asked yet (new question)}"
}
if [ -f "$CONFIG" ] && [ "$CHANGE_KEYS" = 0 ]; then
  KEEP=1
  show_saved_answers
  if is_tty && [ "$ASSUME_YES" != 1 ]; then
    if ! confirm "Keep these answers and only ask what's new?" y; then
      KEEP=0
      echo "  Each question now shows your saved answer: press Enter to keep it, or type a new one."
    fi
  fi
fi

# =====================================================================
step "1/10  Your phone (Argus runs on this Mac; the phone is how you reach it when away)"
OLD_PHONE=""
[ -f "$CONFIG" ] && OLD_PHONE="$(toml_get "$CONFIG" phone type)"
GIVEN="${PHONE_FLAG:-${ARGUS_PHONE:-}}"
PHONE="$(normalize_phone "$GIVEN")"
[ -z "$GIVEN" ] || [ -n "$PHONE" ] || die "--phone must be iphone or android"
if [ -z "$PHONE" ] && [ "$KEEP" = 1 ] && [ -n "$OLD_PHONE" ]; then
  PHONE="$OLD_PHONE"; PHONE_NOTE=" (saved)"        # kept from last time
fi
if [ -z "$PHONE" ]; then
  if is_tty; then
    echo "  Which phone do you carry? (Not the computer: this Mac is already set.)"
    echo "    1) iPhone   Siri and Shortcuts, Apple Maps, calls from your own number through this Mac"
    echo "    2) Android  Argus also runs on the phone itself: texts, WhatsApp, notifications, Google Maps"
    default_choice=""
    [ "$OLD_PHONE" = iphone ] && default_choice=1
    [ "$OLD_PHONE" = android ] && default_choice=2
    while [ -z "$PHONE" ]; do
      read -r -p "  Type 1 or 2${default_choice:+ [$default_choice]}: " answer
      PHONE="$(normalize_phone "${answer:-$default_choice}")"
      [ -n "$PHONE" ] || echo "  Please type 1 for iPhone or 2 for Android."
    done
  else
    PHONE="${OLD_PHONE:-iphone}"
    warn "No terminal to ask on: setting up for $PHONE (choose with --phone iphone|android)"
  fi
fi
PHONE_DOC=docs/IPHONE.md
if [ "$PHONE" = android ]; then
  PHONE_DOC=docs/ANDROID.md
  MAPS_APP=google; GLASSES=false
  ok "Android: Google Maps links, and Argus on the phone itself (texts, WhatsApp, notifications)${PHONE_NOTE:-}"
else
  MAPS_APP=apple; GLASSES=true
  ok "iPhone: Apple Maps, Siri Shortcuts, Meta glasses, calls from your number (optional)${PHONE_NOTE:-}"
fi
# Which navigation apps are on the phone (Apple Maps, Google Maps, Waze): asked once, then kept in the settings.
OLD_APPS=""; OLD_APP=""; SAVED_APPS=""
if [ -f "$CONFIG" ] && [ "$OLD_PHONE" = "$PHONE" ]; then
  OLD_APP="$(toml_get "$CONFIG" maps app)"
  if grep -q '^apps[[:space:]]*=' "$CONFIG"; then OLD_APPS="$OLD_APP $(toml_get "$CONFIG" maps apps)"; fi
  SAVED_APPS="$(printf '%s' "$OLD_APPS" | tr -d '[],')"
  [ "$KEEP" = 1 ] || OLD_APPS=""    # reviewing: ask again, with the saved answers as the defaults
fi
choose_nav_apps "$PHONE" "${ARGUS_MAPS_APPS:-$OLD_APPS}" "$OLD_APP" "$SAVED_APPS"
ok "Directions open in $(app_name "$MAPS_APP")${MAPS_APPS_TOML:+ (also offered: $(printf '%s' "$MAPS_APPS_TOML" | tr -d '"'))}"

# =====================================================================
step "2/10  Checking this Mac"
IS_MAC=0
case "$SYSTEM" in
  Darwin)
    [ "$(uname -m)" = arm64 ] || die "Argus needs an Apple Silicon Mac (this one reports $(uname -m))."
    IS_MAC=1
    macos="$(sw_vers -productVersion 2>/dev/null || echo 0)"
    [ "${macos%%.*}" -ge 13 ] 2>/dev/null || warn "macOS $macos is older than Ventura (13); Argus is tested on 13 and newer."
    xcode-select -p > /dev/null 2>&1 || die "Apple's Command Line Tools are needed: run  xcode-select --install  then ./setup.sh again."
    ok "macOS $macos on Apple Silicon" ;;
  Linux)
    [ "${ARGUS_ALLOW_LINUX:-0}" = 1 ] || die "This sets Argus up on a Mac. On an always-on Linux server, run: scripts/setup_server.sh"
    NO_SERVICE=1
    warn "Linux test mode: the Mac app, permissions and login items are skipped" ;;
  *) die "Unsupported system: $SYSTEM" ;;
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
step "3/10  Your details (keys stay on this Mac, in $SECRETS)"
ANTHROPIC_KEY="${ARGUS_ANTHROPIC_API_KEY:-}"
if [ "$CHANGE_KEYS" = 0 ]; then
  ANTHROPIC_KEY="${ANTHROPIC_KEY:-$(secret_get ANTHROPIC_API_KEY)}"
fi
[ -n "$ANTHROPIC_KEY" ] && ok "Claude API key: found (ending ...$(printf '%s' "$ANTHROPIC_KEY" | tail -c 4))"
if [ -n "$ANTHROPIC_KEY" ] && [ "$KEEP" = 0 ] && [ -f "$CONFIG" ] && [ -z "${ARGUS_ANTHROPIC_API_KEY:-}" ] && is_tty; then
  read -r -s -p "  New Claude API key (Enter keeps the saved one): " new_key; echo
  [ -z "$new_key" ] || ANTHROPIC_KEY="$new_key"
fi
ask_secret ANTHROPIC_KEY "Claude API key (from platform.claude.com, starts with sk-ant-)"
case "$ANTHROPIC_KEY" in sk-ant-*) ;; *) die "That doesn't look like a Claude API key (it should start with sk-ant-)." ;; esac

default_name="$( { id -F 2>/dev/null || getent passwd "$ME" 2>/dev/null | cut -d: -f5 | cut -d, -f1; } | awk '{print $1}' || true)"
SAVED_NAME=""; SAVED_CITY=""
if [ -f "$CONFIG" ]; then SAVED_NAME="$(toml_get "$CONFIG" "" user_name)"; SAVED_CITY="$(toml_get "$CONFIG" "" city)"; fi
NAME="${ARGUS_NAME:-}"
if [ -z "$NAME" ] && [ "$KEEP" = 1 ] && [ -n "$SAVED_NAME" ]; then NAME="$SAVED_NAME"; ok "Your name: $NAME (saved)"; fi
ask NAME "What should Argus call you?" "${SAVED_NAME:-${default_name:-$ME}}"
CITY="${ARGUS_CITY:-}"
if [ -z "$CITY" ] && [ "$KEEP" = 1 ] && [ -n "$SAVED_CITY" ]; then CITY="$SAVED_CITY"; ok "Home city: $CITY (saved)"; fi
ask CITY "Your home city, used when your phone has not shared where you are (e.g. Portland, OR)" "$SAVED_CITY"
TZ_NAME="${ARGUS_TIMEZONE:-$(readlink /etc/localtime 2>/dev/null | sed 's#.*zoneinfo/##' || true)}"
[ -n "$TZ_NAME" ] || TZ_NAME="UTC"
ok "Time zone: $TZ_NAME"

SITES="${ARGUS_BLOCKED_SITES-unset}"
SITES_CHANGED=0
if [ "$SITES" = unset ]; then
  SITES=""
  if is_tty && [ ! -f "$CONFIG" ]; then
    echo "  Argus will never open, read or type into websites you list here (your bank, payments, etc.)."
    read -r -p "  Sites to block, comma-separated (e.g. mybank.com, paypal.com) [none]: " SITES
  elif is_tty && [ "$KEEP" = 0 ]; then
    saved_sites="$(toml_get "$CONFIG" security blocked_sites | tr -d '[]')"
    echo "  Argus never opens, reads or types into these websites (type none to clear the list)."
    read -r -p "  Sites to block, comma-separated [${saved_sites:-none}]: " SITES
    SITES="${SITES:-$saved_sites}"
    [ "$(printf '%s' "$SITES" | tr '[:upper:]' '[:lower:]')" != none ] || SITES=""
    SITES_CHANGED=1
  fi
fi
SITES_TOML="$(printf '%s' "$SITES" | tr ',' '\n' | sed -e 's/^ *//' -e 's/ *$//' -e 's#^https\{0,1\}://##' -e 's#/.*$##' \
  | grep -E '^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$' | tr '[:upper:]' '[:lower:]' | sed 's/.*/"&"/' | paste -sd, - | sed 's/,/, /g' || true)"
[ -n "$SITES_TOML" ] && ok "Never-touch sites: $SITES_TOML"

# Live traffic for directions: Apple Maps on this Mac is free (no key). A Google Maps key adds Google's traffic,
# and is what an Android phone uses. Optional either way; the maps app always shows traffic itself.
GOOGLE_KEY="${ARGUS_GOOGLE_MAPS_KEY:-}"
GOOGLE_ASKED=0
optional_key GOOGLE_KEY GOOGLE_MAPS_API_KEY google_key "Google Maps key"
if [ "$KEY_ASK" = 1 ]; then
  GOOGLE_ASKED=1
  if [ "$PHONE" = android ]; then
    echo "  Live traffic on Android comes from Google Maps: a free API key from console.cloud.google.com"
    echo "  (switch on the Routes API). Press Enter to skip; directions then open Google Maps without times."
  else
    echo "  Live traffic comes free from Apple Maps on this Mac. A Google Maps API key is optional"
    echo "  (the always-on server can use it). Press Enter to skip."
  fi
  ask_secret GOOGLE_KEY "Google Maps API key (starts with AIza; Enter to skip)" optional
fi
if [ -n "$GOOGLE_KEY" ] && ! printf '%s' "$GOOGLE_KEY" | grep -Eq '^AIza[0-9A-Za-z_-]{35}$'; then
  warn "That doesn't look like a Google Maps API key (they start with AIza and are 39 characters); saving it anyway."
fi
ask_tomtom_key

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
step "4/10  Python for Argus (hash-checked packages)"
find_uv() {
  if command -v uv > /dev/null 2>&1; then command -v uv; return 0; fi
  for c in "$ARGUS_HOME/bin/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do [ -x "$c" ] && { echo "$c"; return 0; }; done
  return 1
}
if [ "$DRY_RUN" = 1 ]; then
  run curl -fsSL "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-aarch64-apple-darwin.tar.gz" "(sha256 ${UV_SHA256:0:12}...)"
  run uv venv --python 3.12 --seed "$VENV"
  run "$VENV/bin/python3" -m pip install --require-hashes --only-binary :all: -r "$REPO_DIR/requirements-build.lock"
  run "$VENV/bin/python3" -m pip install --require-hashes --only-binary :all: --no-binary rumps --no-build-isolation \
    -r "$REPO_DIR/requirements-macos.lock"
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
    # Every package is a ready-built wheel except rumps (the menu bar), which PyPI has only as source. It's
    # built here with a hash-pinned setuptools installed first, so the build downloads nothing unchecked.
    "$VENV/bin/python3" -m pip install -q --disable-pip-version-check --require-hashes --only-binary :all: \
      -r "$REPO_DIR/requirements-build.lock" || die "Installing the build tool (setuptools) failed."
    "$VENV/bin/python3" -m pip install -q --disable-pip-version-check --require-hashes --only-binary :all: \
      --no-binary rumps --no-build-isolation -r "$REPO_DIR/requirements-macos.lock" \
      || die "Installing Argus's Python packages failed."
    ok "Python packages installed (every file checked against its pinned hash)"
  fi
  site="$("$VENV/bin/python3" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  printf '%s\n' "$REPO_DIR" > "$site/argus-source.pth"     # Argus runs straight from this folder
fi

# =====================================================================
step "5/10  Speech models (on-device; checksums verified)"
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
if [ "${ARGUS_SKIP_MODELS:-0}" = 1 ]; then
  warn "Skipping speech models (ARGUS_SKIP_MODELS=1)"
else
  while read -r path sum dest; do
    [ -n "$path" ] || continue
    fetch_model "$path" "$sum" "$dest"
  done <<< "$MODELS"
fi

# =====================================================================
step "6/10  Saving keys and settings"
printf '%s' "$ANTHROPIC_KEY" | secret_set ANTHROPIC_API_KEY
[ -z "$GOOGLE_KEY" ] || printf '%s' "$GOOGLE_KEY" | secret_set GOOGLE_MAPS_API_KEY
[ -z "$TOMTOM_KEY" ] || printf '%s' "$TOMTOM_KEY" | secret_set TOMTOM_API_KEY
answer_set claude_key saved
if [ -n "$GOOGLE_KEY" ]; then answer_set google_key saved; elif [ "$GOOGLE_ASKED" = 1 ]; then answer_set google_key skipped; fi
if [ -n "$TOMTOM_KEY" ]; then answer_set tomtom_key saved; elif [ "$TOMTOM_ASKED" = 1 ]; then answer_set tomtom_key skipped; fi
printf '' | secret_set TELEGRAM_BOT_TOKEN        # Telegram was removed: drop any token an older setup saved
ok "Keys saved to $SECRETS (owner-only)"
PHONE_IP=""
if PHONE_IP="$(find_tailscale_ip)"; then PHONE_ENABLED=true; else PHONE_ENABLED=false; fi
BRIEF="${ARGUS_AI_BRIEFING:-}"
BRIEF_ASKED=0
if [ -z "$BRIEF" ] && { [ ! -f "$CONFIG" ] || [ "$KEEP" = 0 ]; }; then
  brief_default=y
  if [ -f "$CONFIG" ] && [ -z "$(toml_get "$CONFIG" schedule morning_briefing)" ]; then brief_default=n; fi
  if confirm "Add an AI-written weekday briefing at 7:30 (weather + news; roughly \$1-3 a month)?" "$brief_default"; then
    BRIEF=yes; else BRIEF=no; fi
  BRIEF_ASKED=1
fi
BRIEF_CRON=""; [ "$BRIEF" = yes ] && BRIEF_CRON="30 7 * * 1-5"
if [ "$DRY_RUN" = 1 ]; then
  printf '  [dry-run] write %s\n' "$CONFIG"
elif [ -f "$CONFIG" ] && [ "${ARGUS_REWRITE_CONFIG:-0}" != 1 ]; then
  ok "Kept your settings in $CONFIG"
  if [ "$SITES_CHANGED" = 1 ]; then
    toml_set "$CONFIG" security blocked_sites "[$SITES_TOML]"
  elif [ -n "$SITES_TOML" ]; then
    warn "To change blocked sites, edit blocked_sites in $CONFIG, then: argusctl restart"
  fi
  [ "$BRIEF_ASKED" = 0 ] || toml_set "$CONFIG" schedule morning_briefing "\"$BRIEF_CRON\""
  if [ "$KEEP" = 0 ] && [ -n "$SAVED_NAME$SAVED_CITY" ]; then
    toml_set "$CONFIG" "" user_name "$(toml_str "$NAME")"
    toml_set "$CONFIG" "" city "$(toml_str "$CITY")"
  fi
  if [ "$OLD_PHONE" != "$PHONE" ]; then            # a different phone: switch the phone-specific settings
    toml_set "$CONFIG" phone type "\"$PHONE\""
    toml_set "$CONFIG" glasses enabled "$GLASSES"
    [ "$PHONE" = iphone ] || toml_set "$CONFIG" calls enabled false
    ok "Switched the phone settings to $PHONE (maps: $MAPS_APP)"
  fi
  toml_set "$CONFIG" maps app "\"$MAPS_APP\""
  toml_set "$CONFIG" maps apps "[$MAPS_APPS_TOML]"
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
self_learning = true            # after a request that hit errors, Argus writes itself a lesson (argusctl lessons)

[listening]
followup_seconds = 8            # after a reply, you can answer without saying "Argus"
conversation_idle_seconds = 120 # conversation mode ends after this much quiet
clap = "double"                 # "double", "single" or "off": clapping wakes Argus
end_of_speech_seconds = 0.6     # how long a pause ends what you're saying (lower = snappier, 0.3 to 2)
voice_lock = true               # only answer your voice, once you've run: argusctl enroll
voice_lock_threshold = 0.45
guests = "ask"                  # someone else talks to Argus: "ask" you first, or "never" answer them
guest_minutes = 60              # how long "Argus, talk to everyone" lasts
mic_device = ""                 # "" = the Mac's current microphone

[voice]
engine = "piper"                # "piper" = on-device neural voice, "say" = built-in macOS voices
say_voice = "Daniel"
rate = 190
speed = 1.0
greeting = "Online and listening."

[security]
approval_needs_touch_id = true  # "Allow" buttons also need Touch ID or your Mac password
voice_approvals = true          # your voice + the code word Argus reads out can approve (never paying)
voice_approval_threshold = 0.55 # how closely it must match your voiceprint (stricter than voice lock)
pause_when_locked = true        # microphone closes while the Mac is locked
screen_control = true           # let Argus see and use the screen (each action needs your OK)
blocked_sites = [$SITES_TOML]
# blocked_apps defaults to password managers, Keychain Access and System Settings.
# To change it, uncomment and list every app (names or bundle IDs):
# blocked_apps = ["1Password", "Bitwarden", "Passwords", "Keychain Access", "System Settings"]

[phone]
type = "$PHONE"                 # "iphone" or "android" (run ./setup.sh again to change)
enabled = $PHONE_ENABLED                 # the phone's shortcut (and the Argus iPhone app), over Tailscale only
port = 8765

[maps]
app = "$MAPS_APP"                 # the navigation app that opens: "apple", "google" or "waze"
apps = [$MAPS_APPS_TOML]                       # your other navigation apps, offered too: "apple", "google", "waze"
traffic = "auto"                # compares every traffic service that is set up: Apple Maps on this Mac (free),
                                # Google and TomTom (with keys). Or just one: "mapkit", "apple", "google", "tomtom"; "off"
news = true                     # look for the last day's news about the roads on a route
units = "imperial"              # or "metric"

[push]
enabled = false                 # reminders on your phone while you're away from the Mac (docs/SERVER.md)

[glasses]
enabled = $GLASSES                  # Meta glasses camera through the Argus iPhone app

[calls]
enabled = false                 # calls from your number (iPhone only): run scripts/setup_calls.sh to switch on
max_minutes = 6
per_hour = 6
monitor = true                  # hear both sides on the Mac's speakers
international = false           # only US and Canadian numbers

[schedule]
morning_headlines = "0 7 * * *" # free headlines, spoken at the Mac (when you're back, if away; "" = off)
morning_briefing = "$BRIEF_CRON"  # AI briefing ("" = off)
EOF
  umask 022
  ok "Settings written to $CONFIG"
fi
run mkdir -p "$WORKSPACE"

# =====================================================================
step "7/10  Building Argus.app"
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
  # Free live traffic from Apple Maps: a small MapKit helper (macos/argus-maps.swift), rebuilt when it changes.
  maps_sum="$(shasum -a 256 "$REPO_DIR/macos/argus-maps.swift" | cut -d' ' -f1)"
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] build %s\n' "$ARGUS_HOME/bin/argus-maps"
  elif [ -x "$ARGUS_HOME/bin/argus-maps" ] && [ "$(cat "$ARGUS_HOME/bin/argus-maps.sha256" 2>/dev/null)" = "$maps_sum" ]; then
    ok "Apple Maps helper is up to date"
  elif swiftc -O -swift-version 5 -o "$ARGUS_HOME/bin/argus-maps" "$REPO_DIR/macos/argus-maps.swift" > /dev/null 2>&1; then
    printf '%s\n' "$maps_sum" > "$ARGUS_HOME/bin/argus-maps.sha256"
    ok "Built the Apple Maps helper (free live traffic for directions)"
  else
    warn "Couldn't build the Apple Maps helper; directions open the maps app without times unless there's a Google key"
  fi
  # Noticing calls at the Mac (FaceTime, Zoom...), so Argus keeps quiet during them: macos/argus-mic.swift.
  mic_sum="$(shasum -a 256 "$REPO_DIR/macos/argus-mic.swift" | cut -d' ' -f1)"
  if [ "$DRY_RUN" = 1 ]; then
    printf '  [dry-run] build %s\n' "$ARGUS_HOME/bin/argus-mic"
  elif [ -x "$ARGUS_HOME/bin/argus-mic" ] && [ "$(cat "$ARGUS_HOME/bin/argus-mic.sha256" 2>/dev/null)" = "$mic_sum" ]; then
    ok "Call helper is up to date"
  elif swiftc -O -swift-version 5 -o "$ARGUS_HOME/bin/argus-mic" "$REPO_DIR/macos/argus-mic.swift" > /dev/null 2>&1; then
    printf '%s\n' "$mic_sum" > "$ARGUS_HOME/bin/argus-mic.sha256"
    ok "Built the call helper (Argus keeps quiet while you're on a call at the Mac)"
  else
    warn "Couldn't build the call helper; say \"Argus, I'm on a call\" to pause it during calls"
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
step "8/10  Starting Argus at login"
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
    ok "Phone gateway on Tailscale: http://$PHONE_IP:8765"
  else
    warn "Tailscale isn't connected, so the phone connection is off for now (see $PHONE_DOC)."
  fi
  want_awake="${ARGUS_STAY_AWAKE:-}"
  if [ -z "$want_awake" ] && [ "$KEEP" = 1 ]; then
    want_awake="$(answer_get stay_awake)"
    [ -z "$want_awake" ] || ok "Keep this Mac awake: $want_awake (saved)"
  fi
  if [ -z "$want_awake" ]; then
    awake_default=y; [ "$(answer_get stay_awake)" != no ] || awake_default=n
    if confirm "Keep this Mac awake while it's plugged in, so Argus can always answer?" "$awake_default"; then
      want_awake=yes; else want_awake=no; fi
  fi
  answer_set stay_awake "$want_awake"
  [ "$want_awake" = yes ] && run "$REPO_DIR/scripts/stay-awake.sh" on
fi

# =====================================================================
step "9/10  Permissions (macOS asks once; click Allow)"
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
step "10/10  Saying hello"
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

GATEWAY_NOTE=""
if [ "$PHONE_ENABLED" = true ]; then GATEWAY_NOTE=" (address http://$PHONE_IP:8765, token: argusctl phone-token)"; fi
cat <<EOF

${B}Done.${N} Say "Argus, what's the news today?" near your Mac (or clap twice, then ask).
  - Menu bar ◉: microphone, conversation mode, screen access, stop, activity log
  - Teach it your voice (recommended): argusctl enroll
EOF
if [ "$PHONE" = android ]; then
  cat <<EOF
  - ${B}Next, the Android phone${N} (docs/ANDROID.md): install Termux and Termux:API from F-Droid, then in Termux:
        pkg install git gh && gh auth login && gh repo clone SandeepSrivatsava/A.R.G.U.S argus && cd argus && ./setup.sh
    That puts Argus on the phone itself: texts, WhatsApp, notifications, live traffic, push-to-talk.
  - Talk to this Mac from the phone too (HTTP Shortcuts): docs/ANDROID.md$GATEWAY_NOTE
  - Calls from your own number need an iPhone; on Android, Argus opens the dialer for you.
EOF
else
  cat <<EOF
  - iPhone, Siri, directions and Meta glasses: docs/IPHONE.md$GATEWAY_NOTE
  - Calls from your own number (optional): scripts/setup_calls.sh
  - When the Mac is off or far away: an always-on server, docs/SERVER.md
EOF
fi
echo "  - Spend so far: argusctl usage    Health check: ./scripts/doctor.sh"
