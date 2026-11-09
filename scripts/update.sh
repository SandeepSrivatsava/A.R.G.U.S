#!/bin/bash
# Update Argus: pull this repo, update Hermes (it backs up your settings first), refresh the
# hash-pinned Python packages, and restart everything. Run it every week or two;
# Hermes ships security fixes often.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES="${HERMES_BIN:-$(command -v hermes 2>/dev/null || echo "$HOME/.local/bin/hermes")}"
VENV="$HOME/.argus/venv"
[ -x "$HERMES" ] || { echo "Hermes is not installed. Run ./setup.sh" >&2; exit 1; }

echo "Updating this repo..."
git -C "$REPO_DIR" pull --ff-only

echo "Updating Hermes..."
"$HERMES" update --yes

echo "Updating Argus's Python packages (hash-checked)..."
"$VENV/bin/python3" -m pip install -q --disable-pip-version-check --require-hashes --only-binary :all: \
  -r "$REPO_DIR/requirements-macos.lock"

install -m 755 "$REPO_DIR/skills/news-briefing/scripts/news.py" "$HERMES_HOME/scripts/argus_news.py"
"$HERMES" gateway restart
"$REPO_DIR/bin/argusctl" restart
echo "Done: $("$HERMES" --version 2>/dev/null | head -1). If setup.sh changed, run ./setup.sh again too."
