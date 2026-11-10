"""Shared test helpers: a scripted Claude and a quick way to build Argus's brain and listening loop."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

from mock_claude import ClaudeMock


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def say(text: str) -> Tuple[List[Dict[str, Any]], str]:
    return [{"type": "text", "text": text}], "end_turn"


def call(name: str, args: Dict[str, Any], text: str = "", toolset: str = "") -> Tuple[List[Dict[str, Any]], str]:
    blocks: List[Dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    block: Dict[str, Any] = {"type": "tool_use", "name": name, "input": args}
    if toolset:
        block["toolset_name"] = toolset
    blocks.append(block)
    return blocks, "tool_use"


class Script:
    """Answers each request with the next scripted reply (then 'Done.')."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.bodies: List[Dict[str, Any]] = []

    def __call__(self, body):
        self.bodies.append(body)
        return self.replies.pop(0) if self.replies else say("Done.")


def make_settings(tmp_path: Path, **overrides):
    from argus import config
    s = config.Settings()
    s.workspace_dir = str(tmp_path / "workspace")
    s.user_name, s.city, s.timezone = "Sandeep", "Portland, OR", "America/Los_Angeles"
    s.web_search = False
    for k, v in overrides.items():
        setattr(s, k, v)
    Path(s.workspace_dir).mkdir(parents=True, exist_ok=True)
    return s


def make_agent(tmp_path: Path, responder, *, settings=None, computer=None, desktop=None, glasses=None):
    from argus.agent import Agent
    from argus.audit import Audit
    from argus.llm import Claude
    from argus.policy import Policy
    from argus.store import Store
    from argus.toolbox import build_tools
    mock = ClaudeMock(responder).start()
    s = settings or make_settings(tmp_path)
    state = desktop if desktop is not None else {"locked": False, "front_bundle": "com.apple.Safari",
                                                 "front_name": "Safari", "visible_apps": []}
    agent = Agent(Claude("sk-ant-test", base_url=mock.url, max_retries=1), Store(tmp_path / "argus.db"),
                  Policy(s, workspace=s.workspace, desktop_state=lambda: dict(state)), build_tools(),
                  settings=s, audit=Audit(tmp_path / "audit.jsonl"), computer=computer, glasses=glasses)
    return agent, mock


# ---- the listening loop with scripted transcripts and a fake microphone ---------------------------------
class FakeSource:
    def __init__(self):
        self.is_open = False

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def read(self, timeout=0.25):
        return None


class FakeSegmenter:
    speech_active = False

    def feed(self, chunk):
        return []

    def flush(self):
        return []

    def reset(self):
        pass

    def recent_audio(self):
        return np.zeros(0, dtype=np.float32)


class FakeVoicePrint:
    """Voice check stand-in: scores are queued per utterance (default: clearly the owner)."""
    enrolled = True
    threshold = 0.45

    def __init__(self, *scores: float):
        self.scores = list(scores)

    def score(self, samples) -> float:
        return self.scores.pop(0) if self.scores else 0.9


def make_argus(tmp_path, transcripts, script=None, answers=(), approver=None, **argus_kwargs):
    from argus.approvals import ScriptedApprover
    from argus.audit import Audit
    from argus.brain import Brain, Sessions
    from argus.daemon import Argus
    from argus.hearing import ScriptedTranscriber
    from argus.listener import Listener
    from argus.voice import RecordingVoice, Speaker
    from argus.wake import NameMatcher
    script = script or Script()
    agent, _ = make_agent(tmp_path, script)
    voice = RecordingVoice()
    speaker = Speaker(voice)
    audit = Audit(tmp_path / "audit.jsonl")
    brain = Brain(agent, speaker, approver or ScriptedApprover(list(answers)), audit, Sessions(agent.store))
    argus = Argus(source=FakeSource(), segmenter=FakeSegmenter(), transcriber=ScriptedTranscriber(transcripts),
                  listener=Listener(NameMatcher("argus")), speaker=speaker, brain=brain, audit=audit, **argus_kwargs)
    argus.set_mic(True)
    return argus, voice, script


SPEECH = np.ones(16000, dtype=np.float32) * 0.1
