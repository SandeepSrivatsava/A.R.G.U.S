"""Who Argus listens to: approving by voice (owner's voice + a code word, never for paying), what counts as
paying, and other people talking to Argus (it asks the owner first)."""
from __future__ import annotations

import threading

import pytest

from argus import approvals as A
from argus.approvals import DENY, ONCE, TASK, ApprovalRequest, MacApprover, VoiceGate, VoiceOrScreenApprover
from argus.policy import ASK, Policy, has_card_number
from fakes import SPEECH, FakeVoicePrint, Script, call, make_agent, make_argus, make_settings, say, wait_for


class Dialog:
    """The screen dialog: never answered unless told to."""

    def __init__(self, button: str = "", hang: bool = True):
        self.button, self.hang = button, hang
        self.terminated = threading.Event()

    def __call__(self, cmd, **kw):
        import io
        self.stdout = io.StringIO(self.button + "\n")
        return self

    def poll(self):
        return None if self.hang and not self.terminated.is_set() else 0

    def terminate(self):
        self.terminated.set()


# ---- what a spoken answer means -------------------------------------------------------------------------
@pytest.mark.parametrize("said,expected", [
    ("Yes, maple.", TASK), ("yeah maple", TASK), ("okay go ahead, maple", TASK), ("yes, just once, maple", ONCE),
    ("yes mapel", TASK), ("yes", None), ("maple", None), ("yes, river", None), ("No.", DENY),
    ("no, don't do that", DENY), ("yes maple, no wait, don't", DENY), ("cancel", DENY), ("what was that?", None)])
def test_spoken_answers(said, expected):
    assert A.parse_voice_answer(said, "maple") == expected


def test_short_code_words_must_be_exact():
    assert A.parse_voice_answer("yes owl", "owl") == TASK
    assert A.parse_voice_answer("yes bowl", "owl") is None


def test_the_gate_takes_one_answer_then_closes():
    gate = VoiceGate(pick=lambda words: "tiger")
    assert not gate.pending and gate.offer("yes tiger") is None      # nothing asked yet
    assert gate.open() == "tiger" and gate.pending and gate.code == "tiger"
    assert gate.offer("hmm") is None
    assert gate.offer("yes tiger") == TASK and gate.wait(0) == TASK
    assert gate.offer("no") is None                                    # already answered
    gate.close()
    assert not gate.pending


def test_spoken_prompts():
    req = ApprovalRequest(run_id="r", description="deleting files", code="tiger")
    assert req.spoken() == "May I go ahead with deleting files? To allow, say yes, tiger. Or use the screen."
    call = ApprovalRequest(run_id="r", description="call Nopa at 415 555 0123 from your number to book", code="owl")
    assert call.spoken().startswith("May I call Nopa at 415 555 0123")
    screen = ApprovalRequest(run_id="r", pattern_key="computer:left_click", code="tiger")
    assert "use your screen for this task" in screen.spoken()
    money = ApprovalRequest(run_id="r", money=True, code="")
    assert "Touch ID" in money.spoken()
    assert "check the screen" in ApprovalRequest(run_id="r").spoken()


# ---- voice or screen, whichever answers first ------------------------------------------------------------
def make_approver(dialog, *, voice_ok=True, timeout=5.0):
    gate = VoiceGate(pick=lambda words: "maple")
    screen = MacApprover(require_owner_auth=True, popen=dialog, auth=lambda reason, cancel=None: True)
    return VoiceOrScreenApprover(screen, gate, voice_ok=lambda: voice_ok, timeout=timeout), gate


def test_a_spoken_yes_takes_the_dialog_away():
    dialog = Dialog()
    approver, gate = make_approver(dialog)
    req = ApprovalRequest(run_id="r", description="deleting files", choices=["once", "session", "deny"])
    approver.prepare(req)
    assert req.code == "maple"
    threading.Timer(0.2, lambda: gate.offer("yes maple")).start()
    assert approver.ask(req) == TASK
    assert wait_for(dialog.terminated.is_set) and not gate.pending


def test_the_screen_can_still_answer_first():
    approver, gate = make_approver(Dialog("Allow once", hang=False))
    req = ApprovalRequest(run_id="r", description="deleting files", choices=["once", "session", "deny"])
    approver.prepare(req)
    assert approver.ask(req) == ONCE and not gate.pending


@pytest.mark.parametrize("req,voice_ok", [
    (ApprovalRequest(run_id="r", description="pay", choices=["once", "deny"], money=True), True),
    (ApprovalRequest(run_id="r", description="deleting files", choices=["once", "deny"], origin="phone"), True),
    (ApprovalRequest(run_id="r", description="deleting files", choices=["once", "deny"]), False)])
def test_no_spoken_answer_for_money_other_channels_or_an_unknown_voice(req, voice_ok):
    approver, gate = make_approver(Dialog("Allow once", hang=False), voice_ok=voice_ok)
    approver.prepare(req)
    assert req.code == "" and not gate.pending
    assert approver.ask(req) == ONCE                                   # the screen (Touch ID) decides


def test_nobody_answering_is_a_no(monkeypatch):
    dialog = Dialog()
    approver, gate = make_approver(dialog, timeout=0.3)
    req = ApprovalRequest(run_id="r", description="deleting files", choices=["once", "deny"])
    approver.prepare(req)
    assert approver.ask(req) == DENY and wait_for(dialog.terminated.is_set)


def test_withdrawing_a_prepared_request_stops_listening_for_it():
    approver, gate = make_approver(Dialog())
    req = ApprovalRequest(run_id="r", description="x", choices=["once", "deny"])
    approver.prepare(req)
    approver.withdraw(req)
    assert not gate.pending


# ---- the whole loop: Argus asks, the owner answers out loud -----------------------------------------------
def voice_argus(tmp_path, transcripts, script, voiceprint):
    gate = VoiceGate(pick=lambda words: "maple")
    dialog = Dialog()
    holder = {}
    screen = MacApprover(popen=dialog, auth=lambda reason, cancel=None: True)
    approver = VoiceOrScreenApprover(screen, gate, voice_ok=lambda: holder["argus"].voice_approvals_available(),
                                     timeout=5)
    argus, voice, _ = make_argus(tmp_path, transcripts, script, approver=approver, gate=gate,
                                 voiceprint=voiceprint, voice_lock=True, owner_name="Sandeep")
    holder["argus"] = argus
    return argus, voice, gate, dialog


def test_owner_approves_a_risky_command_by_voice(tmp_path):
    (tmp_path / "workspace").mkdir(exist_ok=True)
    (tmp_path / "workspace" / "old.txt").write_text("x")
    script = Script(call("run_command", {"command": "rm old.txt"}), say("Deleted."))
    argus, voice, gate, dialog = voice_argus(tmp_path, ["Argus, delete old.txt", "yes", "yes maple"], script,
                                             FakeVoicePrint(0.9, 0.9, 0.9))
    argus.handle_utterance(SPEECH)
    assert wait_for(lambda: gate.pending)
    assert wait_for(lambda: any("say yes, maple" in s for s in voice.spoken))
    argus.handle_utterance(SPEECH)                     # "yes" without the word: asked again
    assert wait_for(lambda: "Please say yes and the word maple." in voice.spoken)
    argus.handle_utterance(SPEECH)                     # "yes maple"
    argus.brain.wait(5)
    assert not (tmp_path / "workspace" / "old.txt").exists()
    assert wait_for(dialog.terminated.is_set)
    assert '"choice": "session"' in (tmp_path / "audit.jsonl").read_text()


def test_someone_else_saying_the_code_word_is_ignored(tmp_path):
    (tmp_path / "workspace").mkdir(exist_ok=True)
    (tmp_path / "workspace" / "old.txt").write_text("x")
    script = Script(call("run_command", {"command": "rm old.txt"}), say("Okay."))
    argus, voice, gate, dialog = voice_argus(tmp_path, ["Argus, delete old.txt", "yes maple", "no"], script,
                                             FakeVoicePrint(0.9, 0.5, 0.9))   # 0.5: below the stricter 0.55
    argus.handle_utterance(SPEECH)
    assert wait_for(lambda: gate.pending)
    argus.handle_utterance(SPEECH)
    assert gate.pending and '"approval_voice_rejected"' in (tmp_path / "audit.jsonl").read_text()
    argus.handle_utterance(SPEECH)                     # the owner says no
    argus.brain.wait(5)
    assert (tmp_path / "workspace" / "old.txt").exists()


def test_without_an_enrolled_voice_there_are_no_spoken_approvals(tmp_path):
    script = Script(call("run_command", {"command": "rm old.txt"}), say("Okay."))
    gate = VoiceGate(pick=lambda words: "maple")
    approver = VoiceOrScreenApprover(MacApprover(popen=Dialog("Deny", hang=False)), gate,
                                     voice_ok=lambda: False, timeout=2)
    argus, voice, _ = make_argus(tmp_path, ["Argus, delete old.txt"], script, approver=approver, gate=gate)
    assert not argus.voice_approvals_available()
    argus.handle_utterance(SPEECH)
    argus.brain.wait(5)
    assert any("check the screen" in s for s in voice.spoken)


# ---- what counts as paying ---------------------------------------------------------------------------------
def make_policy(tmp_path, title="", label=None):
    s = make_settings(tmp_path)
    state = {"locked": False, "front_bundle": "com.apple.Safari", "front_name": "Safari", "visible_apps": [],
             "front_title": title}

    def inspect(coordinate):
        if label == "boom":
            raise RuntimeError("accessibility hiccup")
        return {"role": "AXButton", "title": label} if label else {}
    return Policy(s, workspace=s.workspace, desktop_state=lambda: dict(state), inspect_point=inspect)


@pytest.mark.parametrize("title,label,member,args,money", [
    ("Checkout - Example Shop", None, "left_click", {"coordinate": [5, 5]}, True),
    ("Review your order summary", None, "key", {"text": "Return"}, True),
    ("Reservations - Nopa", "Place order", "left_click", {"coordinate": [5, 5]}, True),
    ("Reservations - Nopa", "Complete reservation", "left_click", {"coordinate": [5, 5]}, False),
    ("Inbox", "Buy now", "double_click", {"coordinate": [5, 5]}, True),
    ("Inbox", "Pay with PayPal", "left_click", {"coordinate": [5, 5]}, True),
    ("Inbox", "PayPal", "left_click", {"coordinate": [5, 5]}, False),
    ("Inbox", "boom", "left_click", {"coordinate": [5, 5]}, False),
    ("Inbox", None, "type", {"text": "4111 1111 1111 1111"}, True),
    ("Inbox", None, "type", {"text": "Call me at 4155550100 tomorrow"}, False),
    ("Inbox", "Buy now", "scroll", {"coordinate": [5, 5]}, False),
    ("Checkout - Example Shop", None, "scroll", {"coordinate": [5, 5]}, False),
])
def test_paying_is_spotted(tmp_path, title, label, member, args, money):
    verdict = make_policy(tmp_path, title, label).check(member, args, "voice", "computer")
    assert verdict.action == ASK
    assert verdict.money is money and (verdict.scope == "money") is money


def test_card_numbers_need_a_valid_checksum():
    assert has_card_number("my card 4111-1111-1111-1111 ok")
    assert has_card_number("5500 0000 0000 0004")
    assert not has_card_number("4111 1111 1111 1112")
    assert not has_card_number("order 12345")


def test_allowing_screen_use_by_voice_doesnt_cover_paying(tmp_path):
    from argus.approvals import ScriptedApprover
    clicks = []

    class FakeComputer:
        def run(self, member, args):
            from argus.toolbox import ToolOutput
            clicks.append(member)
            return ToolOutput("done")
    labels = iter(["Next", "Place order"])
    script = Script(call("left_click", {"coordinate": [10, 10]}, toolset="computer"),
                    call("left_click", {"coordinate": [20, 20]}, toolset="computer"), say("Done."))
    agent, _ = make_agent(tmp_path, script, computer=FakeComputer())
    agent.policy.inspect_point = lambda coordinate: {"title": next(labels)}
    approver = ScriptedApprover([TASK, DENY])

    class Events:
        def text(self, d): pass
        def tool_started(self, n, d): pass
        def tool_finished(self, n, ok): pass
        def approve(self, request): return approver.ask(request)
    agent.run(session_id="s", channel="voice", content="order it", events=Events())
    assert [r.money for r in approver.seen] == [False, True]
    assert clicks == ["left_click"]


# ---- other people --------------------------------------------------------------------------------------------
def guest_argus(tmp_path, transcripts, script=None, scores=(), **kw):
    script = script or Script(say("Paris."), say("Berlin."), say("Rome."))
    argus, voice, _ = make_argus(tmp_path, transcripts, script, voiceprint=FakeVoicePrint(*scores), voice_lock=True,
                                 owner_name="Sandeep", **kw)
    return argus, voice, script


def test_argus_asks_before_answering_someone_else(tmp_path):
    argus, voice, script = guest_argus(tmp_path, ["Argus, what's the capital of France", "yes go ahead"],
                                       scores=(0.1, 0.9))
    argus.handle_utterance(SPEECH)
    assert wait_for(lambda: "Sandeep, someone else is asking me something. Should I answer them?" in voice.spoken)
    assert script.bodies == []
    argus.handle_utterance(SPEECH)
    argus.brain.wait(5)
    assert wait_for(lambda: "Paris." in voice.spoken)
    body = script.bodies[0]
    assert body["messages"][-1]["content"][0]["text"] == "what's the capital of france" or \
        "capital of france" in body["messages"][-1]["content"][0]["text"].lower()
    assert "a guest" in body["system"][1]["text"]


def test_the_owner_can_say_no(tmp_path):
    argus, voice, script = guest_argus(tmp_path, ["Argus, read me Sandeep's messages", "no"], scores=(0.1, 0.9))
    argus.handle_utterance(SPEECH)
    argus.handle_utterance(SPEECH)
    assert wait_for(lambda: voice.spoken[-1:] == ["Okay, I won't."]) and script.bodies == []
    assert '"guest_declined"' in (tmp_path / "audit.jsonl").read_text()


def test_a_guest_cannot_answer_for_the_owner(tmp_path):
    argus, voice, script = guest_argus(tmp_path, ["Argus, what's the capital of France", "yes"], scores=(0.1, 0.1))
    argus.handle_utterance(SPEECH)
    argus.handle_utterance(SPEECH)
    assert script.bodies == [] and argus._guest_question is not None


def test_talk_to_everyone_lasts_a_while_and_only_me_ends_it(tmp_path):
    now = [1000.0]
    argus, voice, script = guest_argus(
        tmp_path, ["Argus, capital of France", "yes, talk to everyone", "Argus, capital of Germany",
                   "Argus, only me", "Argus, capital of Italy"],
        scores=(0.1, 0.9, 0.1, 0.9, 0.1), clock=lambda: now[0])
    argus.handle_utterance(SPEECH)
    argus.handle_utterance(SPEECH)
    argus.brain.wait(5)
    assert "everyone for" in argus.status()["guests"]
    argus.handle_utterance(SPEECH)                    # answered straight away
    argus.brain.wait(5)
    assert wait_for(lambda: "Berlin." in voice.spoken)
    argus.handle_utterance(SPEECH)
    assert wait_for(lambda: "Okay, just you." in voice.spoken)
    argus.handle_utterance(SPEECH)
    assert wait_for(lambda: voice.spoken[-1].endswith("Should I answer them?")) and len(script.bodies) == 2


def test_the_question_expires_if_the_owner_doesnt_answer(tmp_path):
    now = [1000.0]
    argus, voice, script = guest_argus(tmp_path, ["Argus, capital of France", "yes"], scores=(0.1, 0.9),
                                       clock=lambda: now[0])
    argus.handle_utterance(SPEECH)
    now[0] += 60
    argus.handle_utterance(SPEECH)                    # a plain "yes" much later isn't about the guest
    argus.brain.wait(2)
    assert argus._guest_question is None and script.bodies == []


def test_guests_never(tmp_path):
    argus, voice, script = guest_argus(tmp_path, ["Argus, capital of France"], scores=(0.1,), guests="never")
    argus.handle_utterance(SPEECH)
    argus.speaker.wait_idle(2)
    assert voice.spoken == [] and script.bodies == []


def test_a_guest_doesnt_interrupt_the_owner_s_task(tmp_path):
    hold = threading.Event()

    def responder(body):
        hold.wait(5)
        return say("Done with your task.")
    argus, voice, _ = guest_argus(tmp_path, ["Argus, plan my week", "Argus, capital of France", "yes"],
                                  script=responder, scores=(0.9, 0.1, 0.9))
    try:
        argus.handle_utterance(SPEECH)
        assert wait_for(lambda: argus.brain.busy)
        argus.handle_utterance(SPEECH)
        argus.handle_utterance(SPEECH)
        assert wait_for(lambda: "I'm in the middle of something. Please ask me again in a moment." in voice.spoken)
    finally:
        hold.set()
    argus.brain.wait(5)
    assert wait_for(lambda: "Done with your task." in voice.spoken)


def test_guest_commands_need_an_enrolled_voice(tmp_path):
    argus, voice, _ = make_argus(tmp_path, ["Argus, talk to everyone"])
    argus.handle_utterance(SPEECH)
    assert wait_for(lambda: voice.spoken and "Run argusctl enroll" in voice.spoken[-1])
    assert argus.status()["guests"] == "everyone (voice lock is off)"


def test_status_shows_who_argus_answers(tmp_path):
    argus, _, _ = guest_argus(tmp_path, [])
    assert argus.status()["guests"] == "only you (asks you about others)"


def test_the_iphone_gets_its_own_ok(tmp_path):
    s = make_settings(tmp_path)
    state = {"locked": False, "front_bundle": "com.apple.ScreenContinuity", "front_name": "iPhone Mirroring",
             "visible_apps": [], "front_title": "iPhone Mirroring"}
    policy = Policy(s, workspace=s.workspace, desktop_state=lambda: dict(state))
    verdict = policy.check("left_click", {"coordinate": [5, 5]}, "voice", "computer")
    assert verdict.action == ASK and verdict.scope == "computer:iphone" and "iPhone Mirroring" in verdict.reason
    assert "use your iPhone through iPhone Mirroring" in ApprovalRequest(
        run_id="r", pattern_key="computer:iphone", code="tiger").spoken()
    assert policy.check("screenshot", {}, "voice", "computer").action == "allow"
