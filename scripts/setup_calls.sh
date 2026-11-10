#!/bin/bash
# Phone calls from your own number (optional, iPhone only). Installs what Argus needs to place calls
# through your iPhone from the Mac and talk on them, then switches calls on. Safe to run again.
#
#   scripts/setup_calls.sh            set up and switch calls on
#   scripts/setup_calls.sh --off      switch calls off again (nothing is uninstalled)
#
# What it installs:
#   - BlackHole 2ch (GPL-3.0): a virtual audio cable. During a call it is the call's microphone, so the
#     other side hears Argus. Needs your Mac password once (it's an audio driver).
#   - SwitchAudioSource (MIT): switches the Mac's microphone to BlackHole for the call and back after.
#   - audiotee (MIT, Nick Payne): records what the Mac plays (the other side of the call), leaving out
#     Argus's own sound. Built here from source (a pinned commit) with Swift, into ~/.argus/bin.
set -euo pipefail

ARGUS_HOME="${ARGUS_HOME:-$HOME/.argus}"
CONFIG="$ARGUS_HOME/config.toml"
BIN="$ARGUS_HOME/bin"
SRC="$ARGUS_HOME/src/audiotee"
AUDIOTEE_REPO="https://github.com/makeusabrew/audiotee.git"
# Pinned: the exact source that was reviewed for Argus (git checks every file against this commit hash).
AUDIOTEE_COMMIT="56ac954369a09318e46b88a6eec33c2d2b0d32a3"

# shellcheck source=scripts/lib.sh
. "$(dirname "$0")/lib.sh"

set_calls() {  # usage: set_calls true|false  (edits [calls] enabled in config.toml)
    local value="$1"
    [ -f "$CONFIG" ] || die "No $CONFIG yet. Run ./setup.sh first."
    if grep -q '^\[calls\]' "$CONFIG"; then
        if awk '/^\[calls\]/{s=1;next} /^\[/{s=0} s && /^enabled[[:space:]]*=/{f=1} END{exit !f}' "$CONFIG"; then
            awk -v v="$value" '/^\[calls\]/{s=1;print;next} /^\[/{s=0} s && /^enabled[[:space:]]*=/{print "enabled = " v; next} {print}' \
                "$CONFIG" > "$CONFIG.tmp"
        else
            awk -v v="$value" '{print} /^\[calls\]/{print "enabled = " v}' "$CONFIG" > "$CONFIG.tmp"
        fi
        mv "$CONFIG.tmp" "$CONFIG"
    else
        printf '\n[calls]\nenabled = %s\n' "$value" >> "$CONFIG"
    fi
    chmod 600 "$CONFIG"
}

if [ "${1:-}" = "--off" ]; then
    set_calls false
    ok "Phone calls switched off. Now run: argusctl restart"
    exit 0
fi

if [ -f "$CONFIG" ] && [ "$(toml_get "$CONFIG" phone type)" = android ]; then
    die "Calls from your own number go through an iPhone (Continuity), and Argus is set up for Android. \
On Android, Argus opens the dialer for you instead. (Switch phones with: ./setup.sh --phone iphone)"
fi

[ "$(uname -s)" = "Darwin" ] || die "Phone calls need a Mac."
MACOS="$(sw_vers -productVersion)"
MAJOR="${MACOS%%.*}"; REST="${MACOS#*.}"; MINOR="${REST%%.*}"
if [ "$MAJOR" -lt 14 ] || { [ "$MAJOR" -eq 14 ] && [ "${MINOR:-0}" -lt 2 ]; }; then
    die "macOS 14.2 or newer is needed to hear calls (this Mac has $MACOS)."
fi
command -v brew >/dev/null || die "Homebrew is needed: https://brew.sh"
command -v swift >/dev/null || die "Swift is needed to build the call recorder. Run: xcode-select --install"

echo "Setting up phone calls from your own number…"
brew list --cask blackhole-2ch >/dev/null 2>&1 || brew install --cask blackhole-2ch
ok "BlackHole 2ch (the call's microphone during a call)"
brew list switchaudio-osx >/dev/null 2>&1 || brew install switchaudio-osx
ok "SwitchAudioSource"

mkdir -p "$BIN" "$(dirname "$SRC")"
chmod 700 "$ARGUS_HOME" "$BIN"
if [ ! -d "$SRC/.git" ]; then
    git init --quiet "$SRC"
    git -C "$SRC" remote add origin "$AUDIOTEE_REPO"
fi
git -C "$SRC" fetch --quiet --depth 1 origin "$AUDIOTEE_COMMIT"
git -C "$SRC" checkout --quiet --detach FETCH_HEAD
[ "$(git -C "$SRC" rev-parse HEAD)" = "$AUDIOTEE_COMMIT" ] || die "the audiotee source doesn't match the pinned commit"
(cd "$SRC" && swift build -c release --quiet)
install -m 755 "$SRC/.build/release/audiotee" "$BIN/audiotee"
ok "Call recorder (audiotee ${AUDIOTEE_COMMIT:0:7}) in $BIN"

set_calls true
ok "Phone calls switched on in $CONFIG"

cat <<'EOF'

Two settings to check once:
  1. iPhone: Settings → Apps → Phone → Calls on Other Devices → turn on, and allow this Mac.
  2. Mac: FaceTime → Settings → General → tick "Calls from iPhone".
Then run: argusctl restart
The first call asks to allow "System Audio Recording" for Argus: allow it, or Argus
can't hear the other side.

Try it: "Argus, call <a restaurant> and ask what time they close today."
Say "Argus, hang up" or "Argus, let me talk" at any point.
If the other side can't hear Argus: in FaceTime's Video menu, check the microphone isn't
fixed to a particular mic (it should follow the Mac's setting).
EOF
