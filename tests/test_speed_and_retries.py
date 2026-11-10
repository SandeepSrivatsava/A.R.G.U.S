"""Speed and second tries: kept-open connections, warming up while you talk, short time limits that retry,
"Argus, stop" cutting a slow request, replies that break off and pick up again, and slow feeds or pages."""
import json
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import numpy as np
import pytest

from argus import config as config_mod
from argus import llm, news, toolbox
from argus.agent import RESUME_NOTE, Events
from argus.llm import Claude, LLMError
from argus.store import Store
from argus.toolbox import Context
from argus.voice import SentenceBuffer

from fakes import Script, make_agent, make_argus, wait_for
from mock_claude import validate

BODY = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
EVENTS = [
    {"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 3, "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi."}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
    {"type": "message_stop"},
]


class KeepAlive(ThreadingHTTPServer):
    """A stand-in Claude API that speaks HTTP/1.1 with keep-alive (like the real one) and counts connections.
    mode: "ok"; "hang" (never answers); "hang_first" (the first request never answers);
    "stall_first" (the first stream starts, then goes silent); "busy_first" (the first answer is 529 Overloaded);
    "cut_first" (the first answer breaks off); "refuse" (every stream starts with an invalid-request error)."""
    daemon_threads = True

    def __init__(self, mode="ok"):
        super().__init__(("127.0.0.1", 0), _KeepAliveHandler)
        self.mode, self.connections, self.requests = mode, 0, 0
        self.release = threading.Event()
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"

    def handle_error(self, request, client_address):
        pass

    def stop(self):
        self.release.set()
        self.shutdown()


class _KeepAliveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: KeepAlive

    def log_message(self, *a):
        pass

    def setup(self):
        super().setup()
        self.server.connections += 1

    def _chunk(self, data: bytes):
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.server.requests += 1
        first = self.server.requests == 1
        if self.server.mode == "hang" or (self.server.mode == "hang_first" and first):
            self.server.release.wait(15)
            self.close_connection = True
            return
        if self.server.mode == "busy_first" and first:
            data = b'{"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}'
            self.send_response(529)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.server.mode == "cut_first" and first:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "500")
            self.end_headers()
            self.wfile.write(b'{"id": "m", "con')
            self.close_connection = True
            return
        if not body.get("stream"):
            data = json.dumps({"id": "m", "type": "message", "content": [{"type": "text", "text": "Hi."}],
                               "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 2}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if self.server.mode == "stall_first" and first:
            self.wfile.flush()
            self.server.release.wait(15)
            self.close_connection = True
            return
        events = EVENTS
        if self.server.mode == "refuse":
            events = [{"type": "error", "error": {"type": "invalid_request_error", "message": "bad request"}}]
        for event in events:
            self._chunk(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        self.wfile.write(b"0\r\n\r\n")


@pytest.fixture
def no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(llm, "time", SimpleNamespace(sleep=waits.append, monotonic=time.monotonic))
    return waits


# ---- the connection to Claude --------------------------------------------------------------------------------
def test_one_connection_is_reused_for_every_request():
    server = KeepAlive()
    try:
        client = Claude("k", base_url=server.url)
        for _ in range(3):
            assert client.create(BODY)["stop_reason"] == "end_turn"
        for _ in range(2):
            assert [e["type"] for e in client.stream(BODY)][-1] == "message_stop"
        assert client.connections_opened == 1 and server.connections == 1    # no new handshake per request
    finally:
        server.stop()


def test_a_connection_that_was_dropped_or_is_too_old_is_replaced_without_a_wait(no_sleep):
    server = KeepAlive()
    try:
        client = Claude("k", base_url=server.url)
        client.create(BODY)
        client._idle[0][1].sock.close()                  # the server (or a sleeping Mac) dropped it
        assert client.create(BODY)["stop_reason"] == "end_turn"
        assert client.connections_opened == 2 and no_sleep == []
        since, conn = client._idle[0]
        client._idle[0] = (since - 1000, conn)           # idle for ages: not trusted
        client.create(BODY)
        assert client.connections_opened == 3 and no_sleep == []
    finally:
        server.stop()


def test_warming_up_opens_one_connection_ahead_of_the_request():
    server = KeepAlive()
    try:
        client = Claude("k", base_url=server.url)
        client.warm()
        assert wait_for(lambda: len(client._idle) == 1)
        client.warm()                                    # a fresh one is already waiting: nothing to do
        time.sleep(0.2)
        assert client.connections_opened == 1 and server.requests == 0    # nothing was sent
        client.create(BODY)
        assert client.connections_opened == 1 and server.connections == 1
    finally:
        server.stop()


def test_warming_up_when_the_internet_is_down_is_harmless():
    client = Claude("k", base_url="http://127.0.0.1:9", connect_timeout=0.5)
    client.warm()
    assert wait_for(lambda: not client._warming) and client._idle == []


def test_stop_cuts_a_request_that_is_still_waiting_for_claude():
    server = KeepAlive("hang")
    try:
        client = Claude("k", base_url=server.url, first_byte_timeout=30)
        cancel, result = threading.Event(), {}

        def ask():
            started = time.monotonic()
            result["events"] = list(client.stream(BODY, cancel))
            result["took"] = time.monotonic() - started
        worker = threading.Thread(target=ask)
        worker.start()
        time.sleep(0.3)
        cancel.set()                                     # "Argus, stop"
        worker.join(5)
        assert not worker.is_alive() and result["events"] == [] and result["took"] < 2
    finally:
        server.stop()


def test_a_reply_that_doesnt_start_in_time_is_retried(no_sleep):
    server = KeepAlive("hang_first")
    try:
        client = Claude("k", base_url=server.url, first_byte_timeout=0.5, max_retries=1)
        started = time.monotonic()
        assert client.create(BODY)["stop_reason"] == "end_turn"
        assert len(no_sleep) == 1 and time.monotonic() - started < 3
    finally:
        server.stop()


def test_a_stream_that_goes_silent_before_any_words_is_retried(no_sleep):
    server = KeepAlive("stall_first")
    try:
        client = Claude("k", base_url=server.url, first_byte_timeout=0.5, max_retries=1)
        assert [e["type"] for e in client.stream(BODY)][-1] == "message_stop"
        assert len(no_sleep) == 1 and server.requests == 2
    finally:
        server.stop()


def test_retries_give_up_when_the_api_asks_for_a_long_pause(no_sleep):
    class Busy(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            data = b'{"error": {"type": "rate_limit_error", "message": "slow down"}}'
            self.send_response(429)
            self.send_header("Retry-After", "120")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Busy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(LLMError, match="429"):
            Claude("k", base_url=f"http://127.0.0.1:{server.server_port}").create(BODY)
        assert no_sleep == []                            # says so at once instead of going silent for 2 minutes
    finally:
        server.shutdown()


def test_after_a_busy_answer_the_same_connection_is_used_again(no_sleep):
    server = KeepAlive("busy_first")
    try:
        client = Claude("k", base_url=server.url)
        assert client.create(BODY)["stop_reason"] == "end_turn"
        assert len(no_sleep) == 1 and client.connections_opened == 1 and server.requests == 2
    finally:
        server.stop()


def test_an_answer_that_breaks_off_is_asked_for_again(no_sleep):
    server = KeepAlive("cut_first")
    try:
        client = Claude("k", base_url=server.url)
        assert client.create(BODY)["stop_reason"] == "end_turn" and len(no_sleep) == 1
    finally:
        server.stop()


def test_a_refused_request_is_not_retried(no_sleep):
    server = KeepAlive("refuse")
    try:
        with pytest.raises(LLMError, match="bad request") as info:
            list(Claude("k", base_url=server.url).stream(BODY))
        assert not info.value.retryable and no_sleep == [] and server.requests == 1
    finally:
        server.stop()


def test_stop_also_cuts_a_reply_that_went_silent():
    server = KeepAlive("stall_first")
    try:
        client = Claude("k", base_url=server.url, first_byte_timeout=30, stall_timeout=30)
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        assert list(client.stream(BODY, cancel)) == [] and time.monotonic() - started < 2
    finally:
        server.stop()


def test_the_pool_keeps_only_a_couple_of_connections_and_closes_them_at_the_end():
    server = KeepAlive()
    try:
        client = Claude("k", base_url=server.url)
        conns = [client._new() for _ in range(3)]
        for conn in conns:
            conn.connect()
            client._give_back(conn)
        assert len(client._idle) == Claude.MAX_IDLE and conns[2].sock is None     # the extra one was closed
        client.close()
        assert client._idle == [] and all(conn.sock is None for conn in conns)
    finally:
        server.stop()


# ---- a reply that breaks off halfway picks up where it stopped -----------------------------------------------
class BreaksOff:
    """First reply: "The weather today is" and then the connection drops. After that: the rest."""

    def __init__(self, failures=1):
        self.bodies, self.failures = [], failures

    def stream(self, body, cancel=None):
        self.bodies.append(json.loads(json.dumps(body)))
        yield {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 10, "output_tokens": 1}}}
        yield {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
        if len(self.bodies) <= self.failures:
            yield {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "The weather today is"}}
            raise LLMError("lost the connection to the Claude API (reset)", retryable=True, partial=True)
        yield {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " sunny and warm."}}
        yield {"type": "content_block_stop", "index": 0}
        yield {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}
        yield {"type": "message_stop"}


class Heard(Events):
    def __init__(self):
        self.deltas = []

    def text(self, delta):
        self.deltas.append(delta)


def test_a_reply_that_breaks_off_is_picked_up_where_it_stopped(tmp_path):
    agent, _ = make_agent(tmp_path, Script())
    agent.llm = BreaksOff()
    heard = Heard()
    session = agent.store.session_for("voice", "test", 30)
    turn = agent.run(session_id=session, channel="voice", content="weather?", events=heard)
    assert "".join(heard.deltas) == "The weather today is sunny and warm."        # nothing said twice
    assert turn.text == "The weather today is sunny and warm." and not turn.error
    second = agent.llm.bodies[1]["messages"]
    assert second[-2]["content"][0]["text"] == "The weather today is" and second[-1]["content"][0]["text"] == RESUME_NOTE
    assert validate(agent.llm.bodies[1]) == ""
    assert '"reply_resumed"' in (tmp_path / "audit.jsonl").read_text()


def test_a_reply_that_keeps_breaking_off_gives_up_with_a_clear_error(tmp_path):
    agent, _ = make_agent(tmp_path, Script())
    agent.llm = BreaksOff(failures=10)
    session = agent.store.session_for("voice", "test", 30)
    turn = agent.run(session_id=session, channel="voice", content="weather?", events=Heard())
    assert "lost the connection" in turn.error and len(agent.llm.bodies) == 3      # two tries to pick it up


# ---- listening and speaking -----------------------------------------------------------------------------------
def test_a_long_first_sentence_starts_speaking_at_a_comma():
    buf = SentenceBuffer()
    assert buf.feed("The top story today, according to Reuters and the BBC") == []
    first = buf.feed(", is a storm that closed")
    assert first == ["The top story today, according to Reuters and the BBC,"]
    assert buf.feed(" highways, roads, and bridges. Then more, and more.") == \
        ["is a storm that closed highways, roads, and bridges."]          # later sentences stay whole
    short = SentenceBuffer()
    assert short.feed("Sure, I'll do that") == [] and short.flush() == ["Sure, I'll do that"]


def test_argus_gets_a_connection_ready_while_someone_is_talking(tmp_path):
    argus, _, _ = make_argus(tmp_path, [])
    warmed = []
    argus.brain.warm = lambda: warmed.append(1)
    chunks = iter([np.zeros(512, np.float32)] * 3)
    argus.source.read = lambda timeout=0.25: next(chunks, None)
    argus.source.finished = True
    argus.segmenter.speech_active = True
    argus.run()
    assert len(warmed) == 3
    warmed.clear()
    argus.listener.set_sleeping(True)                     # asleep: nothing is touched
    chunks = iter([np.zeros(512, np.float32)] * 3)
    argus.source.read = lambda timeout=0.25: next(chunks, None)
    argus.run()
    assert warmed == []


def test_reminders_wait_while_you_are_away_and_are_spoken_when_you_are_back(tmp_path):
    argus, voice, _ = make_argus(tmp_path, [])
    argus.announce("Reminder: stretch your legs")
    assert wait_for(lambda: voice.spoken == ["Reminder: stretch your legs"])
    argus.set_locked(True)
    argus.announce("Reminder: call home")
    argus.announce("   ")                                # nothing to say: ignored
    argus.set_mic(False)
    argus.set_locked(False)                               # back, but the microphone is still off
    time.sleep(0.2)
    assert voice.spoken == ["Reminder: stretch your legs"]
    argus.set_mic(True)
    assert wait_for(lambda: voice.spoken[1:] == ["While you were away:", "Reminder: call home"])
    argus.set_locked(True)
    for i in range(25):
        argus.announce(f"Reminder {i}")
    argus.set_locked(False)                               # only the latest 20 are kept
    assert wait_for(lambda: voice.spoken[-1] == "Reminder 24" and "Reminder 4" not in voice.spoken)


def test_end_of_speech_setting_is_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("[listening]\nend_of_speech_seconds = 0.45\n")
    assert config_mod.load().end_of_speech_seconds == 0.45
    (tmp_path / "config.toml").write_text("[listening]\nend_of_speech_seconds = 0.1\n")
    with pytest.raises(ValueError, match="end_of_speech_seconds"):
        config_mod.load()


# ---- web pages and news feeds ---------------------------------------------------------------------------------
def test_a_busy_web_site_gets_one_more_try(tmp_path, monkeypatch):
    hits = []

    class Flaky(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            status, data = (503, b"busy") if len(hits) == 1 else (200, b"<html><p>Back to normal</p></html>")
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(toolbox, "public_ip", lambda host: "127.0.0.1")
    slept = []
    monkeypatch.setattr(toolbox.time, "sleep", slept.append)
    ctx = Context(channel="voice", workspace=tmp_path, store=Store(tmp_path / "db.sqlite"))
    try:
        out = toolbox.fetch_page({"url": f"http://site.example:{server.server_port}/"}, ctx)
        assert not out.is_error and "Back to normal" in out.content and len(hits) == 2 and slept == [0.4]
        hits.clear()
        ctx.cancel.set()                                  # cancelled: no second try
        assert toolbox.fetch_page({"url": f"http://site.example:{server.server_port}/"}, ctx).is_error
        assert len(hits) == 1
    finally:
        server.shutdown()


def test_a_feed_that_times_out_gets_one_more_try(monkeypatch):
    calls, slept = [], []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"<rss/>"

    def urlopen(request, timeout, context):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.URLError(TimeoutError("timed out"))
        return Response()
    monkeypatch.setattr(news.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(news.time, "sleep", slept.append)
    assert news.fetch("https://feeds.example/rss") == b"<rss/>" and len(calls) == 2 and slept == [0.4]
    calls.clear()

    def not_found(request, timeout, context):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
    monkeypatch.setattr(news.urllib.request, "urlopen", not_found)
    with pytest.raises(urllib.error.HTTPError):
        news.fetch("https://feeds.example/missing")
    assert len(calls) == 1                                # a 404 isn't worth a second try


def test_slow_feeds_are_skipped_so_the_answer_isnt_held_up(monkeypatch):
    release = threading.Event()
    rss = (b'<rss><channel><item><title>Fast story</title><link>https://a.example/1</link></item>'
           b'</channel></rss>')

    def fetch(url, timeout=None, retries=1):
        if "slow" in url:
            release.wait(5)
        return rss
    monkeypatch.setattr(news, "fetch", fetch)
    started = time.monotonic()
    articles, errors = news.collect([("Fast", "https://fast.example/rss"), ("Slow", "https://slow.example/rss")], 5,
                                    overall=0.5)
    release.set()
    assert time.monotonic() - started < 2
    assert [a.title for a in articles] == ["Fast story"] and errors == ["Slow: too slow, skipped"]
