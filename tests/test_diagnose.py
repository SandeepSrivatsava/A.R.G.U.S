"""Seeing what went wrong: `argusctl errors` and `argusctl report` (diagnose.py)."""
import json
import threading
import time

from argus import diagnose
from argus.__main__ import main
from argus.agent import Events
from argus.audit import Audit

from fakes import Script, call, make_agent, say


def test_problems_are_told_in_plain_words(tmp_path):
    audit = Audit(tmp_path / "audit.jsonl")
    audit.log("request", text="what I said stays out")
    audit.log("llm_error", error="Claude API error 400: messages.3: bad block")
    audit.log("turn", error="Claude API error 400: messages.3: bad block", model="claude-haiku-5-5")
    audit.log("turn", model="claude-haiku-5-5")
    audit.log("tool_failed", tool="weather", error="couldn't reach api.open-meteo.com\n(timed out)")
    audit.log("tool_blocked", tool="run_command", reason="not allowed: sudo")
    audit.log("approval_failed", tool="write_file", error="dialog crashed")
    audit.log("fresh_session_after_refusal", error="Claude API error 400")
    audit.log("handed_over", to="claude-sonnet-5-5")
    audit.log("tool_slow", tool="route", seconds=7.2)
    (tmp_path / "audit.jsonl").open("a").write("not json\n[1, 2]\n")
    found = diagnose.problems(diagnose.read_entries(tmp_path / "audit.jsonl"))
    told = [line[21:] for line in found]
    assert told == ["Claude: Claude API error 400: messages.3: bad block",
                    "A request failed: Claude API error 400: messages.3: bad block",
                    "The weather tool failed: couldn't reach api.open-meteo.com (timed out)",
                    "Blocked by Argus's rules (run_command): not allowed: sudo",
                    "The approval dialog failed (write_file): dialog crashed",
                    "Claude refused the conversation, so Argus started a fresh one: Claude API error 400",
                    "(note) The quick model passed a request up to claude-sonnet-5-5",
                    "(note) The route tool took 7.2 s"]
    assert len(diagnose.problems(diagnose.read_entries(tmp_path / "audit.jsonl"), notes=False)) == 6
    assert diagnose.read_entries(tmp_path / "missing") == [] and diagnose.log_trouble(tmp_path / "missing") == []


def test_a_failing_tool_is_logged_by_the_agent(tmp_path):
    agent, _ = make_agent(tmp_path, Script(call("read_file", {"path": str(tmp_path / "nope.txt")}), say("It isn't there.")))
    agent.run(session_id="s", channel="voice", content="open the file nope", events=Events())
    found = diagnose.problems(diagnose.read_entries(tmp_path / "audit.jsonl"))
    assert len(found) == 1 and "The read_file tool failed" in found[0]


def test_keys_emails_and_the_home_folder_are_removed():
    text = diagnose.redact("key sk-ant-api03-abcdefgh12345678 and AIzaSyA1234567890abcdefghijklmn, mail me@example.com, "
                           "file /Users/sandeep/.argus/x, token tt-secret-token-1, id 0123456789abcdef0123456789abcdef",
                           ["tt-secret-token-1"], home="/Users/sandeep")
    assert text == ("key [key removed] and [key removed], mail [email removed], file ~/.argus/x, token [key removed], "
                    "id [key removed]")


def write_logs(home):
    logs = home / "logs"
    logs.mkdir(parents=True)
    audit = Audit(logs / "audit.jsonl")
    audit.log("request", text="my private question")
    audit.log("turn", model="claude-haiku-5-5", error="Claude API error 529: Overloaded")
    audit.log("turn", model="claude-sonnet-5-5")
    audit.log("timing", first_word_s=1.4, speech_to_text_s=0.3, claude_s=[1.1], tools=[["weather", 0.4]])
    (logs / "argus.log").write_text(
        "2026-11-11 18:00:00 INFO argus.app started\n"
        "2026-11-11 18:01:00 WARNING argus.weather weather warnings unavailable\n"
        "2026-11-11 18:02:00 ERROR argus.brain request failed for sandeep@example.com\n"
        "Traceback (most recent call last):\n  File \"/x/brain.py\", line 1, in f\n    boom()\n"
        "RuntimeError: sk-ant-api03-SECRETSECRET12345\n"
        "2026-11-11 18:03:00 INFO argus.app fine again\n")
    (logs / "launchctl.err").write_text("Bootstrap failed: 5: Input/output error\n")
    (home / "secrets.env").write_text("ANTHROPIC_API_KEY=sk-ant-test-key-000\nTOMTOM_API_KEY=tomtomkey123\n")
    (home / "secrets.env").chmod(0o600)


def test_the_report_has_what_is_needed_and_nothing_private(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    write_logs(home)
    monkeypatch.setenv("ARGUS_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["report", "--show"]) == 0
    text = capsys.readouterr().out
    for expected in ("Argus 2.7.0 on ", "models: everyday claude-haiku-5-5, main claude-sonnet-5-5, heavy claude-opus-5-5",
                     "keys saved: ANTHROPIC_API_KEY, TOMTOM_API_KEY", "Requests in the log: 2, failed: 1",
                     "claude-haiku-5-5 1, claude-sonnet-5-5 1", "A request failed: Claude API error 529: Overloaded",
                     "first word 1.4", "WARNING argus.weather", "RuntimeError: [key removed]", "boom()",
                     "Bootstrap failed: 5", "request failed for [email removed]"):
        assert expected in text, expected
    for private in ("my private question", "sk-ant", "tomtomkey123", "sandeep@example.com", "fine again", "started"):
        assert private not in text, private
    (tmp_path / "Desktop").mkdir()
    assert main(["report"]) == 0
    path = tmp_path / "Desktop" / "argus-report.txt"
    assert path.read_text() .startswith("Argus report") and oct(path.stat().st_mode & 0o777) == "0o600"
    assert "Wrote" in capsys.readouterr().out
    (tmp_path / "Desktop").rename(tmp_path / "Elsewhere")
    assert diagnose.write_report("x").parent == tmp_path


def test_argusctl_errors_lists_and_watches(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    write_logs(home)
    monkeypatch.setenv("ARGUS_HOME", str(home))
    assert main(["errors"]) == 0
    out = capsys.readouterr().out
    assert "A request failed: Claude API error 529: Overloaded" in out and "argusctl errors --watch" in out
    (home / "logs" / "audit.jsonl").write_text("")
    assert main(["errors", "3"]) == 0 and "No problems in the activity log." in capsys.readouterr().out

    seen, stop = [], threading.Event()

    def watch():
        for line in diagnose.follow(home / "logs" / "audit.jsonl", poll=0.02, stop=stop.is_set):
            seen.append(line)
    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    time.sleep(0.1)
    audit = Audit(home / "logs" / "audit.jsonl")
    audit.log("turn", model="m")
    audit.log("tool_failed", tool="route", error="TomTom answered HTTP 500")
    deadline = time.time() + 3
    while not seen and time.time() < deadline:
        time.sleep(0.02)
    (home / "logs" / "audit.jsonl").write_text(json.dumps({"event": "llm_error", "error": "after rotation"}) + "\nbad\n")
    while len(seen) < 2 and time.time() < deadline:
        time.sleep(0.02)
    stop.set()
    thread.join(2)
    assert "The route tool failed: TomTom answered HTTP 500" in seen[0] and "after rotation" in seen[1]


def test_watching_stops_on_ctrl_c(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    write_logs(home)
    monkeypatch.setenv("ARGUS_HOME", str(home))

    def interrupted(path):
        yield "12:00  a new problem"
        raise KeyboardInterrupt
    monkeypatch.setattr(diagnose, "follow", interrupted)
    assert main(["errors", "--watch"]) == 0
    out = capsys.readouterr().out
    assert "Watching for new problems" in out and "a new problem" in out
