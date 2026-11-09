import pytest

from argus.listener import ACK, ASK, COMMAND, IGNORE, Listener
from argus.wake import Command, NameMatcher, command_of, is_hallucination, looks_like_echo


@pytest.fixture
def matcher():
    return NameMatcher("argus")


@pytest.mark.parametrize("text", [
    "Argus, what's the weather?",
    "what's the weather, Argus?",
    "can you Argus open Safari",
    "ARGUS!",
    "Hey Argos, open Mail",            # common mishearing
    "argas play some music",
    "are gus what time is it",         # split by the recogniser
    "Argus's list please",
    "August, what's the top news today?",   # how a British "Argus" often comes out
    "Hey, August, stop.",
    "August go to sleep.",
    "open Safari for me, August",
    "thanks August",
])
def test_name_heard_anywhere(matcher, text):
    assert matcher.hear(text).addressed


@pytest.mark.parametrize("text", [
    "I don't want to argue about it",
    "Angus is coming over in August",
    "pass the args to the function",
    "the weather is lovely today",
    "Argo was a great film",
    "We're going to Spain in August.",
    "August is the hottest month",
    "August 5th is my birthday",
    "last August we moved",
    "the August heat was brutal",
    "",
])
def test_ordinary_speech_does_not_wake(matcher, text):
    assert not matcher.hear(text).addressed


def test_rest_drops_name_and_filler(matcher):
    assert matcher.hear("Hey Argus, please stop.").rest == "stop"
    assert matcher.hear("Argus open Safari for me").rest == "open safari for me"


@pytest.mark.parametrize("rest,command", [
    ("stop", Command.STOP), ("never mind", Command.STOP), ("that's enough", Command.STOP),
    ("go to sleep", Command.SLEEP), ("stop listening", Command.SLEEP),
    ("wake up", Command.WAKE), ("i'm back", Command.WAKE),
    ("turn off the mic", Command.MIC_OFF), ("mute yourself", Command.MIC_OFF),
    ("conversation mode", Command.CONVERSATION_ON), ("let's chat", Command.CONVERSATION_ON),
    ("that's all", Command.CONVERSATION_OFF), ("end the conversation", Command.CONVERSATION_OFF),
    ("new conversation", Command.NEW_SESSION), ("start over", Command.NEW_SESSION),
])
def test_local_commands(rest, command):
    assert command_of(rest) is command


@pytest.mark.parametrize("rest", ["stop the music in spotify", "go to sleep mode settings in macos",
                                  "what's a good conversation starter", "open safari"])
def test_longer_requests_are_not_local_commands(rest):
    assert command_of(rest) is None


def test_bad_names_rejected():
    with pytest.raises(ValueError):
        NameMatcher("two words")


def test_custom_name():
    m = NameMatcher("nova", variants=())
    assert m.hear("nova what's up").addressed
    assert not m.hear("argus what's up").addressed


def test_hallucinations():
    assert is_hallucination("Thank you.")
    assert is_hallucination("  you ")
    assert not is_hallucination("thank you argus")


def test_echo_detection():
    spoken = ["The top story today is about the storm in the Bay Area."]
    assert looks_like_echo("the top story today is about the storm", spoken)
    assert not looks_like_echo("Argus stop", spoken)
    assert not looks_like_echo("what about sports news", spoken)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def listener(clock):
    return Listener(NameMatcher("argus"), followup_seconds=8, conversation_idle_seconds=120, clock=clock)


def test_unaddressed_speech_is_ignored(listener):
    d = listener.decide("I'll make pasta tonight")
    assert d.kind == IGNORE and d.reason == "not addressed"


def test_addressed_request(listener):
    d = listener.decide("Argus, what's on my calendar?")
    assert d.kind == ASK and d.addressed and not d.interrupt


def test_follow_up_window(listener, clock):
    listener.decide("Argus what's the weather")
    listener.reply_finished()
    clock.t += 5
    assert listener.decide("and tomorrow?").kind == ASK          # no name needed right after a reply
    listener.reply_finished()
    clock.t += 9
    assert listener.decide("and the day after?").kind == IGNORE  # window closed


def test_window_closes_once_used(listener, clock):
    listener.reply_finished()
    assert listener.decide("and tomorrow?").kind == ASK
    clock.t += 1
    assert listener.decide("random chatter in the room").kind == IGNORE


def test_conversation_mode(listener, clock):
    d = listener.decide("Argus, let's chat")
    assert d.kind == COMMAND and d.command is Command.CONVERSATION_ON
    clock.t += 60
    assert listener.decide("tell me a joke").kind == ASK
    clock.t += 100
    assert listener.decide("another one").kind == ASK            # each request extends it
    d = listener.decide("that's all")
    assert d.command is Command.CONVERSATION_OFF
    assert listener.decide("are you there").kind == IGNORE


def test_conversation_mode_times_out(listener, clock):
    listener.decide("Argus conversation mode")
    clock.t += 121
    assert listener.decide("hello?").kind == IGNORE
    assert listener.mode() == "listening"


def test_sleep_ignores_everything_but_wake(listener):
    assert listener.decide("Argus go to sleep").command is Command.SLEEP
    assert listener.mode() == "asleep"
    assert listener.decide("Argus what's the time").kind == IGNORE
    assert listener.decide("wake up").kind == IGNORE             # needs the name
    d = listener.decide("Argus, wake up")
    assert d.command is Command.WAKE and listener.mode() == "listening"


def test_busy_only_listens_for_its_name(listener):
    assert listener.decide("random words", speaking=True).kind == IGNORE
    d = listener.decide("Argus stop", speaking=True)
    assert d.kind == COMMAND and d.command is Command.STOP and d.interrupt
    d = listener.decide("Argus, actually open Notes instead", busy=True)
    assert d.kind == ASK and d.interrupt
    d = listener.decide("Argus", busy=True)
    assert d.command is Command.STOP


def test_name_only_acknowledges_and_opens_window(listener):
    d = listener.decide("Argus?")
    assert d.kind == ACK


def test_noise_ignored_even_in_conversation(listener):
    listener.decide("Argus let's talk")
    assert listener.decide("Thank you.").kind == IGNORE
