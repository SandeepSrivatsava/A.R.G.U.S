"""Argus looking after itself: reading its own logs, and fixing its own code.

own_logs (read-only): what went wrong lately, when Argus started and stopped, the last crash, and the end of
its log files. Keys, emails and the home folder are taken out, the same as in `argusctl report`. Its keys
(~/.argus/secrets.env), database and voiceprint stay out of reach: this tool only opens the log files named
in LOG_FILES.

code_fix: Argus changes its own code to fix a problem it found, the way a careful developer would:
  1. start   a fresh copy of the code (a git worktree) on its own branch, argus-fix/<date>-<what>. The code
             Argus is running from (~/argus) isn't touched.
  2. read / search / edit   files in that copy. The safety code can't be edited: the rules for what needs your
             OK (policy, approvals, protected places, guard, the activity log, this file, the tool runner), the
             setup scripts and the macOS app.
  3. test    runs the tests in the copy, inside the same sandbox as Argus's commands (no keys, no secrets in
             the environment). Tests that already failed before the change don't count against it.
  4. submit  needs your OK (code word or Touch ID): it refuses a change that contains a key, an email address or
             a phone number, commits it to the branch, pushes the branch to GitHub (not main) and tells you.
  5. merge   needs Touch ID, every time: the fix goes into main, is pushed, and Argus restarts with it.
             `argusctl fixes` lists fixes and `argusctl fixes merge` merges one from Terminal.
Nothing Argus writes runs for real until you merge it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

LOG_FILES = ("argus.log", "crash.log", "calls.log", "voice.err.log", "voice.out.log", "threads.log")

# Code Argus may change, and code it never may.
EDITABLE = ("argus/", "tests/", "docs/", "README.md")
LOCKED = ("argus/policy.py", "argus/approvals.py", "argus/approve.applescript", "argus/protected.py",
          "argus/guard.py", "argus/audit.py", "argus/selfcare.py", "argus/toolbox.py", "tests/conftest.py")
BRANCH_PREFIX = "argus-fix/"
MAX_READ = 400            # lines per read
TEST_TIMEOUT = 900

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<![\w.])\+?1?[ (.-]*\d{3}[ ).-]*\d{3}[ .-]*\d{4}(?!\d)")
_KEYLIKE = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xai-[A-Za-z0-9]{20,})")


# ---- own_logs ------------------------------------------------------------------------------------------------
LOGS_DESCRIPTION = (
    "Read your own (Argus's) logs, read-only, with keys and emails removed. what: 'problems' (recent errors and "
    "failures in plain words), 'starts' (when you started and stopped, and whether it was a crash), 'crash' (the last "
    "crash), or a file: " + ", ".join(LOG_FILES) + " (its last lines; 'lines' up to 300). Use this first when the "
    "user asks what's wrong with you, why something failed, or about restarts: real evidence, not memory.")
LOGS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["what"], "properties": {
    "what": {"type": "string", "enum": ["problems", "starts", "crash", *LOG_FILES]},
    "lines": {"type": "integer", "minimum": 1, "maximum": 300}}}


def attach_logs(settings, tools: Dict[str, Any], secrets: Sequence[str] = ()) -> None:
    from . import diagnose
    from .toolbox import Tool, ToolOutput
    folder = Path(settings.audit_path).parent

    def run(args: Dict[str, Any], ctx) -> ToolOutput:
        what = str(args.get("what") or "")
        n = int(args.get("lines") or 60) if isinstance(args.get("lines"), int) else 60
        n = max(1, min(300, n))
        if what == "problems":
            lines = diagnose.problems(diagnose.read_entries(settings.audit_path))[-n:] or ["No problems in the activity log."]
        elif what == "starts":
            lines = diagnose.starts_and_stops(diagnose.read_entries(settings.audit_path), last=min(n, 40))
        elif what == "crash":
            lines = diagnose.last_crash(folder / "crash.log") or ["No crash recorded."]
        elif what in LOG_FILES:
            lines = diagnose.tail(folder / what, n) or [f"{what} is empty or doesn't exist yet."]
        else:
            return ToolOutput.error("what must be problems, starts, crash or one of " + ", ".join(LOG_FILES))
        return ToolOutput(diagnose.redact("\n".join(lines), list(secrets)))

    tools["own_logs"] = Tool("own_logs", LOGS_DESCRIPTION, LOGS_SCHEMA, run,
                             frozenset({"voice", "phone", "cli", "android"}))


# ---- code_fix ------------------------------------------------------------------------------------------------
class FixError(Exception):
    pass


def slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", str(text).lower())[:6]
    return "-".join(words) or "fix"


def editable(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or ".." in rel.split("/") or "/." in "/" + rel:
        return False
    if rel in LOCKED:
        return False
    return any(rel == e or (e.endswith("/") and rel.startswith(e)) for e in EDITABLE)


def leaks(text: str, secrets: Sequence[str] = ()) -> List[str]:
    """What in an added line could be personal data or a key (a fix must never publish those)."""
    found = []
    for value in secrets:
        if value and len(value) >= 8 and value in text:
            found.append("a key")
    if _KEYLIKE.search(text):
        found.append("something that looks like a key")
    for match in _EMAIL.findall(text):
        if not match.endswith(("example.com", "example.org", "users.noreply.github.com", "anthropic.com")):
            found.append(f"an email address ({match[:3]}…)")
    for match in _PHONE.findall(text):
        digits = re.sub(r"\D", "", match)
        if "555" not in digits:            # 555 numbers are the ones tests use
            found.append("a phone number")
    return sorted(set(found))


class SelfFix:
    """One fix at a time, in its own git worktree."""

    def __init__(self, repo: Path, root: Path, *, python: str = sys.executable, secrets: Sequence[str] = (),
                 run: Callable[..., Any] = subprocess.run, sandbox: Callable[[], List[str]] = lambda: [],
                 env: Callable[[], Dict[str, str]] = lambda: dict(os.environ), audit=None,
                 notify: Callable[[str], None] = lambda _text: None, clock: Callable[[], float] = time.time):
        self.repo, self.root = Path(repo), Path(root)
        self.python, self.secrets, self.run, self.sandbox, self.env = python, list(secrets), run, sandbox, env
        self.audit, self.notify, self.clock = audit, notify, clock
        self.state_path = self.root / "current.json"

    # -- state --
    def current(self) -> Optional[Dict[str, Any]]:
        try:
            state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return None
        return state if Path(state.get("dir", "")).is_dir() else None

    def _save(self, state: Optional[Dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if state is None:
            try:
                self.state_path.unlink()
            except OSError:
                pass
            return
        self.state_path.write_text(json.dumps(state))

    def _git(self, *args: str, cwd: Optional[Path] = None, check: bool = True, timeout: int = 120) -> str:
        proc = self.run(["git", "-C", str(cwd or self.repo), *args], capture_output=True, text=True, timeout=timeout)
        if check and proc.returncode != 0:
            raise FixError(f"git {args[0]} failed: {(proc.stderr or proc.stdout or '').strip()[:300]}")
        return (proc.stdout or "").strip()

    def _need(self) -> Dict[str, Any]:
        state = self.current()
        if state is None:
            raise FixError("No fix is open. Start one first (action 'start' with a title).")
        return state

    def _file(self, state: Dict[str, Any], rel: str) -> Path:
        rel = str(rel or "").strip()
        base = Path(state["dir"]).resolve()
        path = (base / rel).resolve()
        if base not in path.parents and path != base:
            raise FixError("that path is outside Argus's code")
        if path != base and ".git" in path.relative_to(base).parts:
            raise FixError("the git folder isn't part of the code")
        return path

    # -- actions --
    def start(self, title: str) -> str:
        if self.current() is not None:
            state = self.current()
            return (f"A fix is already open: {state['title']} (branch {state['branch']}). Keep working on it, or "
                    "'abandon' it first.")
        if not (self.repo / ".git").exists():
            raise FixError(f"Argus's code at {self.repo} isn't a git repository, so it can't be fixed safely here.")
        stamp = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M")
        branch = f"{BRANCH_PREFIX}{stamp}-{slug(title)}"
        folder = self.root / branch.replace("/", "-")
        self.root.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "-b", branch, str(folder), "HEAD")
        self._save({"title": " ".join(str(title).split())[:120], "branch": branch, "dir": str(folder),
                    "base": self._git("rev-parse", "HEAD"), "tested": False, "edits": 0})
        if self.audit:
            self.audit.log("selffix_started", branch=branch)
        return (f"Started {branch}: a separate copy of your code in {folder}. Read and search it, change files with "
                "edit, then run test. The code you're running from isn't touched.")

    def read(self, path: str, start: int = 1, lines: int = 200) -> str:
        state = self._need()
        target = self._file(state, path)
        if target.is_dir():
            names = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir() if p.name != ".git")
            return "\n".join(names[:300])
        try:
            text = target.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise FixError(f"can't read {path}: {exc}")
        start = max(1, int(start or 1))
        lines = max(1, min(MAX_READ, int(lines or 200)))
        chunk = text[start - 1:start - 1 + lines]
        more = f"\n… ({len(text)} lines in all)" if start - 1 + lines < len(text) else ""
        return "\n".join(f"{i:5d}  {line}" for i, line in enumerate(chunk, start)) + more

    def search(self, pattern: str) -> str:
        state = self._need()
        if not str(pattern or "").strip():
            raise FixError("say what to search for")
        out = self._git("grep", "-n", "-I", "-E", "--", str(pattern), cwd=Path(state["dir"]), check=False)
        lines = out.splitlines()
        return "\n".join(line[:240] for line in lines[:80]) + (f"\n… {len(lines) - 80} more" if len(lines) > 80 else "") \
            if lines else "No matches."

    def edit(self, path: str, old: str, new: str) -> str:
        state = self._need()
        rel = str(path or "").strip()
        if not editable(rel):
            raise FixError(f"{rel} can't be changed by a self-fix (only argus/, tests/, docs/ and README.md, and "
                           "never the safety code: " + ", ".join(LOCKED) + ")")
        target = self._file(state, rel)
        if old == "":
            if target.exists():
                raise FixError(f"{rel} exists: give the exact text to replace in 'old'")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new, encoding="utf-8")
        else:
            try:
                text = target.read_text(encoding="utf-8")
            except OSError as exc:
                raise FixError(f"can't read {rel}: {exc}")
            count = text.count(old)
            if count != 1:
                raise FixError(f"'old' must appear exactly once in {rel} (found {count}); read the file and copy the "
                               "exact text")
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
        state["tested"] = False
        state["edits"] = int(state.get("edits", 0)) + 1
        self._save(state)
        return f"Changed {rel}. Run test before submitting."

    def _pytest(self, cwd: Path, targets: Sequence[str] = ()) -> subprocess.CompletedProcess:
        # without -I: the copy's own folder comes first on the path, so its argus is the one tested
        cmd = self.sandbox() + [self.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rf", *targets]
        try:
            return self.run(cmd, cwd=str(cwd), env=self.env(), capture_output=True, text=True, timeout=TEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise FixError(f"the tests took longer than {TEST_TIMEOUT // 60} minutes")

    @staticmethod
    def _failed(output: str) -> List[str]:
        return sorted(set(re.findall(r"^(?:FAILED|ERROR) (\S+)", output or "", re.M)))

    def test(self) -> str:
        state = self._need()
        proc = self._pytest(Path(state["dir"]))
        out = (proc.stdout or "") + (proc.stderr or "")
        if "No module named pytest" in out:
            raise FixError("pytest isn't installed for Argus yet: ask the user to run scripts/update.sh once in Terminal")
        failed = self._failed(out)
        summary = next((line for line in reversed(out.splitlines()) if re.search(r"\d+ (passed|failed)", line)), "")
        if proc.returncode == 0:
            state["tested"] = True
            self._save(state)
            return f"All tests pass ({summary.strip('= ')}). You can submit."
        if not failed:
            return "The tests couldn't run:\n" + "\n".join(out.splitlines()[-25:])
        # tests that fail on the unchanged code too aren't this fix's fault
        before = self._failed(((lambda p: (p.stdout or "") + (p.stderr or ""))(self._pytest(self.repo, failed))))
        new = [t for t in failed if t not in before]
        if not new:
            state["tested"] = True
            self._save(state)
            return (f"No new failures: {len(failed)} test(s) fail on the unchanged code too ({', '.join(failed[:5])}). "
                    "You can submit.")
        detail = "\n".join(out.splitlines()[-40:])
        return f"New failures from this change: {', '.join(new[:10])}\n{detail}"

    def diff(self) -> str:
        state = self._need()
        self._git("add", "-A", cwd=Path(state["dir"]))
        out = self._git("diff", "--cached", "--stat", "-p", cwd=Path(state["dir"]))
        return out[:12_000] or "No changes yet."

    def check_submit(self) -> str:
        """Why it can't be submitted yet ("" if it can)."""
        state = self._need()
        if not state.get("edits"):
            return "Nothing has been changed yet."
        if not state.get("tested"):
            return "Run test after the last change first."
        self._git("add", "-A", cwd=Path(state["dir"]))
        changed = self._git("diff", "--cached", "--name-only", cwd=Path(state["dir"])).splitlines()
        bad = [f for f in changed if not editable(f)]
        if bad:
            return "These files can't be part of a self-fix: " + ", ".join(bad)
        added = "\n".join(line[1:] for line in self._git("diff", "--cached", "-U0", cwd=Path(state["dir"])).splitlines()
                          if line.startswith("+") and not line.startswith("+++"))
        found = leaks(added, self.secrets)
        if found:
            return "The change contains " + ", ".join(found) + "; take it out (a fix gets published on GitHub)."
        return ""

    def submit(self, summary: str) -> str:
        state = self._need()
        why = self.check_submit()
        if why:
            raise FixError(why)
        folder = Path(state["dir"])
        message = (f"Argus fix: {state['title']}\n\n{' '.join(str(summary).split())[:1500]}\n\n"
                   "Written and tested by Argus (code_fix); merged only with the owner's Touch ID.")
        self._git("commit", "-q", "-m", message, cwd=folder)
        commit = self._git("rev-parse", "--short", "HEAD", cwd=folder)
        pushed = self.run(["git", "-C", str(folder), "push", "-q", "-u", "origin", state["branch"]],
                          capture_output=True, text=True, timeout=120).returncode == 0
        # the branch keeps the fix; the working copy isn't needed any more, and the next fix can start
        self._git("worktree", "remove", "--force", state["dir"], check=False)
        self._save(None)
        where = f"pushed to GitHub as branch {state['branch']}" if pushed else \
            f"kept on this Mac as branch {state['branch']} (pushing to GitHub didn't work)"
        note = (f"Argus fixed: {state['title']}. Tests pass; {where}. To use it, say \"merge the fix\" (Touch ID) or "
                "run argusctl fixes merge.")
        if self.audit:
            self.audit.log("selffix_submitted", branch=state["branch"], commit=commit, pushed=pushed)
        self.notify(note)
        return note

    def abandon(self) -> str:
        state = self._need()
        self._git("worktree", "remove", "--force", state["dir"], check=False)
        if not state.get("submitted"):
            self._git("branch", "-D", state["branch"], check=False)
        self._save(None)
        return f"Dropped {state['branch']}."

    def merge(self, branch: str = "") -> str:
        """Put a submitted fix into main and push it (the caller has already had Touch ID)."""
        state = self.current()
        if not branch:
            branches = self.fixes()
            if not branches:
                raise FixError("There's no fix to merge.")
            branch = branches[-1]
        if not branch.startswith(BRANCH_PREFIX):
            raise FixError("only argus-fix/ branches can be merged this way")
        if state and state.get("branch") == branch:
            raise FixError("That fix hasn't been submitted (tested and committed) yet.")
        if self._git("status", "--porcelain", "--untracked-files=no"):
            raise FixError(f"{self.repo} has uncommitted changes; merge from Terminal after sorting those out")
        current = self._git("rev-parse", "--abbrev-ref", "HEAD")
        if current != "main":
            raise FixError(f"{self.repo} is on {current}, not main")
        self._git("merge", "--no-edit", "-m", f"Merge {branch}", branch, timeout=60)
        pushed = self.run(["git", "-C", str(self.repo), "push", "-q", "origin", "main"],
                          capture_output=True, text=True, timeout=120).returncode == 0
        if self.audit:
            self.audit.log("selffix_merged", branch=branch, pushed=pushed)
        return (f"Merged {branch} into main" + (" and pushed it to GitHub" if pushed else
                                                 " (pushing to GitHub didn't work; run git push in ~/argus)")
                + ". Argus needs a restart to use it.")

    def fixes(self) -> List[str]:
        out = self._git("branch", "--list", BRANCH_PREFIX + "*", "--format=%(refname:short)", check=False)
        merged = set(self._git("branch", "--merged", "main", "--format=%(refname:short)", check=False).splitlines())
        return [b for b in out.splitlines() if b and b not in merged]


FIX_DESCRIPTION = (
    "Fix your own (Argus's) code when your logs or the user show a bug in it. Work like a careful developer: "
    "'start' (title) opens a separate copy on its own branch; 'search' (pattern) and 'read' (path, start, lines) "
    "to find the cause; 'edit' (path, old, new: exact text, once; old '' creates a file) for the smallest change "
    "that fixes it, plus a test for it in tests/; 'test' runs the tests; 'diff' shows the change; 'submit' "
    "(summary) commits it and pushes the branch to GitHub after the user's OK; 'merge' puts it into main (Touch "
    "ID) and you restart; 'abandon' drops it; 'status'. You can't change the safety code (approvals, policy, "
    "protected places, guard, the activity log, the tool runner) or setup scripts. Say what you're fixing first; "
    "tell the user the result. Some fixes aren't code (a setting, a permission, the network): say so instead.")
FIX_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["action"], "properties": {
    "action": {"type": "string", "enum": ["start", "search", "read", "edit", "test", "diff", "submit", "merge",
                                          "abandon", "status"]},
    "title": {"type": "string"}, "pattern": {"type": "string"}, "path": {"type": "string"},
    "start": {"type": "integer", "minimum": 1}, "lines": {"type": "integer", "minimum": 1, "maximum": MAX_READ},
    "old": {"type": "string"}, "new": {"type": "string"}, "summary": {"type": "string"},
    "branch": {"type": "string"}}}


def test_sandbox(home: Path, python: str) -> List[str]:
    """The sandbox Argus's commands run in (no keys, ~/.argus out of reach), except that the tests may read
    Argus's own Python (it lives in ~/.argus/venv)."""
    from .protected import sandbox_profile
    from .toolbox import SANDBOX_EXEC
    if not SANDBOX_EXEC or not os.path.exists(SANDBOX_EXEC):
        return []
    venv = Path(python).parent.parent
    profile = sandbox_profile(Path(home)) + f'(allow file-read* (subpath "{venv}"))\n'
    return [SANDBOX_EXEC, "-p", profile]


def attach_fix(fixer: SelfFix, tools: Dict[str, Any], *, restart: Callable[[], None] = lambda: None) -> None:
    from .toolbox import Tool, ToolOutput

    def run(args: Dict[str, Any], ctx) -> ToolOutput:
        action = args.get("action")
        try:
            if action == "start":
                return ToolOutput(fixer.start(args.get("title") or "fix"))
            if action == "search":
                return ToolOutput(fixer.search(args.get("pattern") or ""))
            if action == "read":
                return ToolOutput(fixer.read(args.get("path") or "", args.get("start") or 1, args.get("lines") or 200))
            if action == "edit":
                if not isinstance(args.get("old"), str) or not isinstance(args.get("new"), str):
                    return ToolOutput.error("edit needs path, old and new")
                return ToolOutput(fixer.edit(args.get("path") or "", args["old"], args["new"]))
            if action == "test":
                return ToolOutput(fixer.test())
            if action == "diff":
                return ToolOutput(fixer.diff())
            if action == "submit":
                return ToolOutput(fixer.submit(args.get("summary") or ""))
            if action == "merge":
                text = fixer.merge(str(args.get("branch") or ""))
                restart()
                return ToolOutput(text + " Restarting now.")
            if action == "abandon":
                return ToolOutput(fixer.abandon())
            if action == "status":
                state = fixer.current()
                open_ = (f"Open: {state['title']} ({state['branch']}), {state.get('edits', 0)} change(s), "
                         f"{'tested' if state.get('tested') else 'not tested yet'}.") if state else "No fix is open."
                waiting = fixer.fixes()
                return ToolOutput(open_ + (" Not merged yet: " + ", ".join(waiting) if waiting else ""))
        except FixError as exc:
            return ToolOutput.error(str(exc))
        return ToolOutput.error("unknown action")

    tools["code_fix"] = Tool("code_fix", FIX_DESCRIPTION, FIX_SCHEMA, run, frozenset({"voice", "phone", "cli"}))
