import base64
import json
import threading
from pathlib import Path

import pytest

from argus.agent import Events, budget_tokens, prune_images, repair, request_cost
from argus.approvals import DENY, ONCE, TASK
from argus.computer import NOT_EXECUTED, Computer
from argus.llm import Claude, LLMError, StreamAccumulator

from fakes import Script, call, make_agent, make_settings, say

FIXTURES = Path(__file__).parent / "fixtures"


class Recorder(Events):
    def __init__(self, answers=()):
        self.deltas, self.started, self.requests = [], [], []
        self.answers = list(answers)

    def text(self, delta):
        self.deltas.append(delta)

    def tool_started(self, name, description):
        self.started.append(name)

    def approve(self, request):
        self.requests.append(request)
        return self.answers.pop(0) if self.answers else DENY


def run(agent, text, events=None, channel="voice", session=None, cancel=None):
    session = session or agent.store.session_for(channel, "test", 30)
    return agent.run(session_id=session, channel=channel, content=text, events=events or Recorder(), cancel=cancel)


# ---- the API client -----------------------------------------------------------------------------------
def test_accumulator_rebuilds_blocks_exactly():
    acc = StreamAccumulator()
    events = [
        {"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "there."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "left_click",
                                                                       "toolset_name": "computer", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"coordi'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'nate": [1, 2]}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "web_search_tool_result", "tool_use_id": "s1",
                                                                       "content": [{"encrypted_content": "abc"}]}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}},
    ]
    texts = [acc.feed(e) for e in events]
    assert "".join(t for t in texts if t) == "Hi there."
    assert acc.content[1] == {"type": "tool_use", "id": "t1", "name": "left_click", "toolset_name": "computer",
                              "input": {"coordinate": [1, 2]}}
    assert acc.content[2]["content"][0]["encrypted_content"] == "abc"
    assert acc.stop_reason == "tool_use" and acc.usage == {"input_tokens": 10, "output_tokens": 30}


def test_client_retries_overloads_and_reports_bad_keys(tmp_path):
    agent, mock = make_agent(tmp_path, Script(say("Hello.")))
    mock.fail_next = [529]
    client = Claude("sk-ant-test", base_url=mock.url, max_retries=2)
    acc = StreamAccumulator()
    for e in client.stream({"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}):
        acc.feed(e)
    assert acc.text == "Hello."
    with pytest.raises(LLMError) as err:
        list(Claude("sk-ant-wrong", base_url=mock.url).stream({"model": "m", "messages": []}))
    assert "rejected" in str(err.value)
    with pytest.raises(ValueError):
        Claude("")


# ---- the loop -----------------------------------------------------------------------------------------
def test_simple_answer_streams_and_is_remembered(tmp_path):
    script = Script(say("Ten past four."), say("Still ten past four."))
    agent, _ = make_agent(tmp_path, script)
    ev = Recorder()
    session = agent.store.session_for("voice", "test", 30)
    turn = run(agent, "Argus, what time is it?", ev, session=session)
    assert turn.text == "Ten past four." and "".join(ev.deltas) == "Ten past four."
    run(agent, "and now?", session=session)
    second = script.bodies[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]       # history came along
    assert "Ten past four." in json.dumps(second[1])
    body = script.bodies[0]
    assert body["model"] == "claude-sonnet-5-5" and body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Sandeep" in body["system"][0]["text"] and "spoken" in body["system"][1]["text"]
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert turn.cost > 0


def test_tool_call_runs_and_result_goes_back(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_NEWS_OFFLINE", str(FIXTURES / "google_news.xml"))
    script = Script(call("get_news", {"count": 2}, text="Checking. "), say("Top story: the storm."))
    agent, _ = make_agent(tmp_path, script)
    ev = Recorder()
    turn = run(agent, "news?", ev)
    assert turn.text == "Top story: the storm." and ev.started == ["get_news"]
    result = script.bodies[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and "Storm closes Bay Area highways" in result["content"]


def test_risky_command_needs_approval_and_deny_is_respected(tmp_path):
    target = tmp_path / "workspace" / "precious"
    target.mkdir(parents=True)
    script = Script(call("run_command", {"command": f"rm -rf {target}"}), say("Okay, left it alone."))
    agent, _ = make_agent(tmp_path, script)
    ev = Recorder([DENY])
    run(agent, "delete it", ev)
    assert len(ev.requests) == 1 and "deleting files" in ev.requests[0].description
    assert target.exists()
    result = script.bodies[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "declined" in result["content"]


def test_task_scope_approval_covers_the_rest_of_the_task(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    for n in ("a", "b"):
        (ws / n).write_text("x")
    script = Script(call("run_command", {"command": "rm a"}), call("run_command", {"command": "rm b"}), say("Both gone."),
                    call("run_command", {"command": "rm c"}), say("Gone."))
    agent, _ = make_agent(tmp_path, script)
    ev = Recorder([TASK])
    run(agent, "delete a and b", ev)
    assert len(ev.requests) == 1                 # the second rm didn't ask again
    assert not (ws / "a").exists() and not (ws / "b").exists()
    ev2 = Recorder([ONCE])
    (ws / "c").write_text("x")
    run(agent, "delete c", ev2)                    # a new task asks again
    assert len(ev2.requests) == 1


def test_blocked_actions_never_run(tmp_path):
    script = Script(call("run_command", {"command": "cat ~/.argus/secrets.env"}),
                    call("fetch_page", {"url": "https://www.mybank.com/statement"}), say("Can't do that."))
    s = make_settings(tmp_path, blocked_sites=["mybank.com"])
    agent, _ = make_agent(tmp_path, script, settings=s)
    ev = Recorder()
    run(agent, "show me secrets and my bank", ev)
    assert ev.requests == [] and ev.started == []
    r1 = script.bodies[1]["messages"][-1]["content"][0]
    r2 = script.bodies[2]["messages"][-1]["content"][0]
    assert r1["is_error"] and "secrets" in r1["content"]
    assert r2["is_error"] and "never-touch" in r2["content"]
    log = (tmp_path / "audit.jsonl").read_text()
    assert log.count('"tool_blocked"') == 2


class FakeBackend:
    def __init__(self):
        self.calls = []

    def screen_size(self):
        return (1470, 956)

    def capture(self, region, size):
        self.calls.append(("capture", region, size))
        return b"\x89PNG fake"

    def click(self, x, y, button, count, mods):
        self.calls.append(("click", round(x), round(y), button, count, tuple(mods)))

    def cursor(self):
        return (10.0, 10.0)

    def type_text(self, text):
        self.calls.append(("type", text))


def test_screen_use_is_approved_scaled_and_stops_after_a_failure(tmp_path):
    backend = FakeBackend()
    script = Script(call("screenshot", {}, toolset="computer"),
                    ([{"type": "tool_use", "name": "left_click", "toolset_name": "computer", "input": {"coordinate": [640, 400]}},
                      {"type": "tool_use", "name": "left_click", "toolset_name": "computer", "input": {"coordinate": [9999, 1]}},
                      {"type": "tool_use", "name": "type", "toolset_name": "computer", "input": {"text": "hello"}}], "tool_use"),
                    say("Clicked."))
    agent, _ = make_agent(tmp_path, script, computer=Computer(backend))
    ev = Recorder([TASK])
    run(agent, "click the button", ev)
    assert script.bodies[0]["tools"][-1]["type"] == "computer_toolset_20260801"
    shot = script.bodies[1]["messages"][-1]["content"][0]
    assert shot["toolset_name"] == "computer" and shot["content"][0]["type"] == "image"
    assert backend.calls[0] == ("capture", None, (1230, 800))
    results = script.bodies[2]["messages"][-1]["content"]
    assert ("click", 765, 478, "left", 1, ()) in backend.calls          # 640x400 in the 1230x800 shot
    assert results[1]["is_error"] and results[2]["content"] == NOT_EXECUTED
    assert len(ev.requests) == 1                                        # one approval for the click


def test_screen_is_off_limits_when_locked_or_password_app_visible(tmp_path):
    script = Script(call("screenshot", {}, toolset="computer"), say("I can't look right now."))
    agent, _ = make_agent(tmp_path, script, computer=Computer(FakeBackend()),
                          desktop={"locked": False, "front_bundle": "com.apple.Safari", "front_name": "Safari",
                                   "visible_apps": ["1Password", "Safari"]})
    run(agent, "what's on screen?")
    result = script.bodies[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "1Password" in result["content"]


def test_scheduled_tasks_and_the_phone_get_no_or_read_only_screen(tmp_path):
    script = Script(say("hi"), say("hi"))
    agent, _ = make_agent(tmp_path, script, computer=Computer(FakeBackend()))
    run(agent, "hello", channel="scheduler")
    run(agent, "hello", channel="phone")
    assert not any(t.get("type", "").startswith("computer") for t in script.bodies[0]["tools"])
    phone_toolset = [t for t in script.bodies[1]["tools"] if t.get("type", "").startswith("computer")][0]
    assert phone_toolset["configs"]["left_click"] == {"enabled": False} and "screenshot" not in phone_toolset["configs"]
    assert "open_app" not in [t.get("name") for t in script.bodies[0]["tools"]]


def test_cancel_stops_between_steps(tmp_path):
    cancel = threading.Event()

    def responder(body):
        cancel.set()
        return call("get_news", {})

    agent, _ = make_agent(tmp_path, responder)
    turn = run(agent, "news", cancel=cancel)
    assert turn.stopped


def test_step_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_NEWS_OFFLINE", str(FIXTURES / "google_news.xml"))
    agent, _ = make_agent(tmp_path, lambda body: call("get_news", {}), settings=make_settings(tmp_path, max_steps=3))
    turn = run(agent, "loop forever")
    assert turn.error == "stopped after too many steps"


def test_cost_estimates_follow_the_price_list():
    sonnet = request_cost("claude-sonnet-5-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                                                "cache_creation_input_tokens": 1_000_000,
                                                "cache_read_input_tokens": 1_000_000})
    assert sonnet == pytest.approx(2.00 + 10.00 + 2.50 + 0.10)
    assert request_cost("claude-opus-5-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == pytest.approx(24.0)
    assert request_cost("claude-fable-5-2", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == pytest.approx(48.0)
    # Haiku 5.5 is priced by prompt length: over 100k prompt tokens the whole request pays the higher rate.
    assert request_cost("claude-haiku-5-5", {"input_tokens": 100_000}) == pytest.approx(0.01)
    assert request_cost("claude-haiku-5-5", {"input_tokens": 50_000, "cache_read_input_tokens": 60_000}) == \
        pytest.approx(0.025 + 0.003)
    assert request_cost("claude-sonnet-5-5", {"web_search_requests": 3}) == pytest.approx(0.03)
    assert budget_tokens({"input_tokens": 100, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 1000,
                          "output_tokens": 10}) == 260


def test_web_searches_are_counted_from_the_stream():
    acc = StreamAccumulator()
    acc.feed({"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 1}}})
    acc.feed({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
              "usage": {"output_tokens": 9, "server_tool_use": {"web_search_requests": 2}}})
    assert acc.usage == {"input_tokens": 5, "output_tokens": 9, "web_search_requests": 2}


def test_token_budget_stops_an_expensive_request(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_NEWS_OFFLINE", str(FIXTURES / "google_news.xml"))
    agent, _ = make_agent(tmp_path, lambda body: call("get_news", {}),
                          settings=make_settings(tmp_path, max_turn_tokens=2500))
    turn = run(agent, "loop forever")
    assert "token budget" in turn.error
    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    turns = [e for e in entries if e.get("event") == "turn"]
    assert turns and turns[-1]["cost_usd"] > 0


def test_long_conversations_are_compacted(tmp_path):
    def responder(body):
        if not body.get("stream"):
            return say("Summary: talked about the weather.")
        return say("ok " * 50)

    agent, mock = make_agent(tmp_path, responder, settings=make_settings(tmp_path, context_tokens=600))
    session = agent.store.session_for("voice", "test", 30)
    for i in range(8):
        run(agent, f"message {i} " + "words " * 40, session=session)
    history = agent.store.history(session)
    assert "Summary of our conversation" in json.dumps(history[0])
    assert len(history) < 16
    assert any(not r.get("stream") for r in mock.requests)


def test_old_screenshots_are_pruned():
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": [dict(img)]}]}
            for i in range(5)]
    out = prune_images(msgs, keep=2)
    kinds = [m["content"][0]["content"][0]["type"] for m in out]
    assert kinds == ["text", "text", "text", "image", "image"]
    assert msgs[0]["content"][0]["content"][0]["type"] == "image"     # original untouched


def test_repair_closes_dangling_tool_calls():
    msgs = [{"role": "assistant", "content": [{"type": "text", "text": "orphan"}]},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]}]
    fixed = repair(msgs)
    assert fixed[0] == {"role": "user", "content": "hi"}
    assert fixed[-1]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "Interrupted.", "is_error": True}


def test_server_search_blocks_are_echoed_back_untouched(tmp_path):
    search = [{"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "weather"}},
              {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
               "content": [{"type": "web_search_result", "url": "https://w.example", "title": "W", "encrypted_content": "SECRET=="}]},
              {"type": "text", "text": "Sunny."}]
    script = Script((search, "end_turn"), say("Still sunny."))
    agent, _ = make_agent(tmp_path, script, settings=make_settings(tmp_path, web_search=True, blocked_sites=["mybank.com"]))
    session = agent.store.session_for("voice", "t", 30)
    ev = Recorder()
    run(agent, "weather?", ev, session=session)
    run(agent, "and later?", session=session)
    sent = json.dumps(script.bodies[1]["messages"][1])
    assert "SECRET==" in sent and "srvtoolu_1" in sent
    ws = [t for t in script.bodies[0]["tools"] if t.get("name") == "web_search"][0]
    assert ws["type"] == "web_search_20250305" and ws["blocked_domains"] == ["mybank.com"]
    assert ev.started == ["web_search"]


def test_pause_turn_is_continued(tmp_path):
    script = Script(([{"type": "text", "text": "Searching"}], "pause_turn"), say(" Found it."))
    agent, _ = make_agent(tmp_path, script)
    turn = run(agent, "find it")
    assert turn.text == "Found it." and len(script.bodies) == 2


def test_images_from_the_phone_reach_the_model(tmp_path):
    script = Script(say("A cat."))
    agent, _ = make_agent(tmp_path, script)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()
    run(agent, [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}},
                {"type": "text", "text": "what is this?"}], channel="phone")
    assert script.bodies[0]["messages"][-1]["content"][0]["type"] == "image"
