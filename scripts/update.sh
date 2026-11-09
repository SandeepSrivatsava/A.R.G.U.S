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
  -r "$REPO_DIR/requirements-macos.lock"

"$REPO_DIR/bin/argusctl" restart
echo "Done."
