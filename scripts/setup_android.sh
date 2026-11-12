#!/usr/bin/env bash
# Argus on an Android phone. Run it in Termux (./setup.sh in Termux runs this): it installs Argus on the
# phone itself, so it works without the Mac: your texts, WhatsApp and notifications, live traffic and
# drive assist, reminders, push-to-talk from the home screen. Safe to run again.
#
# Before: install from F-Droid (or their GitHub releases) the apps Termux, Termux:API, Termux:Widget
# and Termux:Boot, and open each one once. Then in Termux:  ./setup.sh
#
# Keys go only into ~/.argus/secrets.env, private to Termux. Nothing here runs with root.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib.sh
. "$REPO_DIR/scripts/lib.sh"

ARGUS_HOME="${ARGUS_HOME:-$HOME/.argus}"
SECRETS="$ARGUS_HOME/secrets.env"
CONFIG="$ARGUS_HOME/config.toml"
WORKSPACE="${ARGUS_WORKSPACE:-$HOME/argus-workspace}"
TERMUX_PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
BIN_DIR="${ARGUS_BIN_DIR:-$TERMUX_PREFIX/bin}"
SHORTCUTS="${ARGUS_SHORTCUTS_DIR:-$HOME/.shortcuts}"
BOOT_DIR="${ARGUS_BOOT_DIR:-$HOME/.termux/boot}"
PYTHON="${ARGUS_PYTHON:-python3}"
PKG="${ARGUS_PKG:-pkg}"
MAIN_MODEL="${ARGUS_MAIN_MODEL:-claude-sonnet-5-5}"
SH="$TERMUX_PREFIX/bin/sh"

DRY_RUN=0; ASSUME_YES=0; NO_SERVICE=0; CHANGE_KEYS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --no-service) NO_SERVICE=1 ;;
    --change-keys) CHANGE_KEYS=1 ;;
    --phone|--phone=*) [ "$1" = --phone ] && shift ;;      # this is the Android setup either way
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

anthropic_check() {  # the key is fed to curl on stdin, never on its command line
  local code
  code="$(printf 'header = "x-api-key: %s"\n' "$1" | curl -sS -m 20 -K - -o /dev/null -w '%{http_code}' \
          -H 'anthropic-version: 2023-06-01' 'https://api.anthropic.com/v1/models?limit=1' 2>/dev/null)" || code=000
  [ "$code" = 200 ]
}
quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }   # for the small launcher scripts

# =====================================================================
step "1/7  Checking this phone"
if [ -z "${TERMUX_VERSION:-}" ] && [[ "$TERMUX_PREFIX" != *com.termux* ]] && [ "${ARGUS_ALLOW_NON_TERMUX:-0}" != 1 ]; then
  die "This sets Argus up on an Android phone, inside the Termux app. On a Mac run ./setup.sh; on a server scripts/setup_server.sh."
fi
android="$(getprop ro.build.version.release 2>/dev/null || true)"
ok "Termux${TERMUX_VERSION:+ $TERMUX_VERSION}${android:+ on Android $android}"

# =====================================================================
step "2/7  Termux packages (from Termux's signed repository)"
if [ "${ARGUS_SKIP_PKG:-0}" = 1 ]; then
  warn "Skipping packages (ARGUS_SKIP_PKG=1)"
else
  run "$PKG" install -y python termux-api git
  ok "Python, Termux:API commands, git"
fi
command -v termux-sms-list > /dev/null 2>&1 || warn "The termux-api commands are missing: pkg install termux-api"
"$PYTHON" -c 'import sys, sqlite3, tomllib; sys.exit(sys.version_info < (3, 11))' 2> /dev/null \
  || die "Argus needs Python 3.11 or newer with sqlite3: pkg install python"

# =====================================================================
step "3/7  Your details (keys stay on this phone, in $SECRETS)"
ANTHROPIC_KEY="${ARGUS_ANTHROPIC_API_KEY:-}"
[ "$CHANGE_KEYS" = 1 ] || ANTHROPIC_KEY="${ANTHROPIC_KEY:-$(secret_get ANTHROPIC_API_KEY)}"
[ -n "$ANTHROPIC_KEY" ] && ok "Claude API key: found (ending ...$(printf '%s' "$ANTHROPIC_KEY" | tail -c 4))"
ask_secret ANTHROPIC_KEY "Claude API key (from platform.claude.com, starts with sk-ant-)"
case "$ANTHROPIC_KEY" in sk-ant-*) ;; *) die "That doesn't look like a Claude API key (it should start with sk-ant-)." ;; esac
if [ "${ARGUS_SKIP_VALIDATION:-0}" = 1 ]; then
  warn "Skipping the live key check (ARGUS_SKIP_VALIDATION=1)"
elif anthropic_check "$ANTHROPIC_KEY"; then
  ok "Claude API key works"
else
  die "The Claude API rejected that key, or there's no internet (check the key and that the account has credit)."
fi
NAME="${ARGUS_NAME:-}"
ask NAME "What should Argus call you?" "${USER:-}"
CITY="${ARGUS_CITY:-}"
ask CITY "Your home city, used when your phone has not shared where you are (e.g. Portland, OR)" ""
TZ_NAME="${ARGUS_TIMEZONE:-$(getprop persist.sys.timezone 2>/dev/null || true)}"
[ -n "$TZ_NAME" ] || TZ_NAME="UTC"
ok "Time zone: $TZ_NAME"

GOOGLE_KEY="${ARGUS_GOOGLE_MAPS_KEY:-}"
[ "$CHANGE_KEYS" = 1 ] || GOOGLE_KEY="${GOOGLE_KEY:-$(secret_get GOOGLE_MAPS_API_KEY)}"
if [ -z "$GOOGLE_KEY" ] && is_tty; then
  echo "  Live traffic and better routes come from Google Maps: a free API key from console.cloud.google.com"
  echo "  (switch on the Routes API). Press Enter to skip; directions then open Google Maps without times."
  ask_secret GOOGLE_KEY "Google Maps API key (Enter to skip)" optional
fi
ask_tomtom_key
OLD_APPS=""; OLD_APP=""
if [ -f "$CONFIG" ]; then
  OLD_APP="$(toml_get "$CONFIG" maps app)"
  if grep -q '^apps[[:space:]]*=' "$CONFIG"; then OLD_APPS="$OLD_APP $(toml_get "$CONFIG" maps apps)"; fi
fi
choose_nav_apps android "${ARGUS_MAPS_APPS:-$OLD_APPS}" "$OLD_APP"

WA_KEY="${ARGUS_WHATSAPP_KEY:-}"
[ "$CHANGE_KEYS" = 1 ] || WA_KEY="${WA_KEY:-$(secret_get WHATSAPP_BACKUP_KEY)}"
if [ -z "$WA_KEY" ] && is_tty; then
  echo "  WhatsApp search reads WhatsApp's own encrypted backup, with the 64-digit key you chose in WhatsApp:"
  echo "  Settings > Chats > Chat backup > End-to-end encrypted backup > Use 64-digit encryption key."
  echo "  Press Enter to skip (texts and notifications are searchable without it)."
  ask_secret WA_KEY "WhatsApp 64-digit backup key (Enter to skip)" optional
fi
WA_KEY="$(printf '%s' "$WA_KEY" | tr -d ' \t-' | tr '[:upper:]' '[:lower:]')"
if [ -n "$WA_KEY" ] && ! printf '%s' "$WA_KEY" | grep -Eq '^[0-9a-f]{64}$'; then
  die "The WhatsApp key is 64 characters, digits 0-9 and letters a-f. Check it and run again (or skip it)."
fi

DRIVE="${ARGUS_DRIVE_ASSIST:-}"
if [ -z "$DRIVE" ]; then
  if [ -f "$CONFIG" ]; then DRIVE="$(toml_get "$CONFIG" android drive_assist)"; fi
  if [ -z "$DRIVE" ]; then
    echo "  Drive assist: while the phone charges and moves at driving speed, Argus asks where you're headed,"
    echo "  gives you the fastest route, and speaks up if traffic makes another route faster."
    if confirm "Turn on drive assist? (uses GPS only while charging)" y; then DRIVE=true; else DRIVE=false; fi
  fi
fi
case "$DRIVE" in yes|true|1) DRIVE=true ;; *) DRIVE=false ;; esac

# =====================================================================
step "4/7  WhatsApp backup reader"
WA_DIR="$ARGUS_HOME/wa-crypt-tools"
if [ -z "$WA_KEY" ]; then
  ok "Skipped (no WhatsApp key)"
elif [ -x "$WA_DIR/bin/wadecrypt" ] && [ "$(cat "$WA_DIR/lock.sha256" 2>/dev/null)" = "$(sha256 "$REPO_DIR/scripts/wa-crypt-tools.lock")" ]; then
  ok "wa-crypt-tools already installed"
elif [ "$DRY_RUN" = 1 ]; then
  run "$PYTHON" -m venv "$WA_DIR"
  run "$WA_DIR/bin/pip" install --require-hashes --no-deps --no-binary pycryptodomex -r "$REPO_DIR/scripts/wa-crypt-tools.lock"
else
  [ "${ARGUS_SKIP_PKG:-0}" = 1 ] || "$PKG" install -y clang        # to build its one C library (AES)
  mkdir -p "$ARGUS_HOME" && chmod 700 "$ARGUS_HOME"
  "$PYTHON" -m venv "$WA_DIR" || die "Couldn't create a Python environment for wa-crypt-tools."
  "$WA_DIR/bin/pip" install -q --disable-pip-version-check --require-hashes --no-deps \
    -r "$REPO_DIR/scripts/wa-crypt-tools-build.lock" || die "Couldn't install the build tools for wa-crypt-tools."
  "$WA_DIR/bin/pip" install -q --disable-pip-version-check --require-hashes --no-deps --no-build-isolation \
    --no-binary pycryptodomex -r "$REPO_DIR/scripts/wa-crypt-tools.lock" || die "Couldn't install wa-crypt-tools."
  [ -x "$WA_DIR/bin/wadecrypt" ] || die "wa-crypt-tools installed, but wadecrypt is missing."
  sha256 "$REPO_DIR/scripts/wa-crypt-tools.lock" > "$WA_DIR/lock.sha256"
  ok "wa-crypt-tools 0.1.0 (GPL-3.0, its own folder; every file hash-checked)"
fi

# =====================================================================
step "5/7  Saving keys and settings"
printf '%s' "$ANTHROPIC_KEY" | secret_set ANTHROPIC_API_KEY
[ -z "$GOOGLE_KEY" ] || printf '%s' "$GOOGLE_KEY" | secret_set GOOGLE_MAPS_API_KEY
[ -z "$TOMTOM_KEY" ] || printf '%s' "$TOMTOM_KEY" | secret_set TOMTOM_API_KEY
[ -z "$WA_KEY" ] || printf '%s' "$WA_KEY" | secret_set WHATSAPP_BACKUP_KEY
ok "Keys saved to $SECRETS (private to Termux)"
if [ "$DRY_RUN" = 1 ]; then
  printf '  [dry-run] write %s\n' "$CONFIG"
elif [ -f "$CONFIG" ] && [ "${ARGUS_REWRITE_CONFIG:-0}" != 1 ]; then
  toml_set "$CONFIG" phone type '"android"'
  toml_set "$CONFIG" android drive_assist "$DRIVE"
  ok "Kept your settings in $CONFIG (drive assist: $DRIVE)"
else
  umask 077
  cat > "$CONFIG" <<EOF
# Argus on this Android phone. Edit, then restart it: pkill -f "argus android serve"; argus-android serve &
name = "Argus"
user_name = $(toml_str "$NAME")
city = $(toml_str "$CITY")
timezone = $(toml_str "$TZ_NAME")

[brain]
model = "$MAIN_MODEL"           # conversations and tasks
small_model = "claude-haiku-5-5" # summarising long conversations, lessons
web_search = true               # Claude's web search (\$10 per 1,000 searches)
workspace = $(toml_str "$WORKSPACE")
max_turn_tokens = 400000        # stop a single request before it gets expensive
self_learning = true            # after a request that hit errors, Argus writes itself a lesson

[phone]
type = "android"                # this is Argus on the phone itself
enabled = false                 # (the phone gateway is for the Mac and the server)

[maps]
app = "$MAPS_APP"                  # the navigation app that opens: "google" or "waze"
apps = [$MAPS_APPS_TOML]                       # your other navigation apps: "google", "waze"
traffic = "auto"                # compares every traffic service that has a key (Google, TomTom); or one; "off"
news = true                     # look for the last day's news about the roads on a route
units = "imperial"              # or "metric"

[glasses]
enabled = false                 # the Meta glasses work through the Argus iPhone app

[android]
messages = true                 # search texts, WhatsApp and the notification log
notification_days = 90          # how long the notification log is kept
whatsapp_dir = ""               # "" = WhatsApp's usual backup folders
drive_assist = $DRIVE              # while charging and moving: ask where you're headed, watch the traffic
speak = true                    # say replies and reminders out loud

[schedule]
morning_headlines = ""          # e.g. "0 7 * * *" for headlines at 7 (a notification, read aloud)
morning_briefing = ""           # an AI-written briefing ("30 7 * * 1-5" = weekdays at 7:30)
EOF
  umask 022
  ok "Settings written to $CONFIG"
fi
run mkdir -p "$WORKSPACE"

# =====================================================================
step "6/7  Permissions (Android asks; allow each one)"
if [ "$DRY_RUN" = 1 ] || [ "$NO_SERVICE" = 1 ] || ! is_tty; then
  ok "Skipped here"
else
  echo "  Android will now ask a few times. Allow each, so Argus can read texts, contacts and location:"
  [ -d "$HOME/storage" ] || termux-setup-storage || true        # your files, for WhatsApp's backups
  termux-sms-list -l 1 > /dev/null 2>&1 || warn "Texts aren't allowed yet (Settings > Apps > Termux:API > Permissions > SMS)"
  termux-contact-list > /dev/null 2>&1 || warn "Contacts aren't allowed yet (Termux:API > Permissions > Contacts)"
  termux-location -p network -r last > /dev/null 2>&1 || warn "Location isn't allowed yet (Termux:API > Permissions > Location: allow all the time)"
  termux-notification-list > /dev/null 2>&1 || warn "Notification access is off: Settings > Notifications > Device & app notifications > Termux:API"
  echo "  Also, once: Settings > Apps > Termux and Termux:API > Battery > Unrestricted (so reminders aren't frozen),"
  echo "  and Termux:API > Display over other apps > Allow (so approvals can appear)."
fi

# =====================================================================
step "7/7  Home-screen button and start at boot"
LAUNCHER="$BIN_DIR/argus-android"
if [ "$DRY_RUN" = 1 ]; then
  printf '  [dry-run] write %s, %s/tasks/Argus and %s/argus\n' "$LAUNCHER" "$SHORTCUTS" "$BOOT_DIR"
else
  mkdir -p "$BIN_DIR" "$SHORTCUTS/tasks" "$BOOT_DIR" "$ARGUS_HOME/logs"
  cat > "$LAUNCHER" <<EOF
#!$SH
# argus-android: Argus on this phone (serve | talk | ask "..." | status)
exec $(quote "$PYTHON") -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from argus.__main__ import main; sys.exit(main())' $(quote "$REPO_DIR") android "\$@"
EOF
  chmod 700 "$LAUNCHER"
  printf '#!%s\n# The Argus home-screen button (Termux:Widget): listen once and answer out loud.\nexec %s talk\n' \
    "$SH" "$(quote "$LAUNCHER")" > "$SHORTCUTS/tasks/Argus"
  chmod 700 "$SHORTCUTS/tasks/Argus"
  printf '#!%s\n# Start Argus when the phone starts (Termux:Boot).\ntermux-wake-lock\nexec %s serve >> %s 2>&1\n' \
    "$SH" "$(quote "$LAUNCHER")" "$(quote "$ARGUS_HOME/logs/android.log")" > "$BOOT_DIR/argus"
  chmod 700 "$BOOT_DIR/argus"
  ok "Launcher: argus-android (try: argus-android status)"
  ok "Home-screen button: add the Termux:Widget widget and pick \"Argus\""
  ok "Starts by itself after a restart (Termux:Boot)"
fi
if [ "$NO_SERVICE" = 1 ] || [ "$DRY_RUN" = 1 ]; then
  warn "Not starting it now (--no-service)."
else
  if "$LAUNCHER" status > /dev/null 2>&1; then
    pkill -f "argus android serve" 2> /dev/null || true
    sleep 1
  fi
  nohup "$LAUNCHER" serve >> "$ARGUS_HOME/logs/android.log" 2>&1 &
  for _ in $(seq 1 20); do "$LAUNCHER" status > /dev/null 2>&1 && break; sleep 0.5; done
  if "$LAUNCHER" status > /dev/null 2>&1; then ok "Argus is running"; else warn "Argus didn't start; see $ARGUS_HOME/logs/android.log"; fi
fi

cat <<EOF

${B}Done.${N} Argus now lives on this phone.
  - Talk: tap the Argus button on the home screen (Termux:Widget), or type: argus-android ask "what's on today?"
  - Try: "Find the wifi password Priya sent me", "How's the traffic to the airport?", "Remind me at 6 to call home"
  - What it can see and how it stays private: docs/ANDROID.md
EOF
