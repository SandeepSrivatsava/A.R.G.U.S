#!/usr/bin/env bash
# Runs the test suite with coverage (including the real Argus processes some tests start) and prints which
# lines no test reaches. Optional: --e2e also runs tests/e2e.sh (setup, the whole app, real speech models).
#
# Usage: scripts/coverage.sh [--e2e]
#   ARGUS_VENV          Python environment with Argus's packages, pytest and coverage (default: ~/.argus/venv)
#   ARGUS_TEST_MODELS   folder with the speech models for the real-audio tests (default: ~/.argus/models)
#   ARGUS_MODEL_MIRROR  for --e2e: folder with the model archives, so setup doesn't download them again
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${ARGUS_VENV:-$HOME/.argus/venv}"
PY="$VENV/bin/python"
export ARGUS_TEST_MODELS="${ARGUS_TEST_MODELS:-$HOME/.argus/models}"
"$PY" -c "import coverage, pytest" 2>/dev/null || {
  echo "Needs pytest and coverage in $VENV:  $PY -m pip install pytest coverage"; exit 1; }

DATA="$(mktemp -d)"
trap 'rm -rf "$DATA"' EXIT
cd "$REPO"
COVERAGE_FILE="$DATA/.coverage" "$PY" -m coverage run -m pytest -p no:cacheprovider -q
if [ "${1:-}" = "--e2e" ]; then
  ARGUS_VENV="$VENV" ARGUS_COVERAGE="$DATA/.coverage.e2e" bash tests/e2e.sh
fi
COVERAGE_FILE="$DATA/.coverage" "$PY" -m coverage combine -q "$DATA"
COVERAGE_FILE="$DATA/.coverage" "$PY" -m coverage report
