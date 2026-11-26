"""2.16.0: ideas from the tools Sandeep sent (OmniRoute, Headroom, Humanizer, Caveman, skills, Footprint),
rebuilt for Argus, with most tests on what goes wrong."""
import json

import pytest

from argus import tiers
from argus.agent import Events, switch_model
from argus.llm import LLMError

from fakes import Script, call, make_agent, make_settings, say


def run(agent, text, channel="cli"):
    session = agent.store.session_for(channel, "test", 30)
    return agent.run(session_id=session, channel=channel, content=text, events=Events())


def audit_events(tmp_path, name):
    out = []
    for line in (tmp_path / "audit.jsonl").read_text().splitlines():
        rec = json.loads(line)
        if rec["event"] == name:
            out.append(rec)
    return out


# ---- model fallback (OmniRoute) ---------------------------------------------------------------------------
def test_fallback_order_never_repeats_a_model(tmp_path):
    s = make_settings(tmp_path)
    assert tiers.fallbacks(s.model, s) == [s.heavy_model, s.light_model]
    assert tiers.fallbacks(s.heavy_model, s) == [s.model, s.light_model]
    s.heavy_model = s.light_model = ""
    assert tiers.fallbacks(s.model, s) == [s.small_model]
    s.small_model = s.model
    assert tiers.fallbacks(s.model, s) == []


@pytest.mark.parametrize("exc,expected", [
    (LLMError("x", 529, "overloaded_error", retryable=True), True),
    (LLMError("x", 429, "rate_limit_error", retryable=True), True),
    (LLMError("x", 500, "api_error", retryable=True), True),
    (LLMError("x", 404, "not_found_error"), True),                      # a retired model
    (LLMError("x", 0, "overloaded_error", retryable=True), True),       # said inside the stream
    (LLMError("no network", retryable=True), False),                    # another model won't help
    (LLMError("x", 401, "authentication_error"), False),
    (LLMError("x", 402, "billing_error"), False),
    (LLMError("x", 400, "invalid_request_error"), False),
    (LLMError("x", 529, "overloaded_error", retryable=True, partial=True), False),   # resumed instead
    (LLMError("cancelled", kind="cancelled"), False),
])
def test_which_errors_switch_models(exc, expected):
    assert switch_model(exc) is expected


def test_an_overloaded_model_hands_the_turn_to_the_next(tmp_path):
    s = make_settings(tmp_path, routing="off")
    agent, mock = make_agent(tmp_path, Script(say("Here you go.")), settings=s)
    mock.fail_models[s.model] = 529
    turn = run(agent, "what's up")
    assert turn.text == "Here you go." and not turn.error
    assert turn.model == s.heavy_model
    assert mock.models_asked[-1] == s.heavy_model
    rec = audit_events(tmp_path, "model_fallback")
    assert rec and rec[0]["failed"] == s.model and rec[0]["to"] == s.heavy_model
    assert not mock.invalid


def test_a_retired_model_falls_back_too(tmp_path):
    s = make_settings(tmp_path, routing="off")
    agent, mock = make_agent(tmp_path, Script(say("Still here.")), settings=s)
    mock.fail_models[s.model] = 404
    mock.fail_models[s.heavy_model] = 529
    turn = run(agent, "hello")
    assert turn.text == "Still here." and turn.model == s.light_model


def test_fallback_gives_up_after_every_model_failed(tmp_path):
    s = make_settings(tmp_path, routing="off")
    agent, mock = make_agent(tmp_path, Script(say("never")), settings=s)
    for m in (s.model, s.heavy_model, s.light_model, s.small_model):
        mock.fail_models[m] = 529
    turn = run(agent, "hello")
    assert turn.error and "529" in turn.error
    assert len(audit_events(tmp_path, "model_fallback")) <= 2


def test_a_wrong_key_does_not_hop_between_models(tmp_path):
    s = make_settings(tmp_path, routing="off")
    agent, mock = make_agent(tmp_path, Script(say("never")), settings=s)
    mock.fail_models[s.model] = 401
    turn = run(agent, "hello")
    assert turn.error and not audit_events(tmp_path, "model_fallback")
    assert set(mock.models_asked) == {s.model}


def test_fallback_mid_task_keeps_the_tool_results(tmp_path):
    s = make_settings(tmp_path, routing="off")
    (s.workspace / "notes.txt").write_text("milk, eggs")
    script = Script(call("read_file", {"path": str(s.workspace / "notes.txt")}), say("Milk and eggs."))
    agent, mock = make_agent(tmp_path, script, settings=s)
    original = mock.responder

    def responder(body):
        reply = original(body)
        mock.fail_models[s.model] = 529      # the tool runs next; then the main model is overloaded
        return reply
    mock.responder = responder
    turn = run(agent, "what's in my notes")
    assert turn.text == "Milk and eggs." and turn.model == s.heavy_model
    last = script.bodies[-1]["messages"][-1]["content"]
    assert any(b.get("type") == "tool_result" for b in last)
    assert not mock.invalid


# ---- squeezing long tool results (Headroom) ---------------------------------------------------------------
from argus.squeeze import Squeezer, tidy


def test_short_results_and_non_text_go_in_untouched():
    sq = Squeezer(over=100)
    assert sq.squeeze("weather", "sunny") == "sunny"
    blocks = [{"type": "image"}]
    assert sq.squeeze("screenshot", blocks) is blocks
    assert sq.squeeze("code_fix", "x" * 1000) == "x" * 1000          # exact text matters for edits
    assert sq.squeeze("write_file", "y" * 1000) == "y" * 1000


def test_json_is_compacted_and_empty_fields_dropped():
    raw = json.dumps({"a": 1, "b": None, "c": "", "d": [], "e": {"f": None, "g": [1, {"h": ""}]}}, indent=4)
    assert tidy(raw) == '{"a":1,"e":{"g":[1,{}]}}'
    assert tidy("{not json") == "{not json"


def test_repeated_lines_fold_and_blank_runs_shrink():
    text = "start\n" + "same\n" * 40 + "\n\n\n\n\nend"
    out = tidy(text)
    assert out.count("same") == 2 and "same line 39 more times" in out
    assert "\n\n\n" not in out and out.endswith("end")
    assert tidy("a\n\n\nb") == "a\n\nb"


def test_long_results_keep_head_and_tail_and_the_middle_can_be_read_back():
    sq = Squeezer(over=1000, head=300, tail=100, part=500)
    text = "UNTRUSTED header\n" + "".join(f"line {i} of the page\n" for i in range(300)) + "THE END"
    out = sq.squeeze("fetch_page", text)
    assert len(out) < 800 and out.startswith("UNTRUSTED header") and out.endswith("THE END")
    assert "expand_result" in out and "r1" in out
    first, why = sq.expand("r1", 1)
    assert not why and "part 1 of" in first and "UNTRUSTED header" in first
    found, _ = sq.expand("R1", find="line 150 of")
    assert "line 150 of the page" in found
    missing, _ = sq.expand("r1", find="zebra")
    assert "doesn't appear" in missing
    assert sq.saved_chars > 0


def test_expand_refuses_bad_ids_parts_and_empty_searches():
    sq = Squeezer(over=50, head=10, tail=10, part=40)
    sq.squeeze("fetch_page", "z" * 200)
    assert sq.expand("r9")[0] is None and "run the tool again" in sq.expand("r9")[1]
    assert sq.expand("", 1)[0] is None
    assert sq.expand("r1", 0)[0] is None and "parts 1 to 5" in sq.expand("r1", 99)[1]
    assert sq.expand("r1", find="   ")[0] is None


def test_only_the_newest_results_are_kept():
    sq = Squeezer(over=10, head=2, tail=2, keep=3)
    for i in range(5):
        sq.squeeze("fetch_page", f"{i}" * 50 + "abcdefghij" * 3)
    assert sq.expand("r1")[0] is None and sq.expand("r2")[0] is None
    assert sq.expand("r5")[0] is not None


def test_the_agent_sends_the_short_version_and_can_read_the_rest(tmp_path):
    s = make_settings(tmp_path, routing="off")
    big = s.workspace / "big.txt"
    big.write_text("".join(f"row {i}: value {i * 7}\n" for i in range(3000)) + "the secret word is pineapple\n")
    script = Script(call("read_file", {"path": str(big), "max_chars": 100000}),
                    call("expand_result", {"id": "r1", "find": "secret word"}),
                    say("It's pineapple."))
    agent, mock = make_agent(tmp_path, script, settings=s)
    turn = run(agent, "what is the secret word in big.txt")
    assert turn.text == "It's pineapple."
    first = [b for b in script.bodies[1]["messages"][-1]["content"] if b.get("type") == "tool_result"][0]
    assert len(json.dumps(first)) < 14000 and "expand_result" in json.dumps(first)
    second = [b for b in script.bodies[2]["messages"][-1]["content"] if b.get("type") == "tool_result"][0]
    assert "pineapple" in json.dumps(second)
    assert not mock.invalid


# ---- the daily review: better every day -------------------------------------------------------------------
from argus import daily
from argus.scheduler import REVIEW_JOB, Scheduler, sync_default_jobs
from argus.store import Store


def entry(event, day="2026-11-24", hour=10, **fields):
    return dict({"ts": f"{day}T{hour:02d}:00:00-0800", "event": event}, **fields)


def turn_entry(first=1.0, cost=0.02, error=None, day="2026-11-24"):
    return entry("turn", day=day, cost_usd=cost, error=error, timings={"claude_first_s": [first, 0.5]})


def test_review_counts_only_that_day_and_reads_messy_entries():
    entries = [turn_entry(1.0), turn_entry(2.0), turn_entry(3.0, error="x"), turn_entry(9.0, day="2026-11-23"),
               entry("turn", timings=None), entry("turn", timings={"claude_first_s": []}),
               entry("tool_failed", tool="weather", error="weather failed: KeyError 'temp' at 12"),
               entry("tool_failed", tool="weather", error="weather failed: KeyError 'wind' at 99"),
               entry("tool_slow", tool="fetch_page"), entry("model_fallback"), entry("barge_in"),
               entry("llm_error", error="couldn't reach the Claude API (Errno 8)"), {"no": "ts"}, {"ts": None}]
    d = daily.review(entries, "2026-11-24")
    assert d.requests == 5 and d.errors == 1 and d.first_words == 2.0
    assert d.failures == {"weather: weather failed: KeyError '…' at N": 2}
    assert d.slow == {"fetch_page": 1} and d.fallbacks == 1 and d.offline == 1 and d.barge_ins == 1


def test_a_quiet_good_day_says_nothing():
    d = daily.review([turn_entry(1.0), entry("tool_failed", tool="weather", error="once")], "2026-11-24")
    assert daily.summary(d) == ""
    assert "Nothing went wrong more than once" in daily.report(d)


def test_a_repeated_crash_comes_with_the_words_to_fix_it():
    entries = [turn_entry()] + [entry("tool_failed", tool="weather", error="weather failed: TypeError x")] * 3
    note = daily.summary(daily.review(entries, "2026-11-24"))
    assert note.startswith("Yesterday's review: 1 request,")
    assert "weather crashed 3 times" in note and "fix the weather crash" in note and "Touch ID" in note


def test_quota_slowdowns_fallbacks_and_voice_trouble_are_explained():
    entries = ([turn_entry(2.5)] * 4 +
               [entry("tool_failed", tool="google_search", error="Google search (Gemini): the Gemini API: today's "
                                                                  "free requests are used up")] * 2 +
               [entry("tool_slow", tool="fetch_page")] * 3 + [entry("model_fallback")] +
               [entry("voice_not_recognised")] * 3 + [entry("llm_error", error="couldn't reach the Claude API")] * 3)
    before = [daily.Day("2026-11-23", requests=5, first_words=1.0), daily.Day("2026-11-22", requests=3, first_words=1.2)]
    found = daily.findings(daily.review(entries, "2026-11-24"), before)
    text = " ".join(found)
    assert "google_search ran out of its free requests (2 times)" in text
    assert "fetch_page was slow 3 times" in text and "started slower: 2.5 s" in text
    assert "another model answered" in text and "argusctl enroll" in text and "internet dropped 3" in text


def test_faster_answers_are_noticed_and_error_heavy_days_flagged():
    entries = [turn_entry(0.8, error="boom")] * 4
    found = daily.findings(daily.review(entries, "2026-11-24"), [daily.Day("x", first_words=2.0)])
    assert any("started faster" in f for f in found) and any("4 of 4 requests ended in an error" in f for f in found)


def test_the_reviewer_keeps_history_and_survives_a_broken_row(tmp_path):
    store = Store(tmp_path / "a.db")
    audit = tmp_path / "audit.jsonl"
    lines = [turn_entry(1.0, day="2026-11-23"), turn_entry(3.0, day="2026-11-24")] + \
            [entry("tool_failed", tool="maps", error="maps failed: ValueError")] * 2
    audit.write_text("not json\n" + "\n".join(json.dumps(e) for e in lines) + "\n")
    (tmp_path / "audit.jsonl.1").write_text(json.dumps(turn_entry(1.0, day="2026-11-22")) + "\n")
    r = daily.Reviewer(store, audit)
    r.run("2026-11-22")
    r.run("2026-11-23")
    store.set("daily:2026-11-21", "{broken")
    note = r.morning(daily.date(2026, 11, 25))
    assert "maps crashed 2 times" in note and "started slower: 3.0 s" in note
    assert [d.day for d in r.history("2026-11-24")] == ["2026-11-23", "2026-11-22"]
    assert r.saved("2026-11-21") is None and r.saved("1999-01-01") is None


def test_the_review_job_speaks_only_when_there_is_something(tmp_path):
    store = Store(tmp_path / "a.db")
    sync_default_jobs(store, "", "", "America/Los_Angeles", "41 7 * * *")
    def next_run():
        return next(t["next_run"] for t in store.tasks() if t["text"] == REVIEW_JOB)
    assert store.task_named(REVIEW_JOB) is not None
    said = []
    notes = ["", "Yesterday's review: something"]
    sched = Scheduler(store, deliver=said.append, run_task=lambda t: "", headlines=lambda: "",
                      tz="America/Los_Angeles", review=lambda: notes.pop(0))
    assert sched.tick(next_run() + 1) == 1 and said == []
    assert sched.tick(next_run() + 1) == 1 and said == ["Yesterday's review: something"]
    broken = Scheduler(store, deliver=said.append, run_task=lambda t: "", headlines=lambda: "",
                       tz="America/Los_Angeles", review=lambda: 1 / 0)
    assert broken.tick(next_run() + 1) == 1
    assert "couldn't run" in said[-1]
    sync_default_jobs(store, "", "", "America/Los_Angeles", "")          # turned off
    assert store.task_named(REVIEW_JOB) is None


def test_argusctl_today_and_review(tmp_path, monkeypatch, capsys):
    from argus import __main__ as cli, config
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    s = config.load()
    s.audit_path.parent.mkdir(parents=True, exist_ok=True)
    s.audit_path.write_text(json.dumps(turn_entry(1.2, day="2026-11-24")) + "\n")
    assert cli.main(["review", "2026-11-24"]) == 0
    assert "2026-11-24: 1 requests" in capsys.readouterr().out
    assert cli.main(["review", "yesterday"]) == 2
    assert cli.main(["today"]) == 0


# ---- skills, brief mode, humanizer --------------------------------------------------------------------------
import os

from argus import skills as skills_mod
from argus.humanize import humanize


def system_text(body):
    return "\n".join(b.get("text", "") for b in body["system"])


def write_skill(folder, name, text, mode=0o644):
    d = folder / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(text)
    os.chmod(f, mode)
    return f


def test_built_in_skills_load_and_match_the_right_requests():
    found, problems = skills_mod.load()
    assert {"humanizer", "clean-code", "brief"} <= set(found) and problems == []
    names = lambda req: [s.name for s in skills_mod.pick(found, req)]
    assert names("draft a text to my landlord about the leak") == ["humanizer"]
    assert names("fix the weather crash from yesterday") == ["clean-code"]
    assert names("what's the weather tomorrow") == []
    assert names("use the humanizer on this") == ["humanizer"]
    assert names("") == []
    assert [s.name for s in skills_mod.pick(found, "what time is it", brief=True)] == ["brief"]
    assert "brief" not in names("be brief")            # brief mode decides that, not the words


def test_your_skills_replace_built_ins_and_bad_ones_are_skipped(tmp_path):
    write_skill(tmp_path, "humanizer", "---\nname: humanizer\ndescription: mine\n---\nWrite like me.")
    write_skill(tmp_path, "marketing", "---\nname: marketing\ndescription: copywriting seo ads email campaigns "
                                       "landing pages\n---\nSell it.")
    write_skill(tmp_path, "open", "---\nname: open\ndescription: x\n---\nanyone can edit", mode=0o666)
    write_skill(tmp_path, "nohead", "just text, no header")
    write_skill(tmp_path, "empty", "---\nname: empty\ndescription: x\n---\n   ")
    write_skill(tmp_path, "badname", "---\nname: ../../etc\ndescription: x\n---\nhi")
    write_skill(tmp_path, "huge", "---\nname: huge\ndescription: x\n---\n" + "x" * 50_000)
    (tmp_path / "linked").mkdir()
    os.symlink(tmp_path / "marketing" / "SKILL.md", tmp_path / "linked" / "SKILL.md")
    found, problems = skills_mod.load([tmp_path, tmp_path / "missing"])
    assert found["humanizer"].body == "Write like me." and found["humanizer"].source == "yours"
    assert "marketing" in found and "open" not in found and "huge" not in found and "badname" not in found
    text = " ".join(problems)
    assert "other users could change it" in text and "needs a --- name" in text and "over 40 KB" in text
    assert "link to another file" in text
    assert [s.name for s in skills_mod.pick(found, "write seo copy for my landing pages and ads")][0] == "marketing"


def test_skills_reach_the_owner_prompt_but_never_a_guest(tmp_path):
    script = Script(say("Here's a draft."), say("Hello."))
    agent, mock = make_agent(tmp_path, script)
    run(agent, "draft a text to Priya saying I'll be late", channel="cli")
    assert 'Skill "humanizer"' in system_text(script.bodies[0])
    assert audit_events(tmp_path, "skills_used")[0]["skills"] == ["humanizer"]
    run(agent, "draft a text to my friend", channel="guest")
    assert "Skill " not in system_text(script.bodies[1])


def test_brief_mode_by_voice(tmp_path):
    script = Script(call("reply_style", {"brief": True}), say("Okay."), say("Sunny."),
                    call("reply_style", {"brief": "yes"}), say("Hm."))
    agent, mock = make_agent(tmp_path, script)
    run(agent, "keep your answers short from now on")
    assert skills_mod.is_brief(agent.store)
    run(agent, "what's it like outside")
    assert 'Skill "brief"' in system_text(script.bodies[2])
    run(agent, "talk normally")
    assert "Say whether brief mode" in json.dumps(script.bodies[4]["messages"][-1])
    agent.store.set(skills_mod.STYLE_KEY, "normal")
    assert not skills_mod.is_brief(agent.store)


def test_reply_style_is_not_offered_to_guests(tmp_path):
    script = Script(say("Hi."))
    agent, _ = make_agent(tmp_path, script)
    run(agent, "hello", channel="guest")
    assert "reply_style" not in [t.get("name") for t in script.bodies[0]["tools"]]


@pytest.mark.parametrize("before,after", [
    ("Running late — be there by 7", "Running late, be there by 7"),
    ("I hope this message finds you well. The rent is paid.", "The rent is paid."),
    ("Rent is paid. Let me know if you have any questions!", "Rent is paid."),
    ("Got it -- thanks", "Got it, thanks"),
    ("It’s “fine”", "It's \"fine\""),
    ("I hope this helps", "I hope this helps"),          # nothing left would mean sending nothing: keep it
    ("", ""),
    ("ok see you at 5", "ok see you at 5"),
])
def test_humanize_only_removes_stock_habits(before, after):
    assert humanize(before) == after


def test_a_text_is_tidied_before_you_approve_it(tmp_path):
    from argus.approvals import ONCE
    s = make_settings(tmp_path)
    sent = []
    from argus.toolbox import Tool, ToolOutput
    script = Script(call("send_text", {"to": "+15035550142", "message": "On my way — 10 mins. I hope this helps!"}),
                    say("Sent."))
    agent, _ = make_agent(tmp_path, script, settings=s)
    agent.tools["send_text"] = Tool("send_text", "t", {"type": "object"},
                                    lambda a, c: sent.append(a["message"]) or ToolOutput("sent"))
    asked = []

    class Approver(Events):
        def approve(self, request):
            asked.append(request.command)
            return ONCE
    agent.run(session_id=agent.store.session_for("cli", "t", 30), channel="cli", content="text Sam I'm on my way",
              events=Approver())
    assert sent == ["On my way, 10 mins."]
    assert "On my way, 10 mins." in asked[0]


def test_argusctl_skills_lists_and_explains_skips(tmp_path, monkeypatch, capsys):
    from argus import __main__ as cli, config
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    folder = config.load().skills_dir
    write_skill(folder, "open", "---\nname: open\ndescription: x\n---\nhi", mode=0o666)
    assert cli.main(["skills"]) == 0
    out = capsys.readouterr().out
    assert "humanizer (built in)" in out and "Skipped open" in out and "SKILL.md" in out


# ---- Gemini rests after its free requests run out (seen in the real logs, 2026-11-24) --------------------
from argus import search
from argus.toolbox import Context


class Gemini429:
    def __init__(self, message):
        self.message, self.calls = message, 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return 429, {"error": {"message": self.message}}


def test_after_the_daily_limit_google_search_rests_until_midnight_pacific(tmp_path):
    now = [1764100000.0]                       # an afternoon in late November 2025
    fake = Gemini429("Quota exceeded for metric generate_requests_per_day")
    tools = {}
    search.attach(make_settings(tmp_path), {"GEMINI_API_KEY": "gem-key-1"}, tools, request=fake, clock=lambda: now[0])
    ctx = Context(channel="voice", workspace=tmp_path, store=None)
    first = tools["google_search"].run({"question": "x"}, ctx)
    assert first.is_error and "daily limit" in first.content and "web_search" in first.content
    second = tools["google_search"].run({"question": "y"}, ctx)
    assert not second.is_error and "resting" in second.content and fake.calls == 1
    now[0] = search.rest_until("daily limit", 1764100000.0) + 1
    tools["google_search"].run({"question": "z"}, ctx)
    assert fake.calls == 2


def test_a_burst_limit_rests_only_a_few_minutes(tmp_path):
    now = [1000.0]
    fake = Gemini429("Resource has been exhausted (e.g. check quota).")
    tools = {}
    search.attach(make_settings(tmp_path), {"GEMINI_API_KEY": "gem-key-1"}, tools, request=fake, clock=lambda: now[0])
    ctx = Context(channel="voice", workspace=tmp_path, store=None)
    tools["google_search"].run({"question": "x"}, ctx)
    now[0] += 300
    tools["google_search"].run({"question": "x"}, ctx)
    assert fake.calls == 1
    now[0] += 301
    tools["google_search"].run({"question": "x"}, ctx)
    assert fake.calls == 2
    assert search.rest_until("Google refused the key", 5.0) == 5.0       # other errors: no rest


# ---- privacy: data brokers and people-search sites (Footprint) ---------------------------------------------
from argus import privacy


def test_privacy_plan_starts_with_drop_and_never_touches_identity_checks(tmp_path):
    tools = {}
    privacy.attach(Store(tmp_path / "a.db"), tools)
    plan = tools["privacy"].run({"action": "plan"}, None).content
    assert plan.startswith("1. California DROP (https://privacy.ca.gov/drop/)") and "Whitepages" in plan
    assert "Never fill in identity checks" in plan
    assert "guest" not in tools["privacy"].channels


def test_privacy_tracking_follow_ups_and_bad_input(tmp_path):
    store = Store(tmp_path / "a.db")
    day = 86400.0
    assert privacy.track(store, "https://www.spokeo.com/", "sent", now=0)[1] == "spokeo.com: sent."
    assert privacy.track(store, "Whitepages", "confirmed", "  email   clicked ", now=day)[0]
    assert privacy.track(store, "Radaris", "reappeared", now=2 * day)[0]
    assert privacy.track(store, "", "sent") == (False, "Which site?")
    assert not privacy.track(store, "X", "deleted")[0]
    out = privacy.status(store, now=50 * day)
    assert "spokeo.com: sent (50 days ago)" in out and "Whitepages: confirmed (49 days ago), email clicked" in out
    assert "Needs a follow-up: Radaris (listed again), spokeo.com" in out
    assert privacy.track(store, "SPOKEO.COM", "removed", now=51 * day)[0]           # same site, any case
    assert privacy.status(store, now=52 * day).endswith("Needs a follow-up: Radaris (listed again)")
    store.set(privacy.KEY, "{broken")
    assert "No removal requests tracked yet" in privacy.status(store)
    store.set(privacy.KEY, json.dumps([{"site": "A", "status": "sent"}, "junk", {"nosite": 1}]))
    assert privacy.status(store, now=0).startswith("A: sent")


def test_privacy_list_has_a_limit(tmp_path):
    store = Store(tmp_path / "a.db")
    for i in range(privacy.MAX_SITES):
        assert privacy.track(store, f"site{i}", "sent", now=0)[0]
    assert privacy.track(store, "one-more", "sent", now=0) == (False, f"The list is full ({privacy.MAX_SITES} sites).")
    assert privacy.track(store, "site3", "removed", now=0)[0]             # updating is still fine


def test_privacy_tool_errors(tmp_path):
    tools = {}
    privacy.attach(Store(tmp_path / "a.db"), tools, clock=lambda: 0.0)
    run_ = tools["privacy"].run
    assert run_({"action": "track", "site": "", "status": "sent"}, None).is_error
    assert run_({"action": "nope"}, None).is_error
    assert run_({"action": "track", "site": "Nuwber", "status": "sent"}, None).content == "Nuwber: sent."
    assert "Nuwber: sent" in run_({"action": "status"}, None).content


def test_self_fix_cannot_rewrite_the_skills_it_reads():
    from argus.selfcare import editable
    assert not editable("argus/skills/humanizer/SKILL.md") and not editable("argus/skills.py")
    assert editable("argus/daily.py") and editable("argus/squeeze.py")
