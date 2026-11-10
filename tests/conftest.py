"""Shared test setup: every request Argus sends to the stand-in Claude server must be one the real API
would accept (see mock_claude.validate), unless a test is about invalid requests."""
import pytest

import mock_claude


@pytest.fixture(autouse=True)
def claude_requests_are_valid(request, monkeypatch):
    created = []
    original = mock_claude.ClaudeMock.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(mock_claude.ClaudeMock, "__init__", init)
    yield created
    if request.node.get_closest_marker("invalid_requests_ok"):
        return
    problems = [problem for mock in created for problem in mock.invalid]
    assert not problems, f"requests the real Claude API would reject: {problems}"
