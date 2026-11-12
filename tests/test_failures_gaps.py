"""More negative and error cases. Each one covers a failure path that the coverage report showed no test
reached: odd conversations, broken connections, crashing helpers, and sounds that look like claps but
aren't."""
import base64
import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from argus import llm, toolbox
from argus.agent import Events, repair
from argus.approvals import DENY, Approver, ApprovalRequest
from argus.audit import Audit
from argus.clap import ClapDetector
from argus.config import Settings
from argus.control import ControlServer, send
from argus.cron import Cron
from argus.hearing import Microphone
from argus.listener import Listener
from argus.llm import Claude, LLMError, StreamAccumulator
from argus.policy import ALLOW, ASK, BLOCK, Policy
from argus.scheduler import Scheduler
from argus.store import Store
from argus.toolbox import Context
from argus.voice import RecordingVoice, SentenceBuffer, Speaker
from argus.wake import NameMatcher

from fakes import SPEECH, Script, make_agent, make_argus, make_settings, say, wait_for
from mock_claude import ClaudeMock, rules, validate

MODELS = Path(os.environ.get("ARGUS_TEST_MODELS", "/nonexistent"))


def serve(handler_class):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---- the brain ----------------------------------------------------------------------------------------------
def test_a_channel_that_cant_ask_always_gets_no():
    assert Events().approve(ApprovalRequest(run_id="r", command="rm notes.txt")) == DENY


def test_unfinished_screen_actions_are_answered_the_way_the_api_requires():
    history = [{"role": "user", "content": "click the blue button"},
               {"role": "assistant", "content": [
                   {"type": "text", "text": "Clicking it."},
                   {"type": "tool_use", "id": "s1", "name": "left_click", "toolset_name": "computer",
                    "input": {"coordinate": [10, 20]}},
                   {"type": "tool_use", "id": "t1", "name": "get_news", "input": {}}]}]
    fixed = repair(history)
    results = fixed[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["s1", "t1"] and all(r["is_error"] for r in results)
    assert results[0]["toolset_name"] == "computer" and "toolset_name" not in results[1]
    assert validate({"model": "m", "max_tokens": 5, "messages": fixed}) == ""


def test_keys_are_removed_from_every_kind_of_tool_output(tmp_path):
    agent, _ = make_agent(tmp_path, Script())
    agent.add_secret("phone-token-1234567890")          # the phone token is added once the gateway starts
    agent.add_secret("phone-token-1234567890")
    assert agent.secrets == ["phone-token-1234567890"]
    picture = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    out = agent._redact([{"type": "text", "text": "token phone-token-1234567890 here"}, picture, "odd"])
    assert out == [{"type": "text", "text": "token [secret removed] here"}, picture, "odd"]
    assert agent._redact(None) is None and agent._redact(42) == 42


class StopsMidSentence:
    """A Claude stand-in that streams half a sentence, then the user says "Argus, stop"."""

    def stream(self, body, cancel=None):
        yield {"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 1}}}
        yield {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
        yield {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Opening Safari"}}
        yield {"type": "content_block_start", "index": 1,
               "content_block": {"type": "tool_use", "id": "t1", "name": "open_app", "input": {}}}
        cancel.set()


def test_a_cancelled_reply_keeps_only_what_was_said(tmp_path):
    agent, _ = make_agent(tmp_path, Script())
    agent.llm = StopsMidSentence()
    session = agent.store.session_for("voice", "test", 30)
    turn = agent.run(session_id=session, channel="voice", content="open Safari", events=Events(),
                     cancel=threading.Event())
    assert turn.stopped and not turn.error
    last = agent.store.history(session)[-1]
    assert last == {"role": "assistant", "content": [{"type": "text", "text": "Opening Safari"}]}


def test_a_conversation_with_no_safe_place_to_summarise_is_left_whole(tmp_path):
    agent, mock = make_agent(tmp_path, Script(), settings=make_settings(tmp_path, context_tokens=1000))
    messages = [{"role": "user", "content": "x" * 5000}]
    for i in range(5):            # a long screen task: every later user turn is a tool result
        messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "get_news",
                                                        "input": {}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                                   "content": "y" * 3000}]}]
    assert agent._compact("s", messages) is messages and not mock.requests


# ---- the Claude client --------------------------------------------------------------------------------------
class Replies(BaseHTTPRequestHandler):
    """Answers POST /v1/messages from a script of (status, headers, body) tuples, then a valid reply."""
    script = []
    ok = b'{"id": "m", "type": "message", "content": [], "stop_reason": "end_turn", "usage": {}}'

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        status, headers, body = Replies.script.pop(0) if Replies.script else (200, {}, Replies.ok)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", headers.get("Content-Length", str(len(body))))
        self.end_headers()
        self.wfile.write(body)


def test_a_retry_after_that_isnt_a_number_still_retries(monkeypatch):
    waits = []
    monkeypatch.setattr(llm, "time", SimpleNamespace(sleep=waits.append, monotonic=time.monotonic))
    Replies.script = [(429, {"Retry-After": "soon"}, b'{"error": {"type": "rate_limit_error", "message": "slow down"}}')]
    server = serve(Replies)
    try:
        reply = Claude("k", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=2).create(
            {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
    finally:
        server.shutdown()
    assert reply["stop_reason"] == "end_turn" and len(waits) == 1 and 0.5 <= waits[0] <= 0.75   # the quick first retry


@pytest.mark.parametrize("status, headers, body, why", [
    (200, {}, b"<html>proxy error</html>", "couldn't read"),
    (200, {}, b"[1, 2, 3]", "couldn't read"),
    (200, {"Content-Length": "500"}, b'{"id": "m", "content"', "lost the connection"),
])
def test_unreadable_or_cut_off_replies_are_clear_errors(status, headers, body, why):
    Replies.script = [(status, headers, body)]
    server = serve(Replies)
    try:
        with pytest.raises(LLMError, match=why):
            Claude("k", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=0).create(
                {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
    finally:
        server.shutdown()


def test_summarising_with_an_unreadable_reply_keeps_the_conversation(tmp_path):
    agent, _ = make_agent(tmp_path, Script(), settings=make_settings(tmp_path, context_tokens=1000))
    Replies.script = [(200, {}, b"not json at all")]
    server = serve(Replies)
    agent.llm = Claude("k", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=0)
    messages = [{"role": "user", "content": f"question {i} " + "x" * 2000} if i % 2 == 0 else
                {"role": "assistant", "content": [{"type": "text", "text": f"answer {i}"}]} for i in range(12)]
    try:
        assert agent._compact("s", messages) is messages
    finally:
        server.shutdown()


def test_a_dropped_connection_is_retried_before_the_reply_starts_and_reported_after(monkeypatch):
    closed, waits = [], []
    monkeypatch.setattr(llm, "time", SimpleNamespace(sleep=waits.append, monotonic=time.monotonic))

    class Resp:
        def __init__(self, lines):
            self.lines = list(lines)

        def readline(self):
            item = self.lines.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    class Conn:
        sock = None

        def close(self):
            closed.append(True)

    start = b'data: {"type": "message_start", "message": {"id": "m", "usage": {}}}\n'
    replies = iter([Resp([ConnectionResetError("reset by peer")]),                  # before anything arrived
                    Resp([start, b"\n", ConnectionResetError("reset again")])])    # halfway through
    client = Claude("k", base_url="http://127.0.0.1:9", max_retries=2)
    monkeypatch.setattr(client, "_open", lambda body, stream, cancel=None: (Conn(), next(replies), threading.Event()))
    events = []
    with pytest.raises(LLMError, match="lost the connection") as info:
        for event in client.stream({"model": "m", "messages": []}):
            events.append(event)
    assert [e["type"] for e in events] == ["message_start"]
    assert info.value.partial and info.value.retryable                # the caller picks up from there
    assert len(waits) == 1 and closed == [True, True]                  # one quick retry, both connections closed

def test_accumulator_keeps_citations_thinking_and_signatures():
    acc = StreamAccumulator()
    citation = {"type": "web_search_result_location", "url": "https://weather.example", "title": "Weather",
                "encrypted_index": "e1", "cited_text": "Rain after 3pm."}
    for event in [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Check "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "the rain."}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig=="}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": "", "citations": None}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "citations_delta", "citation": citation}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Rain after 3."}},
        {"type": "content_block_stop", "index": 1},
    ]:
        acc.feed(event)
    thinking, text = acc.content
    assert thinking == {"type": "thinking", "thinking": "Check the rain.", "signature": "sig=="}
    assert text == {"type": "text", "text": "Rain after 3.", "citations": [citation]}


@pytest.mark.parametrize("host, no_proxy, bypass", [
    ("api.anthropic.com", "anthropic.com", True), ("api.anthropic.com", ".anthropic.com", True),
    ("anthropic.com", "anthropic.com", True), ("notanthropic.com", "anthropic.com", False),
    ("api.anthropic.com", "*", True), ("api.anthropic.com", "localhost, 127.0.0.1", False),
    ("api.anthropic.com", "api.anthropic.com:443", True), ("api.anthropic.com", "", False)])
def test_no_proxy_rules_match_whole_domains_only(host, no_proxy, bypass):
    assert llm.bypasses_proxy(host, no_proxy) is bypass


# ---- the rules -----------------------------------------------------------------------------------------------
def test_rules_refuse_odd_requests(tmp_path):
    state = {"locked": False, "front_bundle": "com.apple.Safari", "front_name": "Safari", "visible_apps": []}
    policy = Policy(Settings(), workspace=tmp_path, home=tmp_path, desktop_state=lambda: dict(state))
    assert policy.check("glasses_look", {}, "scheduler").action == BLOCK      # never on its own
    assert policy.check("glasses_look", {}, "voice").action == ALLOW
    assert policy.check("run_command", {"command": "   "}, "voice").action == BLOCK
    assert policy.check("teleport", {}, "voice", "computer").action == BLOCK  # not a screen action Argus knows
    assert policy.check("type", {"text": "hello"}, "voice", "computer").action == ASK
    state["screen_private"] = True                                            # "screen access" switched off
    assert policy.check("screenshot", {}, "voice", "computer").action == BLOCK


def test_an_approver_that_crashes_or_answers_oddly_means_no():
    class Broken(Approver):
        def decide(self, request):
            raise RuntimeError("the dialog crashed")

    class Odd(Approver):
        def decide(self, request):
            return "always"

    request = ApprovalRequest(run_id="r", command="rm notes.txt")
    assert Broken().ask(request) == DENY and Odd().ask(request) == DENY
    with pytest.raises(NotImplementedError):
        Approver().decide(request)


# ---- tools ----------------------------------------------------------------------------------------------------
@pytest.fixture
def ctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return Context(channel="voice", workspace=ws, store=Store(tmp_path / "db.sqlite"))


def test_search_stops_after_100_hits_and_when_cancelled(ctx):
    for folder in ("a", "b"):
        (ctx.workspace / folder).mkdir()
        for i in range(80):
            (ctx.workspace / folder / f"{folder}{i}.txt").write_text("needle\n")
    (ctx.workspace / "needle-notes.md").write_text("nothing to see\n")
    lines = toolbox.search_files({"path": str(ctx.workspace), "pattern": "needle"}, ctx).content.splitlines()
    assert len(lines) == 100 and any(line.endswith("(name matches)") for line in lines)
    ctx.cancel.set()
    assert toolbox.search_files({"path": str(ctx.workspace), "pattern": "needle"}, ctx).content == "no matches"


def test_opening_apps_and_links_reports_every_failure(ctx, monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["open", "-a", "Nonexistent"]:
            return subprocess.CompletedProcess(cmd, 1, "", "Unable to find application named 'Nonexistent'")
        return subprocess.CompletedProcess(cmd, 0 if cmd[1] == "-a" else 1, "", "")
    monkeypatch.setattr(toolbox.subprocess, "run", fake_run)
    assert toolbox.open_app({"name": "Safari"}, ctx).content == "opened Safari"
    out = toolbox.open_app({"name": "Nonexistent"}, ctx)
    assert out.is_error and "Unable to find" in out.content
    out = toolbox.open_url({"url": "https://example.com"}, ctx)
    assert out.is_error and out.content == "couldn't open that link"     # never an empty error

    def hangs(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 20)
    monkeypatch.setattr(toolbox.subprocess, "run", hangs)
    assert "couldn't open Safari" in toolbox.open_app({"name": "Safari"}, ctx).content
    assert toolbox.open_url({"url": "https://example.com"}, ctx).is_error

    def missing(cmd, **kwargs):
        raise FileNotFoundError("open")
    monkeypatch.setattr(toolbox.subprocess, "run", missing)
    assert toolbox.open_app({"name": "Safari"}, ctx).is_error
    assert toolbox.describe_call("open_app", {"name": "Notes"}) == "open Notes"


def test_a_glasses_photo_comes_back_as_a_picture(ctx):
    jpeg = b"\xff\xd8\xff\xe0 pretend this is a photo"
    ctx.glasses = SimpleNamespace(capture=lambda timeout, cancel: jpeg)
    out = toolbox.glasses_look({}, ctx)
    assert not out.is_error and out.content[1]["source"]["data"] == base64.b64encode(jpeg).decode()


def test_fetch_keeps_the_query_string():
    seen = []

    class Page(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"hello")

    server = serve(Page)
    try:
        final, ctype, data = toolbox.fetch_url(f"http://127.0.0.1:{server.server_port}/search?q=rain&lang=en",
                                               allow_private=True)
    finally:
        server.shutdown()
    assert seen == ["/search?q=rain&lang=en"] and data == b"hello" and ctype.startswith("text/plain")


def test_a_command_whose_process_group_is_already_gone_still_times_out(ctx, monkeypatch):
    def gone(pid, sig):
        raise ProcessLookupError(pid)
    real_killpg = os.killpg
    monkeypatch.setattr(toolbox.os, "killpg", gone)
    try:
        out = toolbox.run_command({"command": "sleep 3", "timeout_seconds": 1}, ctx)
    finally:
        monkeypatch.setattr(toolbox.os, "killpg", real_killpg)
    assert out.is_error and "timed out after 1s" in out.content


def test_cron_start_and_step():
    cron = Cron("5/15 * * * *", "UTC")
    t = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc).timestamp()
    minutes = []
    for _ in range(5):
        t = cron.next_after(t)
        minutes.append(datetime.fromtimestamp(t, timezone.utc).minute)
    assert minutes == [5, 20, 35, 50, 5]


# ---- the iPhone gateway and the control socket -------------------------------------------------------------
def test_tailscale_address_skips_junk_lines(monkeypatch):
    from argus import phone
    outputs = iter(["", "garbage\nnot-an-ip 10.0.0.5\n100.101.102.103\n"])
    monkeypatch.setattr(phone.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, next(outputs, ""), ""))
    assert phone.tailscale_ip() == "100.101.102.103"


def test_control_socket_survives_hangups_floods_and_silence(tmp_path):
    path = tmp_path / "c.sock"
    server = ControlServer(path, {"status": lambda req: {"ok": True}})
    server.start()
    try:
        with socket.socket(socket.AF_UNIX) as s:          # connects and leaves without a word
            s.connect(str(path))
        with socket.socket(socket.AF_UNIX) as s:          # 70 kB without a newline: refused, not stuck
            s.connect(str(path))
            s.sendall(b"x" * 70_000)
            s.settimeout(5)
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                reply += chunk
        assert b'"ok": false' in reply
        assert send(path, "status") == {"ok": True}
    finally:
        server.stop()
        server.stop()                                     # stopping twice is harmless
    assert not path.exists()

    mute_path = tmp_path / "mute.sock"
    mute = socket.socket(socket.AF_UNIX)
    mute.bind(str(mute_path))
    mute.listen(1)

    def hang_up():
        conn, _ = mute.accept()
        conn.recv(4096)
        conn.close()
    threading.Thread(target=hang_up, daemon=True).start()
    try:
        assert send(mute_path, "status", timeout=5) == {}   # an empty answer, not an exception
    finally:
        mute.close()


# ---- listening -------------------------------------------------------------------------------------------------
def test_just_the_name_gets_a_yes_and_opens_a_follow_up(tmp_path):
    argus, voice, script = make_argus(tmp_path, ["Argus.", "what's on my calendar"], Script(say("Nothing today.")))
    argus.handle_utterance(SPEECH)
    assert wait_for(lambda: voice.spoken[:1] == ["Yes?"])
    assert wait_for(argus.listener.in_followup)
    argus.speaker._last_end -= 5                 # the person answers a moment after the "Yes?"
    argus.handle_utterance(SPEECH)
    argus.brain.wait(5)
    assert script.bodies[0]["messages"][-1]["content"][0]["text"] == "what's on my calendar"


def test_speaking_over_a_task_replaces_it(tmp_path):
    gate = threading.Event()
    seen = []

    def responder(body):
        seen.append(body)
        if len(seen) == 1:
            gate.wait(5)
            return say("Here is your whole week.")
        return say("It's four o'clock.")
    argus, voice, _ = make_argus(tmp_path, ["Argus, plan my whole week", "Argus, actually what time is it"],
                                 responder)
    try:
        argus.handle_utterance(SPEECH)
        assert wait_for(lambda: argus.brain.busy and seen)
        argus.handle_utterance(SPEECH)
    finally:
        gate.set()
    assert wait_for(lambda: "It's four o'clock." in voice.spoken)
    assert "Here is your whole week." not in voice.spoken


def test_the_loop_waits_while_the_microphone_is_off_and_stops_on_request(tmp_path):
    argus, _, _ = make_argus(tmp_path, [])
    argus.set_mic(False)
    loop = threading.Thread(target=argus.run, daemon=True)
    loop.start()
    time.sleep(0.5)
    assert loop.is_alive()
    argus.stop()
    loop.join(3)
    assert not loop.is_alive()


def test_the_last_sentence_of_a_recording_is_not_lost(tmp_path):
    argus, _, script = make_argus(tmp_path, ["Argus, what's the weather"], Script(say("Sunny.")))
    argus.source.finished = True
    argus.segmenter.flush = lambda: [SPEECH]
    argus.run()
    argus.brain.wait(5)
    assert script.bodies


def test_listener_modes_and_conversations_that_keep_going():
    now = [1000.0]
    listener = Listener(NameMatcher("argus"), followup_seconds=8, conversation_idle_seconds=120,
                        clock=lambda: now[0])
    listener.reply_finished()
    assert listener.mode() == "follow-up"
    now[0] += 9
    assert listener.mode() == "listening"
    listener.set_conversation(True)
    now[0] += 100
    listener.reply_finished()                    # each reply restarts the two quiet minutes
    now[0] += 100
    assert listener.mode() == "conversation"
    now[0] += 30
    assert listener.mode() == "listening"


def test_the_microphone_callback_never_raises(monkeypatch):
    import queue
    mic = Microphone(block=512, max_backlog_seconds=512 * 2 / 16000)      # room for two blocks
    block = np.zeros((512, 1), dtype=np.float32)
    for i in range(3):                             # the third block pushes out the oldest
        mic._callback(block + i, 512, None, "input overflow" if i == 2 else None)
    assert mic.overflows == 1 and [mic.read()[0], mic.read()[0]] == [1.0, 2.0] and mic.read(0.01) is None

    class Jammed:
        def put_nowait(self, item):
            raise queue.Full

        def get_nowait(self):
            raise queue.Empty
    mic._queue = Jammed()
    mic._callback(block, 512, None, None)          # PortAudio's thread must never see an exception


@pytest.mark.skipif(not (MODELS / "wespeaker_en_voxceleb_resnet34_LM.onnx").exists(), reason="speaker model missing")
def test_a_saved_voiceprint_is_loaded_and_used(tmp_path):
    from argus.hearing import VoicePrint
    model = str(MODELS / "wespeaker_en_voxceleb_resnet34_LM.onnx")
    rng = np.random.default_rng(3)
    ref = rng.standard_normal(256).astype(np.float64)
    np.save(tmp_path / "vp.npy", ref / np.linalg.norm(ref))
    vp = VoicePrint(model, str(tmp_path / "vp.npy"), threshold=0.99)
    assert vp.enrolled and vp._ref.dtype == np.float32
    assert vp.matches(rng.standard_normal(16000).astype(np.float32) * 0.1) is False   # noise isn't you


# ---- the voice ---------------------------------------------------------------------------------------------------
def test_sentences_end_at_line_breaks():
    buf = SentenceBuffer()
    assert buf.feed("First line\nSecond") == ["First line"]
    assert buf.flush() == ["Second"]


def test_a_sentence_queued_before_stop_is_never_spoken():
    voice = RecordingVoice()
    speaker = Speaker(voice)
    speaker._idle.clear()
    speaker._queue.put((speaker._generation - 1, "From before the stop."))
    assert speaker.wait_idle(2) and voice.spoken == []


def test_voices_that_need_a_reset_get_one_before_each_sentence():
    calls = []

    class Resettable(RecordingVoice):
        def reset(self):
            calls.append("reset")

        def speak(self, text):
            calls.append(text)
    speaker = Speaker(Resettable())
    speaker.say("Hello there.")
    assert wait_for(lambda: calls == ["reset", "Hello there."])


@pytest.mark.skipif(not (MODELS / "vits-piper-en_US-ryan-medium").exists(), reason="voice model missing")
def test_piper_with_nothing_to_say_plays_nothing_and_cleans_up_anyway(tmp_path, monkeypatch):
    from argus import voice as voice_mod
    piper = voice_mod.PiperVoice(str(MODELS / "vits-piper-en_US-ryan-medium"), tmp_dir=str(tmp_path))
    played = []
    monkeypatch.setattr(piper, "_run", played.append)
    monkeypatch.setattr(piper, "synthesize", lambda text: (np.zeros(0, np.float32), 22050))
    piper.speak("...")
    assert played == []
    monkeypatch.setattr(piper, "synthesize", lambda text: (np.zeros(2205, np.float32), 22050))

    def unlink_fails(path):
        raise PermissionError(path)
    monkeypatch.setattr(voice_mod.os, "unlink", unlink_fails)
    piper.speak("Hello.")                         # a temp file that can't be removed isn't an error
    assert len(played) == 1


# ---- claps that aren't --------------------------------------------------------------------------------------------
RNG = np.random.default_rng(11)


def clap():
    t = np.arange(int(0.08 * 16000))
    return (RNG.standard_normal(len(t)) * 0.5 * np.exp(-t / 192)).astype(np.float32)


def quiet(seconds):
    return (RNG.standard_normal(int(seconds * 16000)) * 0.002).astype(np.float32)


def fires(audio, on_clap=None):
    detector = ClapDetector("double", on_clap=on_clap)
    return sum(detector.feed(audio[i:i + 512]) for i in range(0, len(audio), 512))


def test_a_double_clap_calls_back_once():
    heard = []
    assert fires(np.concatenate([quiet(1), clap(), quiet(0.35), clap(), quiet(1)]), lambda: heard.append(1)) == 1
    assert heard == [1]


def test_a_click_followed_by_a_voice_is_not_a_clap():
    # A "p" or "k" is sharp like a clap, but a voice carries on right after it.
    voice = (RNG.standard_normal(int(0.2 * 16000)) * 0.3).astype(np.float32)
    audio = np.concatenate([quiet(1), clap()[:300], quiet(0.03), voice, quiet(0.4), clap(), quiet(1)])
    assert fires(audio) == 0


def test_a_slam_that_keeps_ringing_is_not_a_clap():
    slam = (RNG.standard_normal(int(0.5 * 16000)) * 0.5).astype(np.float32)
    assert fires(np.concatenate([quiet(1), slam, quiet(0.35), clap(), quiet(1)])) == 0


def test_claps_right_after_talking_or_music_dont_count():
    t = np.arange(int(0.25 * 16000)) / 16000
    hum = (np.sin(2 * np.pi * 200 * t) * 0.08 * np.minimum(1, t / 0.1)).astype(np.float32)   # fades in
    assert fires(np.concatenate([quiet(1), hum, quiet(0.1), clap(), quiet(0.35), clap(), quiet(1)])) == 0


# ---- argusctl -----------------------------------------------------------------------------------------------------
@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    from argus import config as config_mod
    home = tmp_path / "argus-home"
    monkeypatch.setenv("ARGUS_HOME", str(home))
    mock = ClaudeMock(rules).start()
    home.mkdir(mode=0o700)
    (home / "config.toml").write_text(f'[brain]\napi_base = "{mock.url}"\n[telegram]\nenabled = false\n'
                                      '[listening]\nvoice_lock = true\n')
    secrets = home / "secrets.env"
    secrets.write_text("ANTHROPIC_API_KEY=sk-ant-test\n")
    secrets.chmod(0o600)
    settings = config_mod.load()
    config_mod.ensure_private_dirs(settings)
    server = ControlServer(settings.socket_path, {
        "status": lambda req: {"mode": "listening", "voice_lock": False},
        "permissions": lambda req: {"ok": True, "screen_recording": True, "accessibility": True}})
    server.start()
    yield SimpleNamespace(home=home, mock=mock, secrets=secrets)
    server.stop()
    mock.shutdown()


def doctor(capsys):
    from argus.__main__ import main
    code = main(["doctor"])
    return code, capsys.readouterr().out


def test_doctor_asks_claude_for_real_and_says_what_is_wrong(cli_home, capsys):
    code, out = doctor(capsys)
    assert "PASS  Claude API answers" in out
    assert "FAIL  voice lock is switched on, but there's no usable voiceprint" in out
    assert "PASS  Accessibility allowed" in out
    cli_home.mock.key = "sk-ant-a-different-key"           # the saved key is no longer valid
    code, out = doctor(capsys)
    assert code == 1 and "FAIL  Claude API: Claude API error 401: the Claude API key was rejected" in out
    cli_home.secrets.chmod(0o644)                          # readable by others: refuse to use it
    code, out = doctor(capsys)
    assert code == 1 and "chmod 600" in out and "Claude API answers" not in out


def test_enroll_learns_how_the_name_is_misheard_and_nothing_else(cli_home, monkeypatch, capsys):
    from argus import __main__ as cli
    from argus import config as config_mod
    from argus import hearing
    heard = ["Markus, what's the weather like today?",          # the name misheard: learn it
             "open my calendar and check tomorrow",             # the name dropped: learn nothing ("open"!)
             "Markus, play some music in the kitchen.",         # "Hey" dropped too: still finds it
             "Angus, remind me to call home at six.",           # already counts: "Angus," at the start
             "Argus, what's on my screen right now?"]           # heard right

    class Recogniser:
        def __init__(self, model_dir, threads=2):
            pass

        def transcribe(self, samples):
            return heard[int(samples[0])]
    monkeypatch.setattr(cli, "_record", lambda sentences: [(s, np.full(16000, i, np.float32))
                                                            for i, s in enumerate(sentences)])
    monkeypatch.setattr(hearing, "SherpaTranscriber", Recogniser)
    learned = config_mod.load().learned_variants_path
    learned.write_text("argos\n")
    code = cli.main(["enroll", "--no-voiceprint"])
    out = capsys.readouterr().out
    assert code == 0 and "Learned how you say the name: markus" in out and "Name recognised in 2 of 5" in out
    assert learned.read_text().split() == ["argos", "markus"]


@pytest.mark.parametrize("prompt, heard, expected", [
    ("Argus, what's the weather?", "Markus, what's the weather?", "markus"),
    ("Hey Argus, play music.", "Markus, play music.", "markus"),            # a word dropped before the name
    ("Hey Argus, play music.", "hey hey Markus play music", "markus"),      # ...or one added
    ("Argus, open my calendar.", "open my calendar", None),                 # never "open"
    ("Hey Argus, play music.", "hey what's play music", None),              # never "what's" or "play"
    ("Argus, remind me.", "August, remind me.", None),                      # a real word stays a word
    ("Open the door.", "Open the door.", None),                             # the name wasn't in the prompt
])
def test_only_words_that_sound_like_the_name_are_learned(prompt, heard, expected):
    from argus.wake import misheard_name
    assert misheard_name(prompt, heard, "Argus") == expected


# ---- screen actions no other test performs --------------------------------------------------------------------
class FakeScreen:
    """A 2560x1600 display (screenshots are 1280x800) that records what Argus asks it to do."""

    def __init__(self):
        self.done = []
        self.where = (100.0, 200.0)

    def screen_size(self):
        return 2560, 1600

    def capture(self, region, size):
        self.done.append(("capture", region, size))
        return b"\x89PNG fake"

    def move(self, x, y):
        self.done.append(("move", x, y))

    def click(self, x, y, button, count, mods):
        self.done.append(("click", x, y, button, count, tuple(mods)))

    def button(self, x, y, down):
        self.done.append(("button", x, y, down))

    def drag(self, start, end, mods):
        self.done.append(("drag", start, end, tuple(mods)))

    def scroll(self, x, y, dx, dy, mods):
        self.done.append(("scroll", x, y, dx, dy))

    def type_text(self, text):
        self.done.append(("type", text))

    def key(self, mods, key, hold=0.0):
        self.done.append(("key", tuple(mods), key, hold))

    def cursor(self):
        return self.where


def test_every_screen_action_and_its_bad_inputs(monkeypatch):
    from argus import computer as computer_mod
    slept = []
    monkeypatch.setattr(computer_mod, "time", SimpleNamespace(sleep=slept.append))
    screen = FakeScreen()
    c = computer_mod.Computer(screen)
    ok = lambda member, args: c.run(member, args).content == "OK"         # noqa: E731
    assert ok("mouse_move", {"coordinate": [640, 400]}) and screen.done[-1] == ("move", 1280.0, 800.0)
    assert ok("left_click_drag", {"start_coordinate": [0, 0], "coordinate": [10, 10], "text": "shift"})
    assert screen.done[-1] == ("drag", (0.0, 0.0), (20.0, 20.0), ("shift",))
    assert ok("left_mouse_down", {}) and ok("left_mouse_up", {})
    assert screen.done[-2:] == [("button", 100.0, 200.0, True), ("button", 100.0, 200.0, False)]
    assert ok("scroll", {"scroll_direction": "left", "scroll_amount": 999}) and screen.done[-1][3] == 50
    assert ok("type", {"text": "héllo 👋"}) and screen.done[-1] == ("type", "héllo 👋")
    assert ok("hold_key", {"text": "shift", "duration": 9999}) and screen.done[-1][3] == 300
    assert ok("key", {"text": "cmd+c", "repeat": 1000}) and sum(d[0] == "key" for d in screen.done) == 101
    assert ok("wait", {"duration": -5}) and ok("wait", {"duration": 4000}) and slept == [0.0, 300]
    assert c.run("zoom", {"region": [0, 0, 640, 400]}).content[0]["type"] == "image"
    assert screen.done[-1][1] == (0, 0, 1280, 800)
    for member, args, why in [
            ("mouse_move", {"coordinate": [5000, 5]}, "outside the 1280x800 screenshot"),
            ("mouse_move", {}, "coordinate must be [x, y]"),
            ("left_click_drag", {"coordinate": [10, 10]}, "coordinate must be [x, y]"),
            ("zoom", {"region": [0, 0, 640]}, "region must be [x0, y0, x1, y1]"),
            ("zoom", {"region": "all"}, "region must be [x0, y0, x1, y1]"),
            ("key", {"text": "cmd+banana"}, "unknown key"),
            ("wait", {"duration": "soon"}, "could not convert")]:
        out = c.run(member, args)
        assert out.is_error and why in out.content, (member, out.content)


# ---- small things that fail quietly -------------------------------------------------------------------------
def test_the_gateway_ping_and_a_bad_glasses_status(tmp_path):
    import http.client
    import json as json_mod
    from argus.glasses import GlassesBridge
    from argus.phone import Gatekeeper, PhoneServer
    server = PhoneServer(("127.0.0.1", 0), lambda content: "ok", Gatekeeper("phone-secret-token"),
                         Audit(tmp_path / "audit.jsonl"), GlassesBridge())
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def request(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        conn.request(method, path, body=body, headers={"Authorization": "Bearer phone-secret-token",
                                                       "Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json_mod.loads(resp.read() or b"{}")
    try:
        assert request("GET", "/ping") == (200, {"ok": True})
        assert request("POST", "/glasses/status", b"[true]")[0] == 400
        assert not server.glasses.connected
    finally:
        server.shutdown()


def test_a_scheduled_job_that_fails_and_cant_even_report_it_is_retried_next_time(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_task("check the garden cameras", "task", cron="*/5 * * * *", next_run=time.time() - 1)

    def deliver(text):
        raise OSError("Telegram and the speaker are both down")

    def run_task(text):
        raise RuntimeError("Claude unreachable")
    scheduler = Scheduler(store, deliver=deliver, run_task=run_task, headlines=lambda: "")
    scheduler.tick()                                       # must not raise
    task = store.tasks()[0]
    assert task["next_run"] > time.time()                  # the next run is still planned


def test_glasses_that_are_off_are_described_as_off():
    from argus.glasses import GlassesBridge
    bridge = GlassesBridge(clock=lambda: 1000.0)
    bridge.update_status(False, "Ray-Ban Meta")
    assert bridge.describe() == "not connected right now"


def test_the_privacy_guard_survives_a_crashing_lock_handler_and_poll(tmp_path):
    from argus.guard import Guard
    states = iter([True, RuntimeError("Quartz went away"), False])

    def locked():
        value = next(states)
        if isinstance(value, Exception):
            raise value
        return value

    def handler(is_locked):
        raise RuntimeError("the microphone wouldn't close")
    guard = Guard(tmp_path / "state.json", handler, interval=0.05, locked_fn=locked,
                  front_fn=lambda: ("com.apple.Safari", "Safari"))
    guard.start()
    assert wait_for(lambda: guard.locked is False, timeout=5)        # kept polling through both failures
    guard.stop()
