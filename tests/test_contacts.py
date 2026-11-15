"""Looking people up in the Contacts app (contacts.py), read-only."""
import subprocess
from types import SimpleNamespace

from argus import contacts

FS, RS = contacts.FS, contacts.RS


def fake(stdout="", code=0, stderr=""):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)
    return run, calls


def test_finds_people_and_their_numbers():
    out = (f"Roshini Dayton{FS}_$!<Mobile>!$_: (415) 555-0101; work: 415-555-0102; {RS}"
           f"Roshan K{FS}{RS}")
    run, calls = fake(out)
    text = contacts.lookup("  Roshini ", run=run)
    assert text == "Roshini Dayton: mobile (415) 555-0101, work 415-555-0102\nRoshan K: no phone number"
    assert calls[0][:2] == ["osascript", "-e"] and calls[0][3:] == ["Roshini", "phone"]
    assert "make new" not in contacts.LOOKUP_SCRIPT and "delete" not in contacts.LOOKUP_SCRIPT   # read-only


def test_emails_only_when_asked_and_nobody_found():
    run, calls = fake(f"Sam{FS}mobile: 1; {FS}sam@example.com; {RS}")
    assert contacts.lookup("Sam", emails=True, run=run) == "Sam: mobile 1; email sam@example.com"
    assert calls[0][-1] == "email"
    assert contacts.lookup("Zed", run=fake("")[0]) == 'No contact matching "Zed".'


def test_problems_are_explained():
    tools = {}
    contacts.attach(tools, run=fake(code=1, stderr="execution error: Not allowed (-1743)")[0])
    out = tools["contacts"].run({"name": "Sam"}, None)
    assert out.is_error and "Automation > Argus > switch on Contacts" in out.content
    contacts.attach(tools, run=fake(code=1, stderr="")[0])
    assert "didn't answer (no reason given)" in tools["contacts"].run({"name": "Sam"}, None).content
    assert tools["contacts"].run({"name": " "}, None).is_error

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 30)
    contacts.attach(tools, run=slow)
    assert "too long" in tools["contacts"].run({"name": "Sam"}, None).content
    contacts.attach(tools, run=fake(f"Sam{FS}mobile: 1; {RS}")[0])
    assert tools["contacts"].run({"name": "Sam"}, None).content == "Sam: mobile 1"
    assert tools["contacts"].channels == frozenset({"voice", "phone", "cli"})
