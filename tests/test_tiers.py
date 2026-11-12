"""The right Claude model for each request (tiers.py): the quick one for everyday questions, the main one for
the Mac and for judgement, the heavy one for coding and analysis; and handing a request up."""
import json

import pytest

from argus import config, tiers
from argus.agent import Events, request_cost
from argus.computer import Computer

from fakes import Script, call, make_agent, make_settings, say
from test_agent import FakeBackend

LIGHT, MAIN, HEAVY = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"


def run(agent, text, channel="voice", session=None, events=None):
    session = session or agent.store.session_for(channel, "test", 30)
    return agent.run(session_id=session, channel=channel, content=text, events=events or Events())


@pytest.mark.parametrize("said, model", [
    ("what time is it", LIGHT), ("how's the weather today", LIGHT), ("how is the traffic today", LIGHT),
    ("remind me to call mom at six", MAIN),                # "call": could be a phone call
    ("set a timer for ten minutes", LIGHT), ("any news?", LIGHT), ("thanks", LIGHT),
    ("I'm not in a good mood", MAIN), ("should I take the job in Austin", MAIN),
    ("open Safari and look up the menu", MAIN), ("what am I looking at", MAIN), ("I'm going on a flight Friday", MAIN),
    ("write a Python script that renames my photos", HEAVY), ("debug this stack trace for me", HEAVY),
    ("build me a website for my boba shop", HEAVY), ("analyze my spending and write a report", HEAVY),
    ("do a deep dive on small towns near Sacramento", HEAVY),
    ("Taylor Swift's new album, is it any good", LIGHT), ("I have a class at five", LIGHT),
])
def test_requests_go_to_the_model_that_fits(tmp_path, said, model):
    settings = make_settings(tmp_path)
    assert tiers.choose(said, "voice", settings)[1] == model, said


def test_other_channels_photos_and_switched_off_routing_use_the_main_model(tmp_path):
    settings = make_settings(tmp_path)
    assert tiers.choose("what time is it", "scheduler", settings) == ("main", MAIN)
    assert tiers.choose("what time is it", "guest", settings) == ("main", MAIN)
    assert tiers.choose([{"type": "image"}, {"type": "text", "text": "what is this"}], "phone", settings)[1] == MAIN
    assert tiers.choose("word " * 50, "voice", settings)[1] == MAIN
    assert tiers.choose("what time is it", "voice", make_settings(tmp_path, routing="off"))[1] == MAIN
    assert tiers.choose("what time is it", "voice", make_settings(tmp_path, light_model=""))[1] == MAIN
    assert tiers.choose("write some code", "voice", make_settings(tmp_path, heavy_model=""))[1] == MAIN
    assert tiers.model_for("heavy", make_settings(tmp_path, heavy_model="")) == MAIN


def test_the_quick_model_gets_no_screen_and_can_hand_over(tmp_path):
    script = Script(say("Four o'clock."), say("Opened."), say("Here's the script."))
    agent, _ = make_agent(tmp_path, script, computer=Computer(FakeBackend()))
    light = run(agent, "what time is it", session="a")
    main = run(agent, "open Safari and look up the menu", session="b")
    heavy = run(agent, "write a Python script that renames my photos", session="c")
    quick, normal, hard = script.bodies
    assert (light.model, main.model, heavy.model) == (LIGHT, MAIN, HEAVY)
    assert quick["model"] == LIGHT and quick["thinking"] == {"type": "disabled"}
    assert "hand_over" in [t.get("name") for t in quick["tools"]]
    assert not any(str(t.get("type", "")).startswith("computer") for t in quick["tools"])
    assert normal["model"] == MAIN and normal["thinking"] == {"type": "between_tools"}
    assert any(str(t.get("type", "")).startswith("computer") for t in normal["tools"])
    assert "hand_over" not in [t.get("name") for t in normal["tools"]]
    assert hard["model"] == HEAVY and hard["output_config"] == {"effort": "high"} and "thinking" not in hard
    assert light.cost == pytest.approx(request_cost(LIGHT, {"input_tokens": 1000, "output_tokens": 50}))
    assert heavy.cost == pytest.approx(request_cost(HEAVY, {"input_tokens": 1000, "output_tokens": 50}))
    log = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [e["model"] for e in log if e["event"] == "turn"] == [LIGHT, MAIN, HEAVY]


def test_handing_over_moves_the_request_up_without_a_trace(tmp_path):
    script = Script(call("hand_over", {"level": "main"}), say("Take the offer; here's why."))
    agent, _ = make_agent(tmp_path, script)
    turn = run(agent, "hmm, the Austin offer or stay put")
    first, second = script.bodies
    assert first["model"] == LIGHT and second["model"] == MAIN and turn.model == MAIN
    assert second["messages"] == first["messages"]                 # the quick model's message is dropped
    assert turn.text == "Take the offer; here's why."
    history = agent.store.history(agent.store.session_for("voice", "test", 30))
    assert "hand_over" not in json.dumps(history)
    assert '"handed_over"' in (tmp_path / "audit.jsonl").read_text()


def test_handing_over_to_the_heavy_model_and_after_using_tools(tmp_path):
    script = Script(call("get_news", {}), call("hand_over", {"level": "heavy"}), say("A long, careful answer."))
    agent, _ = make_agent(tmp_path, script)
    spoken = []

    class Voice(Events):
        def working(self, tier):
            spoken.append(tier)
    turn = run(agent, "any news about the storm", events=Voice())
    assert [b["model"] for b in script.bodies] == [LIGHT, LIGHT, HEAVY] and turn.model == HEAVY
    last = script.bodies[2]["messages"]
    assert [m["role"] for m in last[-2:]] == ["assistant", "user"]         # a clean exchange for the new model
    assert last[-1]["content"][0]["text"] == tiers.BRIDGE[1]
    assert spoken == ["light", "heavy"]
    without = make_agent(tmp_path / "x", Script(call("hand_over", {"level": "heavy"}), say("ok")),
                         settings=make_settings(tmp_path / "x", heavy_model=""))[0]
    assert run(without, "tell me a joke").model == MAIN


def test_a_short_reply_stays_with_the_model_that_asked(tmp_path):
    script = Script(say("Shall I open it?"), say("Opened."), say("Noon."))
    agent, _ = make_agent(tmp_path, script)
    run(agent, "open the menu for Curry Up Now", session="s")
    run(agent, "yes please", session="s")
    run(agent, "what time is it in Hyderabad right now, roughly speaking", session="s")
    assert [b["model"] for b in script.bodies] == [MAIN, MAIN, LIGHT]


def test_routing_settings_are_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[brain]\nrouting = "sometimes"\n')
    with pytest.raises(ValueError, match="brain.routing"):
        config.load()
    (tmp_path / "config.toml").write_text('[brain]\nrouting = "off"\nlight_model = ""\nheavy_model = "claude-fable-5-2"\n'
                                          'heavy_effort = "max"\n[mail]\nenabled = true\ndays = 30\n')
    s = config.load()
    assert (s.routing, s.light_model, s.heavy_model, s.heavy_effort, s.mail_enabled, s.mail_days) == \
        ("off", "", "claude-fable-5-2", "max", True, 30)
    (tmp_path / "config.toml").write_text('[brain]\nheavy_effort = "lots"\n')
    with pytest.raises(ValueError, match="brain.heavy_effort"):
        config.load()
