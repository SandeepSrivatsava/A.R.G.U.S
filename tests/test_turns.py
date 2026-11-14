"""Knowing when you've finished talking (turns.py and the listening loop)."""
import time

import numpy as np

from argus import turns
from fakes import SPEECH, make_argus


def test_unfinished_sentences():
    for text in ("book a table for", "call Priya and", "what's the weather in", "um", "I want to go to the, ",
                 "remind me about...", "so the thing is that", "what about um."):
        assert turns.unfinished(text), text
    for text in ("", "   ", "what time is it", "call Priya.", "book a table for four", "and?", "12", "!!"):
        assert not turns.unfinished(text), text


def joined(argus):
    asked = []
    argus.brain.ask = lambda text, **kw: asked.append(text)
    return asked


def test_an_unfinished_sentence_waits_for_the_rest_and_is_joined(tmp_path):
    argus, _, _ = make_argus(tmp_path, ["argus book a table for", "argus book a table for four at seven"], smart_turns=True)
    asked = joined(argus)
    argus._utterance(SPEECH)
    assert asked == [] and argus._turn is not None
    argus._check_turn()                                 # not long enough yet
    assert asked == []
    argus._utterance(SPEECH)                            # the rest: joined with what came before
    assert asked == ["argus book a table for four at seven"] and argus._turn is None


def test_a_finished_sentence_goes_straight_through(tmp_path):
    argus, _, _ = make_argus(tmp_path, ["argus what time is it"], smart_turns=True)
    asked = joined(argus)
    argus._utterance(SPEECH)
    assert asked == ["argus what time is it"] and argus._turn is None


def test_waiting_ends_after_a_short_while_or_keeps_going_while_you_talk(tmp_path):
    argus, _, _ = make_argus(tmp_path, ["argus remind me to"], smart_turns=True)
    asked = joined(argus)
    argus._utterance(SPEECH)
    samples, text, _ = argus._turn
    argus.segmenter.speech_active = True                # still talking: keep waiting
    argus._turn = (samples, text, time.monotonic() - 1)
    argus._check_turn()
    assert asked == [] and argus._turn[2] > time.monotonic()
    argus.segmenter.speech_active = False
    argus._turn = (samples, text, time.monotonic() - 1)
    argus._check_turn()                                 # nothing more came: handle what there is
    assert asked == ["argus remind me to"] and argus._turn is None
    argus._check_turn()
    argus._release_turn()


def test_no_waiting_when_it_is_off_or_busy(tmp_path):
    argus, _, _ = make_argus(tmp_path, ["argus book a table for"])        # smart_turns off
    asked = joined(argus)
    argus._utterance(SPEECH)
    assert asked == ["argus book a table for"]
    argus, _, _ = make_argus(tmp_path, ["argus book a table for"], smart_turns=True)
    asked = joined(argus)
    argus.speaker._idle.clear()                         # Argus is talking: talking over it is handled elsewhere
    argus._utterance(SPEECH)
    argus.speaker._idle.set()
    assert argus._turn is None
    argus._utterance(np.zeros(10, dtype=np.float32))    # too short to be speech
    assert argus._turn is None


def test_held_speech_is_handled_when_the_microphone_stream_ends(tmp_path):
    argus, _, _ = make_argus(tmp_path, ["argus tell me about"], smart_turns=True)
    asked = joined(argus)
    argus._utterance(SPEECH)
    argus.source.finished = True
    argus.source.read = lambda: None
    argus.source.is_open = True
    argus.run()
    assert asked == ["argus tell me about"]
