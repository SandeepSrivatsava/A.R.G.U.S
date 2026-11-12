"""setup.sh's start of Argus at login: macOS stops the old Argus a moment after `bootout` returns, so starting
it straight away failed with "Bootstrap failed: 5: Input/output error"."""
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNCTION = re.search(r"^start_argus\(\) \{.*?^\}", (ROOT / "setup.sh").read_text(), re.S | re.M).group(0)

FAKE = """#!/bin/bash
# A stand-in launchctl: the old Argus stays loaded for $GONE_AFTER checks; bootstrap fails while it's loaded,
# and fails $FAIL_TIMES more times after that.
state="$STATE"
echo "$*" >> "$state/calls"
n=$(cat "$state/prints" 2>/dev/null || echo 0)
case "$1" in
  print) echo $((n + 1)) > "$state/prints"; [ "$n" -lt "$GONE_AFTER" ] && exit 0; [ -f "$state/loaded" ] && exit 0; exit 1;;
  bootstrap)
    f=$(cat "$state/fails" 2>/dev/null || echo 0)
    if [ "$n" -lt "$GONE_AFTER" ] || [ "$f" -lt "$FAIL_TIMES" ]; then
      echo $((f + 1)) > "$state/fails"; echo "Bootstrap failed: 5: Input/output error" >&2; exit 5
    fi
    touch "$state/loaded"; exit 0;;
  kickstart) exit 0;;
esac
"""


def run(tmp_path, gone_after, fail_times):
    bin_dir, state, home = tmp_path / "bin", tmp_path / "state", tmp_path / "home"
    for d in (bin_dir, state, home / "logs"):
        d.mkdir(parents=True)
    for name, body in (("launchctl", FAKE), ("sleep", "#!/bin/sh\nexit 0\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", STATE=str(state), GONE_AFTER=str(gone_after),
               FAIL_TIMES=str(fail_times), UID_NUM="501", ARGUS_HOME=str(home), LAUNCH_AGENTS=str(tmp_path))
    script = FUNCTION + "\nstart_argus\n"
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    return result, (state / "calls").read_text().splitlines()


def test_waits_for_the_old_argus_to_stop_before_starting(tmp_path):
    result, calls = run(tmp_path, gone_after=3, fail_times=0)
    assert result.returncode == 0
    assert [c.split()[0] for c in calls] == ["print"] * 4 + ["bootstrap"]


def test_tries_again_when_macos_is_slow(tmp_path):
    result, calls = run(tmp_path, gone_after=0, fail_times=2)
    assert result.returncode == 0 and sum(c.startswith("bootstrap") for c in calls) == 3


def test_says_why_when_it_cant_start(tmp_path):
    result, calls = run(tmp_path, gone_after=0, fail_times=99)
    assert result.returncode == 1 and "Input/output error" in result.stderr
    assert sum(c.startswith("bootstrap") for c in calls) == 5
