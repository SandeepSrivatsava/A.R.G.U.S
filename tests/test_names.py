"""Putting right names the speech model garbled (names.py)."""
import json
from types import SimpleNamespace

import numpy as np

from argus import names
from argus.store import Store

KNOWN = ["Jack in the Box", "Dune Part Three", "Stevenson Blvd", "Argus", "Udupi Palace", "Priya"]


def test_garbled_names_are_put_right():
    for heard, meant in (("call jacket in the box and ask when they close", "call Jack in the Box and ask when they close"),
                         ("is june part three out yet?", "is Dune Part Three out yet?"),
                         ("book a table at oodoopy palace.", "book a table at Udupi Palace."),
                         ("go to stevenson boulevard", "go to Stevenson Blvd"),
                         ("argos, what time is it", "Argus, what time is it")):
        assert names.correct(heard, KNOWN)[0] == meant


def test_everyday_words_and_unclear_ones_are_left_alone():
    for text in ("what time is it", "why is that", "the box is here", "I like june", "call jack in the box", ""):
        fixed, changes = names.correct(text, KNOWN)
        assert changes == [] and fixed == text
    assert names.correct("call smith", ["Smyth", "Smith-e"])[1] == []        # could be either: left alone
    assert names.correct("hello", [])[1] == []
    assert names.correct("tell priya", ["Pri"])[1] == []                    # too short to be sure of


def test_names_are_picked_out_of_text():
    text = ("Jack in the Box on Stevenson Blvd is open until 4 AM. Dune Part Three opens December 18. Call Priya. "
            "The Udupi Palace is good. I think so. OK.")
    assert names.names_in(text) == ["Jack in the Box", "Stevenson Blvd", "Dune Part Three", "December", "Priya",
                                    "Udupi Palace"]


def test_phonetic_keys_and_similarity():
    assert names.phonetic("Phil") == names.phonetic("fill")
    assert names.phonetic("knight") == names.phonetic("night")
    assert names.phonetic("oodoopy") == names.phonetic("Udupi")
    assert names.phonetic("") == "" and names.phonetic("why") == "a" or names.phonetic("why") == ""
    assert names.jaro_winkler("martha", "marhta") > 0.95 and names.jaro_winkler("", "x") == 0.0
    assert names.jaro_winkler("abc", "xyz") == 0.0 and names._ratio("", "") == 1.0


def test_known_names_keep_the_latest_and_their_sources(tmp_path):
    t = [0.0]
    known = names.KnownNames(fixed=["Argus", "Sam", ""], limit=3, clock=lambda: t[0],
                             sources=[lambda: ["Portland"], lambda: 1 / 0])
    known.notice("We looked at Udupi Palace and Dune Part Three.")
    known.add(["x", "A" * 80, "Jack in the Box", "udupi palace", "Nopa"])
    everything = known.all()
    assert everything[:3] == ["Argus", "Sam", "Portland"]
    assert everything[3:] == ["Nopa", "udupi palace", "Jack in the Box"]     # newest first, at most 3
    assert names.prompt_line([]) == ""
    assert "Argus; Sam" in names.prompt_line(everything)


def test_names_added_by_hand(tmp_path, capsys):
    from argus import __main__ as cli
    store = Store(tmp_path / "a.db")
    assert names.add_name(store, "Udupi  Palace") and not names.add_name(store, "udupi palace")
    assert not names.add_name(store, " ")
    assert names.added(store) == ["Udupi Palace"]
    store.set(names.KV_KEY, "not json")
    assert names.added(store) == []
    store.set(names.KV_KEY, json.dumps(["Nopa", 3]))
    assert names.added(store) == ["Nopa"]
    cli.cmd_names(SimpleNamespace(action="add", name=["Jack", "in", "the", "Box"]), store=store)
    cli.cmd_names(SimpleNamespace(action="add", name=["Nopa"]), store=store)
    cli.cmd_names(SimpleNamespace(action="list", name=[]), store=store)
    cli.cmd_names(SimpleNamespace(action="remove", name=["nopa"]), store=store)
    cli.cmd_names(SimpleNamespace(action="remove", name=["nopa"]), store=store)
    out = capsys.readouterr().out
    assert "Added: Jack in the Box" in out and "Already there" in out and "Removed: nopa" in out
    assert "wasn't in the list" in out and names.added(store) == ["Jack in the Box"]
    store.set(names.KV_KEY, "[]")
    cli.cmd_names(SimpleNamespace(action="list", name=[]), store=store)
    assert "No names added yet" in capsys.readouterr().out


def test_what_argus_hears_is_corrected_and_logged(tmp_path):
    from fakes import SPEECH, make_argus
    argus, _, _ = make_argus(tmp_path, ["argus call jacket in the box"])
    argus.names = names.KnownNames(fixed=["Jack in the Box"])
    asked = []
    argus.brain.ask = lambda text, **kw: asked.append(text)
    argus.handle_utterance(SPEECH)
    assert asked and "Jack in the Box" in asked[0]
    assert any(e.get("event") == "names_fixed" for e in argus.audit.tail(10))
    argus.names = SimpleNamespace(all=lambda: 1 / 0)
    assert argus._fix_names("hello") == "hello"


def test_claude_is_told_the_names_and_argus_notices_new_ones(tmp_path):
    from fakes import Script, call, make_agent, say
    from argus.toolbox import Tool, ToolOutput
    script = Script(call("place_search", {"query": "dosa"}), say("Udupi Palace on Main Street it is."), say("Hi."))
    agent, _ = make_agent(tmp_path, script)
    agent.tools["place_search"] = Tool("place_search", "x", {"type": "object"},
                                       lambda a, c: ToolOutput("Udupi Palace, 4.4 stars"), frozenset({"voice"}))
    known = names.KnownNames(fixed=["Argus"])
    agent.known_names, agent.notice = known.all, known.notice
    from argus.agent import Events
    agent.run(session_id="s", channel="voice", content="find a dosa place", events=Events())
    assert "Names you know" in json.dumps(script.bodies[0]["system"])
    assert "Udupi Palace" in known.all() and "Main Street" in known.all()
    agent.known_names = lambda: 1 / 0
    agent.notice = lambda text: 1 / 0
    agent.run(session_id="t", channel="voice", content="hi", events=Events())       # broken name lists never break a turn
