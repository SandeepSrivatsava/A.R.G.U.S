#!/usr/bin/env bash
# Argus server mode: a small always-on Linux machine (a cloud server for a few dollars a month) so Argus
# answers your phone and sends your reminders when the Mac is off or far away. On Debian 12+ or Ubuntu 24.04+:
#
#   git clone https://github.com/SandeepSrivatsava/A.R.G.U.S argus && cd argus && sudo scripts/setup_server.sh
#
# It checks Tailscale is connected (the phone reaches Argus only through Tailscale), copies Argus to
# /opt/argus (owned by root, read-only to Argus), creates a locked-down "argus" user whose data and keys
# live in /var/lib/argus, asks for the keys (typing hidden), installs a hardened systemd service and starts
# it. Afterwards `sudo argus-admin status` (or any argusctl command) talks to it. Safe to run again.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib.sh
. "$REPO_DIR/scripts/lib.sh"

OPT_DIR="${ARGUS_OPT_DIR:-/opt/argus}"
STATE_DIR="${ARGUS_STATE_DIR:-/var/lib/argus}"
ARGUS_HOME="$STATE_DIR/.argus"
SECRETS="$ARGUS_HOME/secrets.env"
CONFIG="$ARGUS_HOME/config.toml"
UNIT_DIR="${ARGUS_UNIT_DIR:-/etc/systemd/system}"
ADMIN="${ARGUS_ADMIN_BIN:-/usr/local/sbin/argus-admin}"
SERVICE_USER="${ARGUS_SERVICE_USER:-argus}"
PYTHON="${ARGUS_PYTHON:-/usr/bin/python3}"
TAILSCALE="${ARGUS_TAILSCALE:-tailscale}"
MAIN_MODEL="${ARGUS_MAIN_MODEL:-claude-sonnet-5-5}"
PORT=8765

DRY_RUN=0; ASSUME_YES=0; NO_SERVICE=0; CHANGE_KEYS=0; PHONE_FLAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --no-service) NO_SERVICE=1 ;;          # write everything, but don't create the user or start the service
    --change-keys) CHANGE_KEYS=1 ;;
    --phone) [ $# -ge 2 ] || { echo "--phone needs iphone or android" >&2; exit 2; }; PHONE_FLAG="$2"; shift ;;
    --phone=*) PHONE_FLAG="${1#--phone=}" ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

anthropic_check() {  # the key is fed to curl on stdin, never on its command line
  local code
  code="$(printf 'header = "x-api-key: %s"\n' "$1" | curl -sS -m 20 -K - -o /dev/null -w '%{http_code}' \
          -H 'anthropic-version: 2023-06-01' 'https://api.anthropic.com/v1/models?limit=1' 2>/dev/null)" || code=000
  [ "$code" = 200 ]
}
safe_dir() { case "$1" in ""|/|/opt|/var|/var/lib|/usr|/etc|/home) return 1 ;; *) return 0 ;; esac; }

# =====================================================================
step "1/7  Checking this server"
[ "$(uname -s)" = Linux ] || die "Server mode is for a Linux machine. On a Mac run ./setup.sh."
if [ "$NO_SERVICE" = 0 ] && [ "$DRY_RUN" = 0 ]; then
  [ "$(id -u)" = 0 ] || die "Run it with sudo: sudo scripts/setup_server.sh"
  command -v systemctl > /dev/null || die "This server has no systemd; Argus's service needs it (Debian or Ubuntu do)."
fi
"$PYTHON" -c 'import sys, sqlite3, ssl, tomllib; sys.exit(sys.version_info < (3, 11))' 2> /dev/null \
  || die "Argus needs Python 3.11 or newer (Debian 12, Ubuntu 24.04 or later): sudo apt install python3"
ok "Python $("$PYTHON" -c 'import platform; print(platform.python_version())') (Argus needs nothing else installed)"
command -v curl > /dev/null || die "curl is missing: sudo apt install curl"
TS_IP="$("$TAILSCALE" ip -4 2>/dev/null | head -1 || true)"
case "$TS_IP" in
  100.*) ok "Tailscale connected: $TS_IP" ;;
  *) die "Tailscale isn't connected. Your phone reaches Argus only through it. Install it from
  https://tailscale.com/download/linux (or: sudo apt install tailscale, after adding Tailscale's repository),
  then run: sudo tailscale up   and run this script again." ;;
esac

# =====================================================================
step "2/7  Your phone"
OLD_PHONE=""
[ -f "$CONFIG" ] && OLD_PHONE="$(toml_get "$CONFIG" phone type)"
GIVEN="${PHONE_FLAG:-${ARGUS_PHONE:-}}"
PHONE="$(normalize_phone "$GIVEN")"
[ -z "$GIVEN" ] || [ -n "$PHONE" ] || die "--phone must be iphone or android"
if [ -z "$PHONE" ]; then
  if is_tty; then
    echo "  Which phone will talk to this server?   1) iPhone   2) Android"
    while [ -z "$PHONE" ]; do
      read -r -p "  Type 1 or 2: " answer
      PHONE="$(normalize_phone "$answer")"
    done
  else
    PHONE="${OLD_PHONE:-iphone}"
    warn "No terminal to ask on: setting up for $PHONE (choose with --phone iphone|android)"
  fi
fi
if [ "$PHONE" = android ]; then MAPS_APP=google; else MAPS_APP=apple; fi
ok "$PHONE (directions open in $( [ "$MAPS_APP" = apple ] && echo Apple || echo Google ) Maps)"

# =====================================================================
step "3/7  Keys and details (kept in $SECRETS, readable only by the argus user)"
ANTHROPIC_KEY="${ARGUS_ANTHROPIC_API_KEY:-}"
[ "$CHANGE_KEYS" = 1 ] || ANTHROPIC_KEY="${ANTHROPIC_KEY:-$(secret_get ANTHROPIC_API_KEY)}"
if [ -n "$ANTHROPIC_KEY" ]; then
  ok "Claude API key: found (ending ...$(printf '%s' "$ANTHROPIC_KEY" | tail -c 4))"
else
  echo "  Tip: make a separate Claude key for the server (platform.claude.com), so you can cancel it alone."
fi
ask_secret ANTHROPIC_KEY "Claude API key (starts with sk-ant-)"
case "$ANTHROPIC_KEY" in sk-ant-*) ;; *) die "That doesn't look like a Claude API key (it should start with sk-ant-)." ;; esac
if [ "${ARGUS_SKIP_VALIDATION:-0}" = 1 ]; then
  warn "Skipping the live key check (ARGUS_SKIP_VALIDATION=1)"
elif anthropic_check "$ANTHROPIC_KEY"; then
  ok "Claude API key works"
else
  die "The Claude API rejected that key (check it, and that your account has credit)."
fi
NAME="${ARGUS_NAME:-}"
[ -n "$NAME" ] || [ ! -f "$CONFIG" ] || NAME="$(toml_get "$CONFIG" "" user_name)"
ask NAME "What should Argus call you?" ""
CITY="${ARGUS_CITY:-}"
[ -n "$CITY" ] || [ ! -f "$CONFIG" ] || CITY="$(toml_get "$CONFIG" "" city)"
ask CITY "Your city, for weather (e.g. Portland, OR)" ""
TZ_NAME="${ARGUS_TIMEZONE:-}"
[ -n "$TZ_NAME" ] || [ ! -f "$CONFIG" ] || TZ_NAME="$(toml_get "$CONFIG" "" timezone)"
[ -n "$TZ_NAME" ] || TZ_NAME="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
if [ -z "$TZ_NAME" ] || [ "$TZ_NAME" = Etc/UTC ]; then      # cloud servers run on UTC: ask for yours
  if is_tty; then TZ_NAME=""; ask TZ_NAME "Your time zone (e.g. America/Los_Angeles)" "UTC"; else TZ_NAME=UTC; fi
fi
"$PYTHON" -I -c 'import sys, zoneinfo; zoneinfo.ZoneInfo(sys.argv[1])' "$TZ_NAME" 2> /dev/null \
  || die "Unknown time zone $TZ_NAME (use a name like America/Los_Angeles)"
ok "Time zone: $TZ_NAME"

GOOGLE_KEY="${ARGUS_GOOGLE_MAPS_KEY:-}"
[ "$CHANGE_KEYS" = 1 ] || GOOGLE_KEY="${GOOGLE_KEY:-$(secret_get GOOGLE_MAPS_API_KEY)}"
APPLE_TOKEN="${ARGUS_APPLE_MAPS_TOKEN:-}"
[ "$CHANGE_KEYS" = 1 ] || APPLE_TOKEN="${APPLE_TOKEN:-$(secret_get APPLE_MAPS_TOKEN)}"
if [ -z "$GOOGLE_KEY" ] && [ -z "$APPLE_TOKEN" ] && is_tty; then
  echo "  Live traffic for directions (optional; Enter skips each):"
  echo "   - a Google Maps API key (console.cloud.google.com, with the Routes API switched on), and/or"
  echo "   - an Apple Maps token: on your Mac, argusctl maps-token --print ... (docs/SERVER.md)"
  ask_secret GOOGLE_KEY "Google Maps API key" optional
  ask_secret APPLE_TOKEN "Apple Maps token" optional
fi

TOPIC="${ARGUS_NTFY_TOPIC:-$(secret_get NTFY_TOPIC)}"
[ -n "$TOPIC" ] || TOPIC="$("$PYTHON" -I -c 'import secrets; print("argus-" + secrets.token_urlsafe(24))')"
PUSH_SERVER="${ARGUS_PUSH_SERVER:-https://ntfy.sh}"
ok "Push notifications: ntfy topic ready (shown at the end)"

# =====================================================================
step "4/7  Installing Argus into $OPT_DIR (owned by root, read-only to Argus)"
safe_dir "$OPT_DIR" || die "ARGUS_OPT_DIR=$OPT_DIR isn't a safe place to install into"
if [ "$DRY_RUN" = 1 ]; then
  printf '  [dry-run] copy argus/ to %s\n' "$OPT_DIR"
else
  mkdir -p "$OPT_DIR"
  rm -rf "${OPT_DIR:?}/argus.new"
  cp -R "$REPO_DIR/argus" "$OPT_DIR/argus.new"
  find "$OPT_DIR/argus.new" -name '__pycache__' -type d -prune -exec rm -rf {} +
  rm -rf "${OPT_DIR:?}/argus"
  mv "$OPT_DIR/argus.new" "$OPT_DIR/argus"
  cp "$REPO_DIR/LICENSE" "$REPO_DIR/THIRD_PARTY_NOTICES.md" "$OPT_DIR/"
  cat > "$OPT_DIR/run.py" <<EOF
# Started by systemd and argus-admin as: python3 -I $OPT_DIR/run.py <argusctl command>.
# -I: nothing from the environment or the current folder; Argus is loaded only from this root-owned folder.
import sys
sys.path.insert(0, $(toml_str "$OPT_DIR"))
from argus.__main__ import main
sys.exit(main())
EOF
  "$PYTHON" -I -m compileall -q "$OPT_DIR/argus" > /dev/null || true
  if [ "$NO_SERVICE" = 0 ]; then chown -R root:root "$OPT_DIR"; fi
  chmod -R u+rwX,go+rX,go-w "$OPT_DIR"
  ok "Argus $(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO_DIR/pyproject.toml") installed"
fi

# =====================================================================
step "5/7  The argus user, its keys and settings"
if [ "$NO_SERVICE" = 1 ] || [ "$DRY_RUN" = 1 ]; then
  warn "Not creating the $SERVICE_USER user (--no-service or --dry-run)"
elif ! id "$SERVICE_USER" > /dev/null 2>&1; then
  useradd --system --user-group --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  ok "Created the $SERVICE_USER user (no password, no login)"
else
  ok "The $SERVICE_USER user exists"
fi
run mkdir -p "$ARGUS_HOME" "$STATE_DIR/workspace"
run chmod 700 "$STATE_DIR" "$ARGUS_HOME"
printf '%s' "$ANTHROPIC_KEY" | secret_set ANTHROPIC_API_KEY
[ -z "$GOOGLE_KEY" ] || printf '%s' "$GOOGLE_KEY" | secret_set GOOGLE_MAPS_API_KEY
[ -z "$APPLE_TOKEN" ] || printf '%s' "$APPLE_TOKEN" | secret_set APPLE_MAPS_TOKEN
printf '%s' "$TOPIC" | secret_set NTFY_TOPIC
if [ "$DRY_RUN" = 1 ]; then
  printf '  [dry-run] write %s\n' "$CONFIG"
elif [ -f "$CONFIG" ] && [ "${ARGUS_REWRITE_CONFIG:-0}" != 1 ]; then
  toml_set "$CONFIG" phone type "\"$PHONE\""
  toml_set "$CONFIG" phone host "\"$TS_IP\""
  [ "$OLD_PHONE" = "$PHONE" ] || toml_set "$CONFIG" maps app "\"$MAPS_APP\""
  ok "Kept your settings in $CONFIG (phone: $PHONE, address $TS_IP)"
else
  umask 077
  cat > "$CONFIG" <<EOF
# Argus server mode. Edit, then: sudo systemctl restart argus
name = "Argus"
user_name = $(toml_str "$NAME")
city = $(toml_str "$CITY")
timezone = $(toml_str "$TZ_NAME")

[brain]
model = "$MAIN_MODEL"           # conversations and tasks
small_model = "claude-haiku-5-5" # summarising long conversations, lessons
web_search = true               # Claude's web search (\$10 per 1,000 searches)
workspace = $(toml_str "$STATE_DIR/workspace")
max_turn_tokens = 400000        # stop a single request before it gets expensive
self_learning = true

[phone]
type = "$PHONE"                 # "iphone" or "android": which phone talks to this server
enabled = true                  # the phone gateway, on this server's Tailscale address only
host = "$TS_IP"
port = $PORT

[maps]
app = "$MAPS_APP"                 # navigation links open in "apple" or "google" Maps
traffic = "auto"                # Google (GOOGLE_MAPS_API_KEY) or Apple (APPLE_MAPS_TOKEN); "off"
units = "imperial"              # or "metric"

[push]
enabled = true                  # reminders and results arrive as phone notifications (ntfy)
server = $(toml_str "$PUSH_SERVER")

[glasses]
enabled = false                 # the glasses go through the Argus iPhone app and the Mac

[schedule]
morning_headlines = "0 7 * * *" # free headlines as a notification ("" = off)
morning_briefing = ""           # AI briefing, e.g. "30 7 * * 1-5"
EOF
  umask 022
  ok "Settings written to $CONFIG"
fi
if [ "$NO_SERVICE" = 0 ] && [ "$DRY_RUN" = 0 ]; then
  chown -R "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR"
fi

# =====================================================================
step "6/7  The service (systemd, locked down)"
UNIT="$UNIT_DIR/argus.service"
if [ "$DRY_RUN" = 1 ]; then
  printf '  [dry-run] write %s and %s\n' "$UNIT" "$ADMIN"
else
  mkdir -p "$UNIT_DIR" "$(dirname "$ADMIN")"
  cat > "$UNIT" <<EOF
[Unit]
Description=Argus assistant (server mode)
Documentation=https://github.com/SandeepSrivatsava/A.R.G.U.S/blob/main/docs/SERVER.md
Wants=network-online.target tailscaled.service
After=network-online.target tailscaled.service

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
Environment=ARGUS_HOME=$ARGUS_HOME ARGUS_MODE=server
WorkingDirectory=$STATE_DIR
ExecStart=$PYTHON -I $OPT_DIR/run.py server
Restart=on-failure
RestartSec=10
UMask=0077
# Only its own folder is writable; it can't gain privileges, see other users' files or change the system.
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=$STATE_DIR
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
DevicePolicy=closed
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
ProcSubset=pid
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
KeyringMode=private
RemoveIPC=yes

[Install]
WantedBy=multi-user.target
EOF
  chmod 644 "$UNIT"
  cat > "$ADMIN" <<EOF
#!/bin/sh
# argus-admin: argusctl for this server's Argus, run as the $SERVICE_USER user. Example: sudo argus-admin status
[ "\$(id -u)" = 0 ] || { echo "Run it with sudo: sudo argus-admin \$*" >&2; exit 1; }
exec setpriv --reuid=$SERVICE_USER --regid=$SERVICE_USER --init-groups --no-new-privs \\
  env -i ARGUS_HOME=$ARGUS_HOME ARGUS_MODE=server HOME=$STATE_DIR LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin \\
  $PYTHON -I $OPT_DIR/run.py "\$@"
EOF
  chmod 755 "$ADMIN"
  ok "Service file $UNIT and the admin command $ADMIN"
fi
if [ "$NO_SERVICE" = 1 ] || [ "$DRY_RUN" = 1 ]; then
  warn "Not starting the service (--no-service or --dry-run)"
else
  systemctl daemon-reload
  systemctl enable argus > /dev/null 2>&1
  systemctl restart argus
  for _ in $(seq 1 30); do "$ADMIN" status > /dev/null 2>&1 && break; sleep 1; done
  if "$ADMIN" status > /dev/null 2>&1; then ok "Argus is running (sudo argus-admin status)"
  else warn "Argus didn't answer yet; see: sudo journalctl -u argus -n 50"; fi
fi

# =====================================================================
step "7/7  Connect your phone"
TOKEN=""
if [ "$NO_SERVICE" = 0 ] && [ "$DRY_RUN" = 0 ]; then TOKEN="$("$ADMIN" phone-token 2>/dev/null || true)"; fi
cat <<EOF
  Address for the phone:  http://$TS_IP:$PORT   (the phone needs the Tailscale app, signed in)
  Token:                  ${TOKEN:-run: sudo argus-admin phone-token}
  Notifications:          install ntfy (App Store / Google Play), Subscribe to topic:
                            server $PUSH_SERVER, topic $TOPIC
                          (keep the topic private: anyone who knows it can read your reminders)

${B}Done.${N} Set up the phone's shortcut with that address and token: $( [ "$PHONE" = android ] && echo docs/ANDROID.md || echo docs/IPHONE.md ).
Try it here first: sudo argus-admin ask "what's the weather tomorrow?"
EOF
