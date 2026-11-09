#!/bin/bash
# Keep this Mac from sleeping while it's plugged in, so Argus can always answer.
# The display can still turn off. Closing a MacBook's lid still puts it to sleep,
# unless an external display is connected.
#
#   stay-awake.sh on       start (also starts again after every login)
#   stay-awake.sh off      stop and remove
#   stay-awake.sh status
set -euo pipefail

LABEL="com.argus.stay-awake"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ "$(uname -s)" != Darwin ]; then
  echo "stay-awake is only needed on macOS."
  exit 0
fi

case "${1:-status}" in
  on)
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/caffeinate</string><string>-s</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
EOF
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "On: this Mac stays awake while plugged in (the screen can still turn off)."
    ;;
  off)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Off: your normal sleep settings are back."
    ;;
  status)
    if launchctl print "$DOMAIN/$LABEL" > /dev/null 2>&1; then echo "on"; else echo "off"; fi
    ;;
  *)
    echo "usage: $0 on|off|status" >&2
    exit 2
    ;;
esac
