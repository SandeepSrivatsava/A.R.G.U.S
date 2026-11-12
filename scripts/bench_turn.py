"""How much of a spoken answer's time is Argus's own Python code? (The Python vs Go question.)

Runs a whole turn, two Claude requests and one tool, against the stand-in Claude on this computer, so the
network and the model take ~0 ms and what's left is Argus itself (plus the stand-in server's own work, so
this overstates Argus). Compare with `argusctl timing`, where a real turn takes seconds.

    ARGUS_TEST_MODELS=... python scripts/bench_turn.py
"""
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from argus.agent import Events  # noqa: E402
from argus.toolbox import ToolOutput  # noqa: E402
from fakes import Script, call, make_agent, say  # noqa: E402

ANSWER = ("Oh nice, New York next month! Quick question so I don't throw a whole guidebook at you: is this "
          "more of a food and shows trip, or museums and walking around?")


def main(runs: int = 30) -> None:
    times = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(runs):
            folder = Path(tmp) / str(i)
            folder.mkdir()
            agent, mock = make_agent(folder, Script(call("get_news", {}), say(ANSWER)))
            agent.tools["get_news"].run = lambda args, ctx: ToolOutput("headlines")
            began = time.perf_counter()
            agent.run(session_id="s", channel="voice", content="I'm going to New York next month", events=Events())
            times.append((time.perf_counter() - began) * 1000)
            mock.shutdown()
    times = sorted(times[3:])
    print(f"Argus's own work for a whole turn: median {statistics.median(times):.1f} ms, "
          f"slowest {times[-1]:.1f} ms (a real spoken turn: 2,000-5,000 ms)")


if __name__ == "__main__":
    main()
