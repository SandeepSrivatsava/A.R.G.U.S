"""Other AI brains next to Claude: ChatGPT and Grok (pay-per-use), Gemini, Groq, Mistral and OpenRouter (free).
Only the question leaves the Mac, without your name, numbers or addresses; a slow one doesn't hold the answer up;
the paid ones have a daily cap; and `argusctl brains` adds, removes and tests them."""
import threading
import time
from types import SimpleNamespace

import pytest

from argus import config, council
from argus.toolbox import Context

from fakes import make_settings


class Brains:
    """The providers' chat APIs: answers by URL, or a status code, or a delay."""

    def __init__(self, replies=None, delays=None):
        self.replies, self.delays = replies or {}, delays or {}
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, method, url, headers, body=None, timeout=0):
        with self.lock:
            self.calls.append((url, headers, body))
        for part, seconds in self.delays.items():
            if part in url:
                time.sleep(seconds)
        for part, reply in self.replies.items():
            if part in url:
                return reply(body) if callable(reply) else reply
        if "generativelanguage" in url:
            return 200, {"candidates": [{"content": {"parts": [{"text": "Gemini says April."}]}}]}
        return 200, {"choices": [{"message": {"content": f"{url.split('/')[2]} says April."}}]}


def test_chatgpt_and_grok_join_with_their_own_keys(tmp_path):
    brains = Brains()
    tools = {}
    c = council.attach(make_settings(tmp_path), {"OPENAI_API_KEY": "sk-openai-1", "XAI_API_KEY": "xai-1"}, tools,
                       request=brains)
    assert [m.name for m in c.team] == ["ChatGPT (GPT-6 Luna)", "Grok (4.7)"] and all(m.paid for m in c.team)
    out = tools["ask_council"].run({"question": "NYC in December or April?"}, Context(channel="voice", workspace=tmp_path, store=None))
    assert "- ChatGPT (GPT-6 Luna): api.openai.com says April." in out.content and "- Grok (4.7): api.x.ai says April." in out.content
    sent = {url: (headers, body) for url, headers, body in brains.calls}
    headers, body = sent["https://api.openai.com/v1/chat/completions"]
    assert headers == {"Authorization": "Bearer sk-openai-1"} and body["model"] == "gpt-6-luna"
    assert body["reasoning_effort"] == "low" and body["max_completion_tokens"] == 1500 and "temperature" not in body
    assert sent["https://api.x.ai/v1/chat/completions"][1]["model"] == "grok-4.7"


def test_a_setting_a_model_refuses_is_dropped_once(tmp_path):
    seen = []

    def picky(body):
        seen.append(dict(body))
        return (400, {"error": {"message": "Unsupported parameter: reasoning_effort"}}) if "reasoning_effort" in body else (
            200, {"choices": [{"message": {"content": "Fine."}}]})
    member = council.members(make_settings(tmp_path), {"OPENAI_API_KEY": "k"})[0]
    assert member.ask("q", Brains({"openai": picky})) == "Fine."
    assert "reasoning_effort" in seen[0] and set(seen[1]) == {"model", "messages"}
    stubborn = Brains({"openai": (400, {"error": {"message": "bad request for key k-secret"}})})
    member.key = "k-secret"
    with pytest.raises(council.SearchError, match=r"HTTP 400: bad request for key \.\.\."):
        member.ask("q", stubborn)


def test_problems_are_said_plainly(tmp_path):
    member = council.members(make_settings(tmp_path), {"XAI_API_KEY": "x"})[0]
    for status, words in ((401, "the key isn't right"), (402, "no credit left"), (429, "free allowance is used up"),
                          (404, "unknown model")):
        with pytest.raises(council.SearchError, match=words):
            member.ask("q", Brains({"x.ai": (status, {"error": "nope"})}))


def test_a_slow_brain_doesnt_hold_the_answer_up(tmp_path):
    brains = Brains(delays={"x.ai": 3.0})
    team = council.members(make_settings(tmp_path), {"GEMINI_API_KEY": "g", "XAI_API_KEY": "x"})
    c = council.Council(team, request=brains, wait=0.5)
    began = time.monotonic()
    answers = c.ask("q")
    assert time.monotonic() - began < 2.5
    assert answers[0] == ("Gemini (gemini-2.5-flash)", "Gemini says April.", None)
    assert answers[1] == ("Grok (4.7)", None, "still thinking after 0.5 s (left out)")


def test_paid_brains_have_a_daily_cap(tmp_path):
    days = ["2026-11-12"]
    count = council.DailyCount(tmp_path / "council-today.json", limit=1, today=lambda: days[0])
    team = council.members(make_settings(tmp_path), {"OPENAI_API_KEY": "o", "GROQ_API_KEY": "g"})
    c = council.Council(team, request=Brains(), count=count)
    assert all(answer for _, answer, _ in c.ask("first"))
    second = dict((name, (answer, problem)) for name, answer, problem in c.ask("second"))
    assert second["ChatGPT (GPT-6 Luna)"] == (None, "skipped: its 1 questions for today are used")
    assert second["Groq (gpt-oss-120b, OpenAI's open model)"][0]                 # free: no cap
    assert council.DailyCount(tmp_path / "council-today.json", limit=1, today=lambda: days[0]).take("ChatGPT (GPT-6 Luna)") is False
    days[0] = "2026-11-13"                                                       # a new day
    assert count.take("ChatGPT (GPT-6 Luna)") is True
    assert oct((tmp_path / "council-today.json").stat().st_mode & 0o777) == "0o600"
    only_paid = council.Council(team[:1], request=Brains(), count=council.DailyCount(None, limit=0))
    assert only_paid.ask("q") == [("ChatGPT (GPT-6 Luna)", None, "skipped: its 0 questions for today are used")]


def test_your_details_dont_leave_the_mac(tmp_path):
    question = ("Sandeep Kumar wants to know: is flight 1234567890 worth it? Reach me at me@example.com or "
                "+1 (503) 555-0142. Card 4111111111111111. Sandeep's budget is $300.")
    clean = council.scrub(question, ["Sandeep", "Kumar", "Al"])
    assert "Sandeep" not in clean and "Kumar" not in clean and "example.com" not in clean and "555" not in clean
    assert "4111" not in clean and "1234567890" not in clean and "$300" in clean and "the user" in clean
    assert council.scrub("fly on 2026-12-20 or 2026-12-27, 100 200 300 miles?") == "fly on 2026-12-20 or 2026-12-27, 100 200 300 miles?"
    brains = Brains()
    tools = {}
    council.attach(make_settings(tmp_path, user_name="Sandeep Kumar"), {"GROQ_API_KEY": "g"}, tools, request=brains)
    tools["ask_council"].run({"question": question}, Context(channel="voice", workspace=tmp_path, store=None))
    sent = brains.calls[0][2]["messages"][0]["content"]
    assert "Sandeep" not in sent and "me@example.com" not in sent and "budget is $300" in sent


def test_checking_a_brain(tmp_path):
    member = council.members(make_settings(tmp_path), {"MISTRAL_API_KEY": "m"})[0]
    ok, said, seconds = council.check(member, Brains())
    assert ok and said == "api.mistral.ai says April." and seconds >= 0
    ok, said, _ = council.check(member, Brains({"mistral": (401, {})}))
    assert not ok and "HTTP 401" in said
    assert council.preset("OpenAI").short == "chatgpt" and council.preset("xai").short == "grok" and council.preset("hal") is None


def test_council_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("[council]\npaid_per_day = 10\nwait_seconds = 20\n")
    s = config.load()
    assert (s.council_paid_per_day, s.council_wait) == (10, 20.0)
    for bad in ("paid_per_day = -1", "wait_seconds = 1"):
        (tmp_path / "config.toml").write_text(f"[council]\n{bad}\n")
        with pytest.raises(ValueError, match="council"):
            config.load()


# ---- argusctl brains --------------------------------------------------------------------------------------------
def brains_cmd(action, name=None, **kw):
    from argus.__main__ import cmd_brains
    return cmd_brains(SimpleNamespace(action=action, name=name), **kw)


def test_argusctl_brains_add_test_and_remove(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    assert brains_cmd("test") == 1 and "argusctl brains add chatgpt" in capsys.readouterr().out
    assert brains_cmd("add", "openai", ask_secret=lambda p: "sk-test-openai-key", request=Brains()) == 0
    out = capsys.readouterr().out
    assert "ChatGPT (GPT-6 Luna): pay-per-use" in out and "platform.openai.com/api-keys" in out and "Test: it answered" in out
    assert brains_cmd("add", "grok", ask_secret=lambda p: "xai-test-key-123", request=Brains({"x.ai": (401, {})})) == 1
    out = capsys.readouterr().out
    assert "data-sharing program off" in out and "it did NOT answer" in out and "HTTP 401" in out
    assert brains_cmd("add", "gemini", ask_secret=lambda p: "AIza-test-gemini-key") == 0
    capsys.readouterr()
    assert brains_cmd("test", request=Brains({"x.ai": (402, {})})) == 1
    out = capsys.readouterr().out
    assert "ok   Gemini" in out and "ok   ChatGPT" in out and "FAIL Grok (4.7)" in out and "no credit left" in out
    assert brains_cmd("list") == 0
    out = capsys.readouterr().out
    assert "  on   ChatGPT (GPT-6 Luna)" in out and "  off  Mistral (Medium)" in out and "brains test" in out
    assert brains_cmd("remove", "grok") == 0 and "XAI_API_KEY" not in (tmp_path / "secrets.env").read_text()
    assert brains_cmd("remove", "gemini") == 0 and "GEMINI_API_KEY" not in (tmp_path / "secrets.env").read_text()
    assert brains_cmd("add", "hal9000") == 1 and "Choose one of: chatgpt, grok, groq, mistral, openrouter, gemini" in capsys.readouterr().out
