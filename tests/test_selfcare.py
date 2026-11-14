"""Argus reading its own logs and fixing its own code (selfcare.py)."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus import selfcare
from argus.approvals import ApprovalRequest
from argus.policy import ALLOW, ASK, BLOCK, Policy
from fakes import make_settings


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "argus-src"
    (repo / "argus").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "argus" / "__init__.py").write_text("")
    (repo / "argus" / "sums.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "argus" / "policy.py").write_text("RULES = 1\n")
    (repo / "tests" / "test_sums.py").write_text("from argus.sums import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "owner@users.noreply.github.com")
    git(repo, "config", "user.name", "Owner")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "start")
    return repo


def fixer_for(repo, tmp_path, **kw):
    notes = []
    fixer = selfcare.SelfFix(repo, tmp_path / "fixes", python=sys.executable, notify=notes.append,
                             clock=lambda: 1_763_100_000, **kw)
    return fixer, notes


def test_a_fix_from_start_to_merge(repo, tmp_path):
    fixer, notes = fixer_for(repo, tmp_path, secrets=["supersecretkey123"])
    with pytest.raises(selfcare.FixError):
        fixer.read("argus/sums.py")                               # nothing open yet
    started = fixer.start("Adding numbers is wrong")
    assert "argus-fix/" in started and "adding-numbers-is-wrong" in started
    assert "already open" in fixer.start("again")
    state = fixer.current()
    assert Path(state["dir"]).is_dir() and git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert "return a - b" in fixer.read("argus/sums.py")
    assert "argus/" in fixer.read("")                              # a folder lists its contents
    assert "sums.py:2" in fixer.search("a - b")
    assert fixer.search("nothing-like-this") == "No matches."
    for bad in ("argus/policy.py", "setup.sh", "../outside.py", "/etc/passwd", ".git/config"):
        with pytest.raises(selfcare.FixError):
            fixer.edit(bad, "", "x")
    with pytest.raises(selfcare.FixError):
        fixer.edit("argus/sums.py", "not there", "x")
    with pytest.raises(selfcare.FixError):
        fixer.read("../../etc/passwd")
    assert "Run test" in fixer.check_submit() or "Nothing" in fixer.check_submit()
    fixer.edit("argus/sums.py", "a - b", "a + b")
    assert "Run test" in fixer.check_submit()
    assert "All tests pass" in fixer.test()
    fixer.edit("tests/test_more.py", "", "def test_more():\n    assert True\n  # call 510-656-9535\n")
    fixer.test()
    assert "phone number" in fixer.check_submit()
    with pytest.raises(selfcare.FixError):
        fixer.submit("x")
    fixer.edit("tests/test_more.py", "  # call 510-656-9535\n", "  # key supersecretkey123\n")
    fixer.test()
    assert "a key" in fixer.check_submit()
    fixer.edit("tests/test_more.py", "  # key supersecretkey123\n", "")
    fixer.test()
    assert "+    return a + b" in fixer.diff()
    note = fixer.submit("add used minus")
    assert "kept on this Mac" in note and notes == [note]          # no GitHub remote here
    assert fixer.fixes() == [state["branch"]] and fixer.current() is None and not Path(state["dir"]).exists()
    message = git(repo, "log", "-1", "--format=%B", state["branch"])
    assert message.startswith("Argus fix: Adding numbers is wrong") and "Touch ID" in message
    merged = fixer.merge()
    assert "Merged" in merged and "didn't work" in merged
    assert "a + b" in (repo / "argus" / "sums.py").read_text()
    assert fixer.fixes() == []
    with pytest.raises(selfcare.FixError):
        fixer.merge()


def test_new_failures_count_and_old_ones_dont(repo, tmp_path):
    (repo / "tests" / "test_old.py").write_text("def test_already_broken():\n    assert False\n")
    (repo / "tests" / "test_zero.py").write_text("from argus.sums import add\n\n\ndef test_zero():\n    assert add(1, 0) == 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "broken test")
    fixer, _ = fixer_for(repo, tmp_path)
    fixer.start("fix add")
    fixer.edit("argus/sums.py", "a - b", "a + b")
    assert "No new failures" in fixer.test() and "test_already_broken" in fixer.test()
    fixer.edit("argus/sums.py", "a + b", "a + b + 100")
    out = fixer.test()
    assert out.startswith("New failures from this change") and "test_zero" in out
    assert "Run test" in fixer.check_submit()
    assert "Dropped" in fixer.abandon() and fixer.current() is None
    assert fixer.fixes() == []


def test_merge_refuses_unless_main_is_clean_and_submitted(repo, tmp_path):
    fixer, _ = fixer_for(repo, tmp_path)
    fixer.start("x")
    fixer.edit("argus/sums.py", "a - b", "a + b")
    with pytest.raises(selfcare.FixError, match="no fix to merge"):
        fixer.merge()
    with pytest.raises(selfcare.FixError, match="submitted"):
        fixer.merge(fixer.current()["branch"])
    with pytest.raises(selfcare.FixError, match="only argus-fix"):
        fixer.merge("main")
    fixer.test()
    fixer.submit("x")
    with pytest.raises(selfcare.FixError, match="only argus-fix"):
        fixer.merge("main")
    (repo / "argus" / "sums.py").write_text("changed by hand\n")
    with pytest.raises(selfcare.FixError, match="uncommitted"):
        fixer.merge()
    git(repo, "checkout", "-q", "--", ".")
    git(repo, "checkout", "-q", "-b", "other")
    with pytest.raises(selfcare.FixError, match="not main"):
        fixer.merge()


def test_tests_that_cant_run_and_missing_pytest(repo, tmp_path):
    outputs = iter([SimpleNamespace(returncode=1, stdout="/x/python: No module named pytest", stderr=""),
                    SimpleNamespace(returncode=2, stdout="ImportError while loading conftest", stderr="")])
    real = subprocess.run

    def run(cmd, **kw):
        return next(outputs) if "pytest" in cmd else real(cmd, **kw)
    fixer, _ = fixer_for(repo, tmp_path, run=run)
    fixer.start("x")
    with pytest.raises(selfcare.FixError, match="update.sh"):
        fixer.test()
    assert fixer.test().startswith("The tests couldn't run")

    def slow(cmd, **kw):
        if "pytest" in cmd:
            raise subprocess.TimeoutExpired(cmd, 1)
        return real(cmd, **kw)
    fixer.run = slow
    with pytest.raises(selfcare.FixError, match="longer than"):
        fixer.test()


def test_not_a_git_repo_and_broken_state(tmp_path):
    fixer, _ = fixer_for(tmp_path / "nope", tmp_path)
    with pytest.raises(selfcare.FixError, match="isn't a git repository"):
        fixer.start("x")
    (tmp_path / "fixes").mkdir()
    (tmp_path / "fixes" / "current.json").write_text("not json")
    assert fixer.current() is None
    fixer._save(None)
    fixer._save(None)                                # nothing to remove: fine


def test_leaks_and_editable():
    assert selfcare.leaks("x = 'me@gmail.com'") == ["an email address (me@…)"]
    assert selfcare.leaks("ok@example.com 415-555-0123 bot@users.noreply.github.com") == []
    assert selfcare.leaks("AIza" + "x" * 30) == ["something that looks like a key"]
    assert selfcare.editable("argus/daemon.py") and selfcare.editable("README.md")
    assert not selfcare.editable("argus/approvals.py") and not selfcare.editable("scripts/setup_calls.sh")
    assert selfcare.slug("!!!") == "fix"


def test_the_code_fix_tool_and_its_approvals(repo, tmp_path):
    fixer, _ = fixer_for(repo, tmp_path)
    tools, restarted = {}, []
    selfcare.attach_fix(fixer, tools, restart=lambda: restarted.append(1))
    tool = tools["code_fix"]
    assert tool.channels == frozenset({"voice", "phone", "cli"})
    assert "No fix is open" in tool.run({"action": "status"}, None).content
    assert tool.run({"action": "read", "path": "x"}, None).is_error
    assert "Started" in tool.run({"action": "start", "title": "add"}, None).content
    assert "argus/" in tool.run({"action": "search", "pattern": "def"}, None).content
    assert "a - b" in tool.run({"action": "read", "path": "argus/sums.py"}, None).content
    assert tool.run({"action": "edit", "path": "argus/sums.py"}, None).is_error
    assert not tool.run({"action": "edit", "path": "argus/sums.py", "old": "a - b", "new": "a + b"}, None).is_error
    assert "not tested yet" in tool.run({"action": "status"}, None).content
    assert "pass" in tool.run({"action": "test"}, None).content
    assert "a + b" in tool.run({"action": "diff"}, None).content
    assert "Argus fixed" in tool.run({"action": "submit", "summary": "plus"}, None).content
    assert "No fix is open. Not merged yet" in tool.run({"action": "status"}, None).content
    assert "Restarting now" in tool.run({"action": "merge"}, None).content and restarted == [1]
    assert tool.run({"action": "abandon"}, None).is_error
    assert tool.run({"action": "dance"}, None).is_error

    s = make_settings(tmp_path)
    policy = Policy(s, workspace=s.workspace)
    assert policy.check("code_fix", {"action": "edit"}, "voice").action == ALLOW
    submit = policy.check("code_fix", {"action": "submit"}, "voice")
    assert submit.action == ASK and not submit.touch_only
    merge = policy.check("code_fix", {"action": "merge"}, "voice")
    assert merge.action == ASK and merge.touch_only
    assert policy.check("code_fix", {"action": "start"}, "scheduler").action == BLOCK
    request = ApprovalRequest("r", description=merge.reason, short=merge.short, touch_only=True)
    assert request.spoken() == "To merge my fix into my code, I need your Touch ID. Please check the screen."


def test_touch_only_is_never_approved_by_voice():
    from argus.approvals import ONCE, MacApprover, VoiceGate, VoiceOrScreenApprover
    asked = []
    screen = MacApprover(popen=lambda *a, **k: SimpleNamespace(poll=lambda: 0, stdout=SimpleNamespace(read=lambda: "Allow once"),
                                                               terminate=lambda: None),
                         require_owner_auth=False, auth=lambda reason, cancel=None: asked.append(reason) or True)
    approver = VoiceOrScreenApprover(screen, VoiceGate(pick=lambda w: "owl"), voice_ok=lambda: True)
    request = ApprovalRequest("r", description="merge", touch_only=True, origin="voice")
    approver.prepare(request)
    assert request.code == "" and "changes Argus's own code" in screen.message(request)
    assert approver.decide(request) == ONCE and asked                       # Touch ID was asked for


def test_own_logs(tmp_path):
    s = make_settings(tmp_path)
    folder = Path(s.audit_path).parent
    folder.mkdir(parents=True, exist_ok=True)
    Path(s.audit_path).write_text("\n".join(json.dumps(e) for e in [
        {"ts": "2026-11-13T21:00:00-0800", "event": "started", "name": "Argus", "version": "2.10.6", "pid": 1},
        {"ts": "2026-11-13T21:05:00-0800", "event": "tool_failed", "tool": "phone_call", "error": "BlackHole missing"},
    ]) + "\n")
    (folder / "argus.log").write_text("line one\nkey sk-ant-abcdefghijklmnopqrstuv here\nmail me@gmail.com\n")
    (folder / "crash.log").write_text("--- Argus started, pid 1 ---\nFatal Python error: Segmentation fault\n  File x\n")
    tools = {}
    selfcare.attach_logs(s, tools, secrets=["sk-ant-abcdefghijklmnopqrstuv"])
    run = lambda **a: tools["own_logs"].run(a, None)
    assert "phone_call" in run(what="problems").content or "BlackHole" in run(what="problems").content
    assert "2.10.6" in run(what="starts").content or "start" in run(what="starts").content.lower()
    assert "Segmentation fault" in run(what="crash").content
    text = run(what="argus.log", lines=10).content
    assert "sk-ant" not in text and "me@gmail.com" not in text and "line one" in text
    assert "empty or doesn't exist" in run(what="calls.log").content
    assert run(what="secrets.env").is_error
    (folder / "crash.log").write_text("")
    assert run(what="crash").content == "No crash recorded."
    Path(s.audit_path).write_text("")
    assert run(what="problems").content == "No problems in the activity log."
    assert "none recorded" in run(what="starts").content
    assert tools["own_logs"].channels == frozenset({"voice", "phone", "cli", "android"})


def test_test_sandbox_lets_tests_read_argus_python_only(monkeypatch, tmp_path):
    from argus import toolbox
    monkeypatch.setattr(toolbox, "SANDBOX_EXEC", "")
    assert selfcare.test_sandbox(tmp_path, "/h/.argus/venv/bin/python3") == []
    exe = tmp_path / "sandbox-exec"
    exe.write_text("")
    monkeypatch.setattr(toolbox, "SANDBOX_EXEC", str(exe))
    cmd = selfcare.test_sandbox(tmp_path, "/h/.argus/venv/bin/python3")
    assert cmd[0] == str(exe) and '(allow file-read* (subpath "/h/.argus/venv"))' in cmd[2]
    assert "(deny file-read* file-write*" in cmd[2]


def test_argusctl_fixes(repo, tmp_path, capsys):
    from argus import __main__ as cli
    fixer, _ = fixer_for(repo, tmp_path)
    assert cli.cmd_fixes(SimpleNamespace(action="list", branch=""), fixer=fixer) == 0
    assert "No fixes waiting" in capsys.readouterr().out
    assert cli.cmd_fixes(SimpleNamespace(action="merge", branch=""), fixer=fixer) == 1
    fixer.start("add")
    fixer.edit("argus/sums.py", "a - b", "a + b")
    fixer.test()
    fixer.submit("plus")
    fixer.start("another")
    assert cli.cmd_fixes(SimpleNamespace(action="list", branch=""), fixer=fixer) == 0
    out = capsys.readouterr().out
    assert "Being worked on: another" in out and "Ready to merge: argus-fix/" in out
    assert cli.cmd_fixes(SimpleNamespace(action="show", branch=""), fixer=fixer) == 0
    assert "a + b" in capsys.readouterr().out
    assert cli.cmd_fixes(SimpleNamespace(action="merge", branch=""), fixer=fixer, auth=lambda r: False) == 1
    restarted = []
    assert cli.cmd_fixes(SimpleNamespace(action="merge", branch=""), fixer=fixer, auth=lambda r: True,
                         restart=lambda: restarted.append(1)) == 0
    assert restarted == [1] and "a + b" in (repo / "argus" / "sums.py").read_text()
    assert cli.cmd_fixes(SimpleNamespace(action="merge", branch="main"), fixer=fixer, auth=lambda r: True) == 1
    assert cli.cmd_fixes(SimpleNamespace(action="dance", branch="x"), fixer=fixer) == 2
