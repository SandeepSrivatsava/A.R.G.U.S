"""Big jobs divided into tasks for background workers (crew.py), and handing jobs to Paperclip (paperclip.py)."""
import io
import json
import threading
import urllib.error
from types import SimpleNamespace

import pytest

from argus import crew as crew_mod
from argus import paperclip as pc
from argus.store import Store
from fakes import make_settings

PLAN = [{"key": "hotels", "title": "Find hotels near Midtown under $250", "role": "researcher",
         "detail": "three options with prices"},
        {"key": "food", "title": "Pick a dinner place for each night", "role": "researcher", "detail": "near hotel"},
        {"key": "plan", "title": "Write the trip plan", "role": "writer", "detail": "one page",
         "after": ["hotels", "food"]}]


class LLM:
    def __init__(self, plan=PLAN):
        self.plan, self.bodies = plan, []

    def create(self, body):
        self.bodies.append(body)
        usage = {"input_tokens": 1000, "output_tokens": 200}
        if body.get("tools"):
            return {"content": [{"type": "tool_use", "name": "plan", "input": {"tasks": self.plan}}], "usage": usage}
        return {"content": [{"type": "text", "text": "The Pod Times Square fits. Dinners are booked-ready."}],
                "usage": usage}


class Agent:
    """A worker: answers each task from the words in its prompt."""
    def __init__(self, answers=None):
        self.prompts, self.answers, self.lock = [], answers or {}, threading.Lock()

    def run(self, *, session_id, channel, content, events):
        with self.lock:
            self.prompts.append((channel, content))
        for word, answer in self.answers.items():
            if word in content:
                if isinstance(answer, Exception):
                    raise answer
                return SimpleNamespace(text=answer, error=None, cost=0.02)
        return SimpleNamespace(text="Done: " + content.split("Your task: ")[1].split("\n")[0], error=None, cost=0.02)


def make(tmp_path, agent=None, llm=None, **kw):
    s = make_settings(tmp_path)
    said = []
    crew = crew_mod.Crew(Store(tmp_path / "a.db"), agent or Agent(), s, llm=llm or LLM(), deliver=said.append,
                         workspace=tmp_path / "ws", start_threads=False, **kw)
    return crew, said


def test_a_job_is_planned_worked_in_order_and_reported(tmp_path):
    agent = Agent()
    crew, said = make(tmp_path, agent)
    job = crew.start("Plan the New York trip: hotel, dinners", budget=2)
    assert job["status"] == "running" and [t["key"] for t in crew.tasks(job["id"])] == ["hotels", "food", "plan"]
    assert [t["key"] for t in crew.ready(job["id"])] == ["hotels", "food"]      # the plan waits for both
    crew.run_job(job["id"])
    done = crew.job(job["id"])
    assert done["status"] == "done" and 0 < done["spent"] < 2
    channels = {c for c, _ in agent.prompts}
    assert channels == {"crew"}
    writer = [p for _, p in agent.prompts if "Write the trip plan" in p][0]
    assert "The whole job (why this matters): Plan the New York trip" in writer      # goal ancestry
    assert "Done: Find hotels near Midtown under $250" in writer                      # the earlier results
    assert said and said[0].startswith(f"Crew job {job['id']} is done: The Pod Times Square fits.")
    report = (tmp_path / "ws" / "crew" / f"job-{job['id']}.md").read_text()
    assert "# Crew job" in report and "Write the trip plan (writer, done)" in report


def test_a_task_that_needs_the_owner_stops_what_waits_on_it(tmp_path):
    agent = Agent({"Pick a dinner": "NEEDS_YOU: which night do you want the steakhouse?"})
    crew, said = make(tmp_path, agent)
    job = crew.start("Plan the trip")
    crew.run_job(job["id"])
    tasks = {t["key"]: t for t in crew.tasks(job["id"])}
    assert tasks["food"]["status"] == "needs_you" and "steakhouse" in tasks["food"]["result"]
    assert tasks["plan"]["status"] == "blocked" and "Pick a dinner place" in tasks["plan"]["result"]
    assert crew.job(job["id"])["status"] == "needs you" and "needs you for a step" in said[0]


def test_failures_are_retried_once_then_reported(tmp_path):
    agent = Agent({"Find hotels": RuntimeError("network down")})
    crew, _ = make(tmp_path, agent)
    job = crew.start("Plan the trip")
    crew.run_job(job["id"])
    hotels = crew.tasks(job["id"])[0]
    assert hotels["status"] == "failed" and hotels["attempts"] == 2 and "network down" in hotels["result"]
    empty = Agent({"Find hotels": ""})
    crew2, _ = make(tmp_path / "b", empty)
    job2 = crew2.start("Plan the trip")
    crew2.run_job(job2["id"])
    assert "no answer" in crew2.tasks(job2["id"])[0]["result"]


def test_the_budget_pauses_the_job_and_carry_on_resumes(tmp_path):
    crew, said = make(tmp_path)
    job = crew.start("Plan the trip", budget=0.05)
    crew.run_job(job["id"])
    assert crew.job(job["id"])["status"] == "paused" and "used its $0.05 budget" in said[0]
    assert crew.carry_on(job["id"], 1) == f"Crew job {job['id']} is running again."
    crew.run_job(job["id"])
    assert crew.job(job["id"])["status"] == "done"
    assert "not paused" in crew.carry_on(job["id"]) and "no crew job" in crew.carry_on(999)


def test_a_task_is_only_ever_claimed_once(tmp_path):
    crew, _ = make(tmp_path)
    job = crew.start("Plan the trip")
    task = crew.tasks(job["id"])[0]
    assert crew.checkout(task["id"], "w1") and not crew.checkout(task["id"], "w2")
    crew.work(crew.job(job["id"]), dict(task))                 # a second worker finds it taken and leaves it
    assert crew.tasks(job["id"])[0]["owner"] == "w1"
    crew.resume(job["id"])                                       # after a restart it goes back to the queue
    assert crew.tasks(job["id"])[0]["status"] == "todo"


def test_cancel_list_status_and_bad_plans(tmp_path):
    crew, _ = make(tmp_path)
    job = crew.start("Plan the trip")
    assert "Cancelled" in crew.cancel(job["id"]) and "already ended" in crew.cancel(job["id"])
    crew.run_job(job["id"])                                      # cancelled: nothing runs
    assert all(t["status"] == "todo" for t in crew.tasks(job["id"]))
    assert "no crew job" in crew.cancel(42) and "no crew job" in crew.describe(42)
    assert "[cancelled]" not in crew.describe(job["id"]) and "cancelled" in crew.describe(job["id"])
    with pytest.raises(ValueError):
        crew.start("  ")
    bad, _ = make(tmp_path / "c", llm=LLM(plan=[]))
    with pytest.raises(ValueError):
        bad.start("x")
    assert bad.jobs()[0]["status"] == "failed"
    cleaned = crew_mod.clean_plan([{"key": "a b", "title": "One", "role": "wizard", "after": ["later"]},
                                   {"key": "ab", "title": "Two", "detail": "d", "role": "coder", "after": ["ab"]},
                                   {"title": ""}, "junk"])
    assert [(t["key"], t["role"], t["after"]) for t in cleaned] == [("ab", "researcher", []), ("abx", "coder", ["ab"])]


def test_threads_run_jobs_in_the_background(tmp_path):
    s = make_settings(tmp_path)
    said, finished = [], threading.Event()
    crew = crew_mod.Crew(Store(tmp_path / "a.db"), Agent(), s, llm=LLM(),
                         deliver=lambda t: (said.append(t), finished.set()), workspace=tmp_path / "ws")
    crew.start("Plan the trip")
    assert finished.wait(10) and "is done" in said[0]
    crew.resume()


def test_the_crew_tool(tmp_path):
    crew, _ = make(tmp_path)
    tools = {"fetch_page": SimpleNamespace(channels=frozenset({"voice"}), name="fetch_page")}
    from argus.toolbox import Tool
    tools["fetch_page"] = Tool("fetch_page", "x", {}, lambda a, c: None, frozenset({"voice"}))
    crew_mod.attach(crew, tools)
    assert "crew" in tools["fetch_page"].channels and "run_command" not in tools
    run = lambda **a: tools["crew"].run(a, None)
    assert run(action="list").content == "No crew jobs yet."
    out = run(action="start", goal="Plan the trip", budget_usd=3).content
    assert out.startswith("Started crew job 1 (up to $3.00) with 3 tasks: Find hotels")
    assert "Crew job 1: running" in run(action="status").content
    assert "Job 1 [running]" in run(action="list").content
    assert "Cancelled" in run(action="cancel", job=1).content
    assert "running again" in run(action="carry_on", job=1).content
    assert run(action="start", goal="").is_error and run(action="dance").is_error
    crew.llm = SimpleNamespace(create=lambda body: 1 / 0)
    assert "couldn't start" in run(action="start", goal="x").content
    assert tools["crew"].channels == frozenset({"voice", "phone", "cli", "android"})


def test_crew_workers_only_get_their_tools(tmp_path):
    from fakes import Script, make_agent
    agent, _ = make_agent(tmp_path, Script())
    crew_mod.open_to_crew(agent.tools)
    names = {t["name"] for t in agent._tools_for("crew")}
    assert {"fetch_page", "read_file", "write_file"} <= names
    assert not names & {"run_command", "phone_call", "open_app", "schedule", "remember", "crew"}
    assert "code_execution" in names


def test_argusctl_crew(tmp_path, capsys, monkeypatch):
    from argus import __main__ as cli
    crew, _ = make(tmp_path)
    cli.cmd_crew(SimpleNamespace(action="list", job=0), crew=crew)
    assert "No crew jobs yet" in capsys.readouterr().out
    crew.start("Plan the trip")
    cli.cmd_crew(SimpleNamespace(action="list", job=0), crew=crew)
    cli.cmd_crew(SimpleNamespace(action="show", job=0), crew=crew)
    cli.cmd_crew(SimpleNamespace(action="cancel", job=1), crew=crew)
    out = capsys.readouterr().out
    assert "Job 1 [running]" in out and "Crew job 1: running" in out and "Cancelled crew job 1" in out


# ---- Paperclip ---------------------------------------------------------------------------------------------
class Server:
    """A stand-in Paperclip API."""
    def __init__(self, fail_create=None):
        self.requests, self.fail_create = [], fail_create
        self.issues = []

    def __call__(self, req, timeout=None):
        self.requests.append((req.get_method(), req.full_url, req.get_header("Authorization"),
                              json.loads(req.data) if req.data else None))
        path = req.full_url.split("3100", 1)[1]
        if path == "/api/health":
            body = {"status": "ok"}
        elif path.endswith("/agents"):
            body = [{"id": "a1", "name": "CEO", "role": "ceo"}, {"id": "a2", "name": "Researcher", "role": "research"}]
        elif path.endswith("/issues") and req.get_method() == "POST":
            issue = dict(json.loads(req.data), id="i1", identifier="ARG-1")
            self.issues.append(issue)
            if self.fail_create:
                raise urllib.error.HTTPError(req.full_url, self.fail_create, "x", {}, None)
            body = issue
        elif path.endswith("/issues"):
            body = self.issues
        elif path.endswith("/comments"):
            body = [{"body": "Found three hotels."}, {"body": "  "}]
        elif path.startswith("/api/issues/"):
            body = {"id": "i1", "identifier": "ARG-1", "title": "Plan trip", "status": "in_progress"}
        else:
            raise urllib.error.HTTPError(req.full_url, 404, "x", {}, None)
        return io.BytesIO(json.dumps(body).encode())


def test_paperclip_only_on_this_mac_or_tailscale():
    for url in ("http://localhost:3100", "http://127.0.0.1:3100", "http://[::1]:3100", "http://100.101.1.2:3100",
                "https://mac.tail1234.ts.net"):
        assert pc.allowed_url(url), url
    for url in ("http://example.com", "http://192.168.1.5:3100", "ftp://localhost", "", "http://[bad"):
        assert not pc.allowed_url(url), url
    with pytest.raises(pc.PaperclipError):
        pc.Paperclip("https://paperclip.example.com", "c1", "k")


def test_paperclip_tickets(tmp_path):
    server = Server()
    s = make_settings(tmp_path)
    s.paperclip_url, s.paperclip_company = "http://localhost:3100", "c1"
    tools = {}
    assert pc.attach(s, tools, "secret-key", opener=server) is not None
    run = lambda **a: tools["paperclip"].run(a, None)
    assert run(action="create", title="Plan the NY trip", agent="ceo").content == \
        "On the Paperclip board as ARG-1, assigned to ceo."
    method, url, auth, body = server.requests[-1]
    assert (method, url, auth) == ("POST", "http://localhost:3100/api/companies/c1/issues", "Bearer secret-key")
    assert body["assigneeAgentId"] == "a1" and body["status"] == "todo" and body["description"] == "Plan the NY trip"
    assert run(action="status", issue="ARG-1").content == "ARG-1: Plan trip [in_progress]\n- Found three hotels."
    assert run(action="agents").content == "CEO (ceo)\nResearcher (research)"
    assert "no agent called" in run(action="create", title="x", agent="cfo").content
    assert run(action="create", title=" ").is_error and run(action="status").is_error
    assert run(action="dance").is_error


def test_paperclip_500_after_saving_and_other_errors(tmp_path):
    client = pc.Paperclip("http://localhost:3100", "c1", "k", opener=Server(fail_create=500))
    assert client.create("Plan the trip", "d")["identifier"] == "ARG-1"     # found again by title
    with pytest.raises(pc.PaperclipError, match="answered 403"):
        pc.Paperclip("http://localhost:3100", "c1", "k", opener=Server(fail_create=403)).create("x", "d")

    def down(req, timeout=None):
        raise urllib.error.URLError("refused")
    with pytest.raises(pc.PaperclipError, match="is it running"):
        pc.Paperclip("http://localhost:3100", "c1", "", opener=down).health()

    def junk(req, timeout=None):
        return io.BytesIO(b"<html>")
    with pytest.raises(pc.PaperclipError, match="isn't JSON"):
        pc.Paperclip("http://localhost:3100", "c1", "", opener=junk).health()
    s = make_settings(tmp_path)
    assert pc.attach(s, {}, "") is None                                   # not set up
    s.paperclip_url, s.paperclip_company = "http://example.com", "c1"
    assert pc.attach(s, {}, "") is None                                   # not local: refused


def test_argusctl_paperclip(tmp_path, monkeypatch, capsys):
    from argus import __main__ as cli
    from argus import config
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    assert cli.cmd_paperclip(SimpleNamespace(action="test")) == 1
    answers = iter(["", "c1"])
    client = pc.Paperclip("http://localhost:3100", "c1", "k", opener=Server())
    assert cli.cmd_paperclip(SimpleNamespace(action="setup"), ask=lambda p: next(answers),
                             ask_secret=lambda p: "pc-key-123456", client=client) == 0
    s = config.load()
    assert (s.paperclip_url, s.paperclip_company) == ("http://localhost:3100", "c1")
    assert config.read_secrets(s.secrets_path)["PAPERCLIP_API_KEY"] == "pc-key-123456"
    assert "2 agent(s): CEO, Researcher" in capsys.readouterr().out
    assert cli.cmd_paperclip(SimpleNamespace(action="setup"), ask=lambda p: "http://example.com") == 1
    answers = iter(["", ""])
    monkeypatch.setattr(config, "load", lambda: SimpleNamespace(paperclip_url="", paperclip_company="",
                                                                home=tmp_path))
    assert cli.cmd_paperclip(SimpleNamespace(action="setup"), ask=lambda p: next(answers)) == 1
    monkeypatch.undo()
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))

    def down(req, timeout=None):
        raise urllib.error.URLError("refused")
    assert cli.cmd_paperclip(SimpleNamespace(action="test"),
                             client=pc.Paperclip("http://localhost:3100", "c1", "", opener=down)) == 1
