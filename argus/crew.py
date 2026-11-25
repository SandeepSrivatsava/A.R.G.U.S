"""Argus's crew: dividing a big job into tasks and working through them, the way Paperclip runs a company.

Ideas borrowed (with thanks, MIT) from Paperclip (github.com/paperclipai/paperclip): a lead splits the work
into tickets for roles; tickets can wait on other tickets ("blocked by"); a ticket is checked out by one
worker at a time (atomic checkout); every ticket carries the goal it serves (goal ancestry), so a worker
knows why it's doing it; a budget per job pauses the work when it's spent; anything that needs the owner's OK
goes back to him instead of being done. Written from scratch for Argus: it runs on the Claude API key, in
Argus's own database, with Argus's security rules.

How a job runs:
  1. The lead (the main model) breaks the goal into at most 8 tasks, each with a role (researcher, analyst,
     writer, coder) and the tasks it waits for.
  2. Workers take ready tasks (everything they wait for is done), two at a time. A worker is a Claude turn on
     the "crew" channel: it can look things up, read and write files in Argus's workspace and run code in
     Anthropic's sandbox. Anything that would need the owner's OK (screen, calls, messages, buying, files
     outside the workspace) is refused on this channel; the worker says what it needed and the task is
     marked "needs you".
  3. When nothing is left to run, the lead writes the result up; it's saved in the workspace (crew/) and
     Argus tells the owner. Jobs carry on after a restart.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

log = logging.getLogger("argus.crew")

CHANNEL = "crew"
MAX_TASKS = 8
MAX_PARALLEL = 2
DEFAULT_BUDGET = 1.00          # dollars of Claude use per job
MAX_BUDGET = 10.00
MAX_ATTEMPTS = 2
NEEDS_YOU = "NEEDS_YOU:"

ROLES = {
    "researcher": "Find facts: search the web, read pages, compare sources. Report what you found with where it came "
                  "from; say what you couldn't confirm.",
    "analyst": "Work things out from what's known: compare options, add up costs, rank, decide. Show the reasoning "
               "briefly; use the code sandbox for sums.",
    "writer": "Write the thing that's needed (a plan, a message draft, a summary, an itinerary) clearly and briefly, "
              "and save it as a file in the workspace when it's long.",
    "coder": "Write or fix code or a script; test it in the code sandbox; save it in the workspace.",
}

# Tools a crew worker may use: look-ups, the workspace, the code sandbox (added by agent.py), web search.
CREW_TOOLS = ("fetch_page", "get_news", "search_files", "read_file", "write_file", "list_files", "recall",
              "google_search", "place_search", "weather", "flight_history", "route", "ask_council")

PLAN_TOOL = {
    "name": "plan",
    "description": "The tasks the job is divided into.",
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["tasks"], "properties": {
        "tasks": {"type": "array", "minItems": 1, "maxItems": MAX_TASKS, "items": {
            "type": "object", "additionalProperties": False, "required": ["key", "title", "role", "detail"],
            "properties": {
                "key": {"type": "string", "description": "short id, e.g. t1"},
                "title": {"type": "string"},
                "role": {"type": "string", "enum": sorted(ROLES)},
                "detail": {"type": "string", "description": "exactly what to do and what to hand back"},
                "after": {"type": "array", "items": {"type": "string"}, "description": "keys this waits for"}}}}}},
}

LEAD_PLAN = """You lead a small crew of AI workers for {user}. Divide this job into at most {max} tasks.
Job: {goal}
Rules: each task is one clear piece of work for one role ({roles}). Use "after" when a task needs another's
result. Keep it as small as the job allows (often 2-4 tasks); the last task usually pulls everything together.
Workers can search the web, read pages, use Argus's files and a code sandbox. They can't use the screen, make
calls, send messages, buy, or touch files outside Argus's workspace: if the job needs those, plan up to the
point where {user} has to act, and say so in that task. Call the plan tool."""

WORKER = """You're the {role} on Argus's crew, working for {user}.
The whole job (why this matters): {goal}
Your task: {title}
What to do: {detail}
{inputs}
{role_note}
When you're done, reply with your result: the facts, numbers, links or text the next step needs, briefly.
If you can't finish because something only {user} can give or approve (a choice, a login, a payment, a call),
start your reply with "NEEDS_YOU:" and say exactly what's needed."""

LEAD_SUMMARY = """You led a crew job for {user}. Write the result for {user}: start with the answer in one or two
sentences, then the details that matter, briefly. Name anything that still needs {user}. No preamble.
Job: {goal}
Results:
{results}"""


class Crew:
    def __init__(self, store, agent, settings, *, llm=None, deliver: Callable[[str], None] = lambda text: None,
                 workspace: Optional[Path] = None, audit=None, clock: Callable[[], float] = time.time,
                 parallel: int = MAX_PARALLEL, start_threads: bool = True):
        self.store, self.agent, self.settings = store, agent, settings
        self.llm = llm or getattr(agent, "llm", None)
        self.deliver, self.audit, self.clock = deliver, audit, clock
        self.workspace = Path(workspace or getattr(settings, "workspace", ".")).expanduser()
        self.parallel = max(1, parallel)
        self.start_threads = start_threads
        self._lock = threading.Lock()
        self._running: Dict[int, threading.Thread] = {}
        self._schema()

    # ---- storage ------------------------------------------------------------------------------------
    def _schema(self) -> None:
        q = self.store._q
        q("CREATE TABLE IF NOT EXISTS crew_jobs (id INTEGER PRIMARY KEY, goal TEXT, status TEXT, budget REAL, "
          "spent REAL DEFAULT 0, created REAL, updated REAL, result TEXT DEFAULT '')")
        q("CREATE TABLE IF NOT EXISTS crew_tasks (id INTEGER PRIMARY KEY, job INTEGER, key TEXT, title TEXT, "
          "role TEXT, detail TEXT, after TEXT, status TEXT, owner TEXT DEFAULT '', result TEXT DEFAULT '', "
          "cost REAL DEFAULT 0, attempts INTEGER DEFAULT 0, updated REAL)")

    def job(self, job_id: int) -> Optional[Dict[str, Any]]:
        rows = self.store._q("SELECT id, goal, status, budget, spent, created, result FROM crew_jobs WHERE id=?",
                             (job_id,))
        if not rows:
            return None
        r = rows[0]
        return {"id": r[0], "goal": r[1], "status": r[2], "budget": r[3], "spent": r[4], "created": r[5],
                "result": r[6]}

    def jobs(self, limit: int = 10) -> List[Dict[str, Any]]:
        ids = [r[0] for r in self.store._q("SELECT id FROM crew_jobs ORDER BY id DESC LIMIT ?", (limit,))]
        return [self.job(i) for i in ids]

    def tasks(self, job_id: int) -> List[Dict[str, Any]]:
        rows = self.store._q("SELECT id, key, title, role, detail, after, status, owner, result, cost, attempts "
                             "FROM crew_tasks WHERE job=? ORDER BY id", (job_id,))
        return [{"id": r[0], "key": r[1], "title": r[2], "role": r[3], "detail": r[4], "after": json.loads(r[5]),
                 "status": r[6], "owner": r[7], "result": r[8], "cost": r[9], "attempts": r[10]} for r in rows]

    def _set_job(self, job_id: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.store._q(f"UPDATE crew_jobs SET {sets}, updated=? WHERE id=?", (*fields.values(), self.clock(), job_id))

    def _set_task(self, task_id: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.store._q(f"UPDATE crew_tasks SET {sets}, updated=? WHERE id=?", (*fields.values(), self.clock(), task_id))

    def checkout(self, task_id: int, owner: str) -> bool:
        """Claim a task for one worker. Only one claim can win (the update only matches a task still to do)."""
        with self._lock:
            rows = self.store._q("UPDATE crew_tasks SET status='doing', owner=?, updated=? "
                                 "WHERE id=? AND status='todo' RETURNING id", (owner, self.clock(), task_id))
        return bool(rows)

    def _spend(self, job_id: int, task_id: Optional[int], cost: float) -> None:
        with self._lock:
            self.store._q("UPDATE crew_jobs SET spent=spent+? WHERE id=?", (cost, job_id))
            if task_id is not None:
                self.store._q("UPDATE crew_tasks SET cost=cost+? WHERE id=?", (cost, task_id))

    # ---- planning -----------------------------------------------------------------------------------
    def start(self, goal: str, budget: float = DEFAULT_BUDGET) -> Dict[str, Any]:
        goal = " ".join(str(goal or "").split())[:1500]
        if not goal:
            raise ValueError("say what the job is")
        budget = max(0.05, min(MAX_BUDGET, float(budget or DEFAULT_BUDGET)))
        now = self.clock()
        job_id = self.store._q("INSERT INTO crew_jobs (goal, status, budget, created, updated) VALUES (?,?,?,?,?) "
                               "RETURNING id", (goal, "planning", budget, now, now))[0][0]
        try:
            planned = self._plan(job_id, goal)
        except Exception as exc:
            self._set_job(job_id, status="failed", result=f"couldn't plan it: {exc}")
            raise
        for t in planned:
            self.store._q("INSERT INTO crew_tasks (job, key, title, role, detail, after, status, updated) "
                          "VALUES (?,?,?,?,?,?,?,?)", (job_id, t["key"], t["title"], t["role"], t["detail"],
                                                       json.dumps(t["after"]), "todo", now))
        self._set_job(job_id, status="running")
        if self.audit:
            self.audit.log("crew_started", job=job_id, tasks=len(planned), budget=budget)
        self.resume(job_id)
        return self.job(job_id)

    def _plan(self, job_id: int, goal: str) -> List[Dict[str, Any]]:
        from .agent import request_cost
        model = self.settings.model
        body = {"model": model, "max_tokens": 3000, "tools": [PLAN_TOOL], "tool_choice": {"type": "tool", "name": "plan"},
                "messages": [{"role": "user", "content": LEAD_PLAN.format(
                    user=self.settings.user_name or "the user", max=MAX_TASKS, goal=goal, roles=", ".join(sorted(ROLES)))}]}
        reply = self.llm.create(body)
        self._spend(job_id, None, request_cost(model, reply.get("usage") or {}))
        plan = next((b.get("input") for b in reply.get("content") or [] if b.get("type") == "tool_use"), None)
        return clean_plan((plan or {}).get("tasks") or [])

    # ---- running ------------------------------------------------------------------------------------
    def resume(self, job_id: Optional[int] = None) -> None:
        """Start (or after a restart, pick up again) the jobs still running."""
        ids = [job_id] if job_id is not None else \
            [r[0] for r in self.store._q("SELECT id FROM crew_jobs WHERE status='running'")]
        for i in ids:
            # a task that was being worked on when Argus stopped goes back to the queue
            self.store._q("UPDATE crew_tasks SET status='todo', owner='' WHERE job=? AND status='doing'", (i,))
            if not self.start_threads:
                continue
            with self._lock:
                if i in self._running and self._running[i].is_alive():
                    continue
                thread = threading.Thread(target=self.run_job, args=(i,), name=f"argus-crew-{i}", daemon=True)
                self._running[i] = thread
            thread.start()

    def ready(self, job_id: int) -> List[Dict[str, Any]]:
        tasks = self.tasks(job_id)
        done = {t["key"] for t in tasks if t["status"] == "done"}
        return [t for t in tasks if t["status"] == "todo" and all(k in done for k in t["after"])]

    def run_job(self, job_id: int) -> None:
        while True:
            job = self.job(job_id)
            if job is None or job["status"] != "running":
                return
            if job["spent"] >= job["budget"]:
                self._set_job(job_id, status="paused")
                self.deliver(f"Crew job {job_id} is paused: it used its ${job['budget']:.2f} budget. Say \"carry on "
                             f"with job {job_id}\" to give it more.")
                return
            batch = self.ready(job_id)[: self.parallel]
            if not batch:
                self._block_stuck(job_id)
                self._finish(job_id)
                return
            threads = [threading.Thread(target=self.work, args=(job, t), daemon=True) for t in batch]
            for th in threads:
                th.start()
            for th in threads:
                th.join()

    def work(self, job: Dict[str, Any], task: Dict[str, Any]) -> None:
        owner = f"{task['role']}-{task['id']}"
        if not self.checkout(task["id"], owner):
            return                                  # someone else has it
        from .agent import Events
        inputs = [t for t in self.tasks(job["id"]) if t["key"] in task["after"] and t["result"]]
        given = "\n".join(f"Result of \"{t['title']}\":\n{t['result'][:2500]}" for t in inputs)
        prompt = WORKER.format(role=task["role"], user=self.settings.user_name or "the user", goal=job["goal"],
                               title=task["title"], detail=task["detail"],
                               inputs=("What the earlier tasks found:\n" + given) if given else "",
                               role_note=ROLES.get(task["role"], ""))
        session = self.store.new_session(CHANNEL, f"job-{job['id']}")
        try:
            turn = self.agent.run(session_id=session, channel=CHANNEL, content=prompt, events=Events())
        except Exception as exc:
            log.exception("crew task %s failed", task["id"])
            turn = None
            error = str(exc)
        else:
            error = turn.error or ""
        cost = getattr(turn, "cost", 0.0) or 0.0
        self._spend(job["id"], task["id"], cost)
        text = (getattr(turn, "text", "") or "").strip()
        attempts = task["attempts"] + 1
        if text.upper().startswith(NEEDS_YOU):
            self._set_task(task["id"], status="needs_you", result=text[len(NEEDS_YOU):].strip(), attempts=attempts)
        elif error or not text:
            status = "todo" if attempts < MAX_ATTEMPTS else "failed"
            self._set_task(task["id"], status=status, owner="", attempts=attempts,
                           result=f"(failed: {error or 'no answer'})"[:500])
        else:
            self._set_task(task["id"], status="done", result=text[:6000], attempts=attempts)
        if self.audit:
            self.audit.log("crew_task", job=job["id"], task=task["key"], status=self.task_status(task["id"]),
                           cost_usd=round(cost, 4))

    def task_status(self, task_id: int) -> str:
        rows = self.store._q("SELECT status FROM crew_tasks WHERE id=?", (task_id,))
        return rows[0][0] if rows else ""

    def _block_stuck(self, job_id: int) -> None:
        """Tasks waiting on something that failed or needs the owner can't run: mark them blocked."""
        tasks = self.tasks(job_id)
        by_key = {t["key"]: t for t in tasks}
        for t in tasks:
            if t["status"] == "todo":
                waits = [by_key[k]["title"] for k in t["after"] if k in by_key and by_key[k]["status"] != "done"]
                self._set_task(t["id"], status="blocked", result="waiting on: " + "; ".join(waits))

    def _finish(self, job_id: int) -> None:
        from .agent import request_cost
        job = self.job(job_id)
        tasks = self.tasks(job_id)
        lines = [f"- [{t['status']}] {t['title']} ({t['role']}): {t['result'][:3000]}" for t in tasks]
        summary = ""
        try:
            model = self.settings.model
            reply = self.llm.create({"model": model, "max_tokens": 1500, "messages": [{"role": "user", "content":
                                     LEAD_SUMMARY.format(user=self.settings.user_name or "the user",
                                                         goal=job["goal"], results="\n".join(lines))}]})
            self._spend(job_id, None, request_cost(model, reply.get("usage") or {}))
            summary = "".join(b.get("text", "") for b in reply.get("content") or [] if b.get("type") == "text").strip()
        except Exception as exc:
            log.warning("crew summary failed: %s", exc)
        if not summary:
            summary = "\n".join(lines)
        needs = [t for t in tasks if t["status"] in ("needs_you", "failed", "blocked")]
        status = "done" if not needs else "needs you"
        path = self._save(job_id, job["goal"], summary, tasks)
        self._set_job(job_id, status=status, result=summary[:6000])
        if self.audit:
            self.audit.log("crew_finished", job=job_id, status=status, spent_usd=round(self.job(job_id)["spent"], 4))
        first = re.split(r"(?<=[.!?])\s", summary.strip(), maxsplit=1)[0][:300]
        self.deliver(f"Crew job {job_id} is {'done' if status == 'done' else 'finished, but needs you for a step'}: "
                     f"{first}" + (f" The full write-up is in {path}." if path else ""))

    def _save(self, job_id: int, goal: str, summary: str, tasks: Sequence[Dict[str, Any]]) -> str:
        try:
            folder = self.workspace / "crew"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"job-{job_id}.md"
            body = [f"# Crew job {job_id}", "", f"**Goal:** {goal}", "", summary, "", "## Tasks", ""]
            for t in tasks:
                body += [f"### {t['title']} ({t['role']}, {t['status']})", "", t["result"] or "(nothing)", ""]
            path.write_text("\n".join(body), encoding="utf-8")
            return str(path)
        except OSError as exc:
            log.warning("couldn't save crew job %s: %s", job_id, exc)
            return ""

    # ---- the owner's controls -------------------------------------------------------------------------
    def cancel(self, job_id: int) -> str:
        job = self.job(job_id)
        if job is None:
            return f"There's no crew job {job_id}."
        if job["status"] in ("done", "needs you", "failed", "cancelled"):
            return f"Crew job {job_id} has already ended ({job['status']})."
        self._set_job(job_id, status="cancelled")
        return f"Cancelled crew job {job_id}; the task being worked on now finishes, nothing new starts."

    def carry_on(self, job_id: int, more: float = DEFAULT_BUDGET) -> str:
        job = self.job(job_id)
        if job is None:
            return f"There's no crew job {job_id}."
        if job["status"] not in ("paused", "cancelled"):
            return f"Crew job {job_id} is {job['status']}, not paused."
        self._set_job(job_id, status="running", budget=min(job["spent"] + MAX_BUDGET, job["budget"] + max(0.05, more)))
        self.resume(job_id)
        return f"Crew job {job_id} is running again."

    def describe(self, job_id: int) -> str:
        job = self.job(job_id)
        if job is None:
            return f"There's no crew job {job_id}."
        out = [f"Crew job {job_id}: {job['status']}, ${job['spent']:.2f} of ${job['budget']:.2f} used. Goal: {job['goal']}"]
        for t in self.tasks(job_id):
            waits = f" (after {', '.join(t['after'])})" if t["after"] else ""
            out.append(f"  {t['key']} [{t['status']}] {t['title']} - {t['role']}{waits}"
                       + (f": {t['result'][:200]}" if t["status"] in ("done", "needs_you", "failed", "blocked") else ""))
        if job["result"]:
            out.append("Result: " + job["result"][:1500])
        return "\n".join(out)


def clean_plan(tasks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The lead's plan made safe to run: unique keys, known roles, waits only on earlier tasks (so no loops)."""
    out: List[Dict[str, Any]] = []
    seen: List[str] = []
    for i, t in enumerate(list(tasks)[:MAX_TASKS]):
        if not isinstance(t, dict):
            continue
        key = re.sub(r"[^\w-]", "", str(t.get("key") or f"t{i + 1}"))[:20] or f"t{i + 1}"
        while key in seen:
            key += "x"
        title = " ".join(str(t.get("title") or "").split())[:160]
        if not title:
            continue
        role = t.get("role") if t.get("role") in ROLES else "researcher"
        after = [k for k in (t.get("after") or []) if isinstance(k, str) and k in seen]
        out.append({"key": key, "title": title, "role": role,
                    "detail": " ".join(str(t.get("detail") or title).split())[:1500], "after": after})
        seen.append(key)
    if not out:
        raise ValueError("the lead didn't produce any tasks")
    return out


def open_to_crew(tools: Dict[str, Any], names: Sequence[str] = CREW_TOOLS) -> None:
    """Let crew workers use these tools (look-ups, the workspace)."""
    for name in names:
        tool = tools.get(name)
        if tool is not None:
            tools[name] = replace(tool, channels=frozenset(tool.channels) | {CHANNEL})


DESCRIPTION = (
    "Hand a big, multi-step job to Argus's crew: a lead splits it into tasks for workers (researcher, analyst, "
    "writer, coder) that run one after another or side by side, in the background, then reports back. For jobs "
    "with several parts that take more than a quick answer (\"plan the New York trip: hotels near Midtown under "
    "$250, a dinner each night and the best way from the airport\", \"compare these three laptops and write me a "
    "recommendation\"). Workers can search and read the web and Argus's files and run code; they can't use the "
    "screen, call, message or buy, so those steps come back to the user. action 'start' (goal in the user's words, "
    "budget_usd default 1, max 10), 'status' (job), 'list', 'cancel' (job), 'carry_on' (job, budget_usd more). "
    "Say what it will cost at most before starting a job over $2.")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["action"], "properties": {
    "action": {"type": "string", "enum": ["start", "status", "list", "cancel", "carry_on"]},
    "goal": {"type": "string"}, "job": {"type": "integer"},
    "budget_usd": {"type": "number", "minimum": 0.05, "maximum": MAX_BUDGET}}}


def attach(crew: Crew, tools: Dict[str, Any]) -> None:
    from .toolbox import Tool, ToolOutput
    open_to_crew(tools)

    def run(args: Dict[str, Any], ctx) -> ToolOutput:
        action = args.get("action")
        try:
            if action == "start":
                job = crew.start(args.get("goal") or "", args.get("budget_usd") or DEFAULT_BUDGET)
                tasks = crew.tasks(job["id"])
                plan = "; ".join(f"{t['title']} ({t['role']})" for t in tasks)
                return ToolOutput(f"Started crew job {job['id']} (up to ${job['budget']:.2f}) with {len(tasks)} "
                                  f"tasks: {plan}. It runs in the background; Argus will say when it's done.")
            if action == "status":
                job_id = args.get("job") or (crew.jobs(1)[0]["id"] if crew.jobs(1) else 0)
                return ToolOutput(crew.describe(int(job_id)))
            if action == "list":
                jobs = crew.jobs(10)
                return ToolOutput("\n".join(f"Job {j['id']} [{j['status']}] {j['goal'][:120]}" for j in jobs)
                                  or "No crew jobs yet.")
            if action == "cancel":
                return ToolOutput(crew.cancel(int(args.get("job") or 0)))
            if action == "carry_on":
                return ToolOutput(crew.carry_on(int(args.get("job") or 0), args.get("budget_usd") or DEFAULT_BUDGET))
        except ValueError as exc:
            return ToolOutput.error(str(exc))
        except Exception as exc:
            log.exception("crew %s failed", action)
            return ToolOutput.error(f"the crew couldn't {action}: {exc}")
        return ToolOutput.error("unknown action")

    tools["crew"] = Tool("crew", DESCRIPTION, SCHEMA, run, frozenset({"voice", "phone", "cli", "android"}))
