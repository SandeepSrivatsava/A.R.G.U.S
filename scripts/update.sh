#!/bin/bash
# Update Argus: pull this repo, refresh the hash-pinned Python packages, and restart.
# Run it every week or two (and re-run ./setup.sh if setup.sh itself changed).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${ARGUS_HOME:-$HOME/.argus}/venv"
[ -x "$VENV/bin/python3" ] || { echo "Argus isn't installed. Run ./setup.sh" >&2; exit 1; }

echo "Updating this repo..."
git -C "$REPO_DIR" pull --ff-only

echo "Updating Argus's Python packages (hash-checked)..."
"$VENV/bin/python3" -m pip install -q --disable-pip-version-check --require-hashes --only-binary :all: \
  -r "$REPO_DIR/requirements-build.lock"
"$VENV/bin/python3" -m pip install -q --disable-pip-version-check --require-hashes --only-binary :all: \
  --no-binary rumps --no-build-isolation -r "$REPO_DIR/requirements-macos.lock"
# pytest, so Argus can test a fix to its own code before offering it (hash-checked too)
"$VENV/bin/python3" -m pip install -q --disable-pip-version-check --require-hashes --only-binary :all: \
  -r "$REPO_DIR/requirements-dev.lock"

# Echo cancellation helper (macos/argus-ear.swift), rebuilt when it changes
BIN="${ARGUS_HOME:-$HOME/.argus}/bin"
if command -v swiftc >/dev/null 2>&1 && [ -f "$REPO_DIR/macos/argus-ear.swift" ]; then
  ear_sum="$(shasum -a 256 "$REPO_DIR/macos/argus-ear.swift" | cut -d' ' -f1)"
  if [ "$(cat "$BIN/argus-ear.sha256" 2>/dev/null)" != "$ear_sum" ]; then
    echo "Building the echo cancellation helper..."
    mkdir -p "$BIN"
    if swiftc -O -swift-version 5 -o "$BIN/argus-ear" "$REPO_DIR/macos/argus-ear.swift" > /dev/null 2>&1; then
      printf '%s\n' "$ear_sum" > "$BIN/argus-ear.sha256"
    else
      echo "Couldn't build it; Argus keeps using the plain microphone."
    fi
  fi
fi

"$REPO_DIR/bin/argusctl" restart
echo "Done."
