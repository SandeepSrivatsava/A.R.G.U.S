"""Looking people up in your contacts, and only looking: names and phone numbers from the Mac's Contacts app.

Argus asks the Contacts app through one fixed AppleScript below (macOS asks you once whether Argus may control
Contacts). There's no script for adding, changing or deleting a contact. It returns at most 8 people matching
the name you said, with their phone numbers (and emails only when asked), so the rest of your address book
never reaches Claude. Only from you: your voice, the phone app or the command line, never guests or tasks
Argus runs by itself.
"""
from __future__ import annotations

import subprocess
from typing import Any, Callable, Dict, List

FS, RS = "\x1f", "\x1e"
MAX_PEOPLE = 8
TIMEOUT = 30.0

LOOKUP_SCRIPT = r'''
on run argv
	set theName to item 1 of argv
	set withEmail to ((item 2 of argv) is "email")
	set FS to ASCII character 31
	set RS to ASCII character 30
	set out to ""
	set n to 0
	tell application "Contacts"
		set found to (every person whose name contains theName)
		repeat with p in found
			if n is greater than or equal to 8 then exit repeat
			set n to n + 1
			set rowText to (name of p) & FS
			repeat with ph in phones of p
				set rowText to rowText & (label of ph) & ": " & (value of ph) & "; "
			end repeat
			if withEmail then
				set rowText to rowText & FS
				repeat with em in emails of p
					set rowText to rowText & (value of em) & "; "
				end repeat
			end if
			set out to out & rowText & RS
		end repeat
	end tell
	return out
end run
'''


def _label(raw: str) -> str:
    """'_$!<Mobile>!$_' (how Contacts stores built-in labels) -> 'mobile'."""
    raw = raw.strip()
    if raw.startswith("_$!<") and raw.endswith(">!$_"):
        raw = raw[4:-4]
    return raw.lower() or "phone"


def parse(output: str) -> List[Dict[str, Any]]:
    people = []
    for record in str(output).split(RS):
        if not record.strip():
            continue
        parts = record.split(FS)
        name = parts[0].strip()
        phones = []
        for item in (parts[1] if len(parts) > 1 else "").split(";"):
            if ":" in item:
                label, number = item.split(":", 1)
                if number.strip():
                    phones.append((_label(label), number.strip()))
        emails = [e.strip() for e in (parts[2] if len(parts) > 2 else "").split(";") if e.strip()]
        if name:
            people.append({"name": name, "phones": phones, "emails": emails})
    return people[:MAX_PEOPLE]


def lookup(name: str, *, emails: bool = False, run: Callable[..., Any] = subprocess.run) -> str:
    name = " ".join(str(name or "").split())[:60]
    if not name:
        raise ValueError("say whose number to look up")
    proc = run(["osascript", "-e", LOOKUP_SCRIPT, name, "email" if emails else "phone"],
               capture_output=True, text=True, timeout=TIMEOUT)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "-1743" in err or "not allowed" in err.lower():
            raise PermissionError("macOS hasn't let Argus read Contacts yet: System Settings > Privacy & Security > "
                                  "Automation > Argus > switch on Contacts (or allow it when macOS asks)")
        raise RuntimeError(f"the Contacts app didn't answer ({err[:160] or 'no reason given'})")
    people = parse(proc.stdout)
    if not people:
        return f"No contact matching \"{name}\"."
    lines = []
    for person in people:
        phones = ", ".join(f"{label} {number}" for label, number in person["phones"]) or "no phone number"
        extra = f"; email {', '.join(person['emails'])}" if emails and person["emails"] else ""
        lines.append(f"{person['name']}: {phones}{extra}")
    return "\n".join(lines)


DESCRIPTION = ("Look someone up in the user's contacts (the Mac's Contacts app, read-only): their phone numbers, and "
               "emails if asked. Use it when the user names a person to call or text, instead of asking for the number. "
               "name: the name as said (first name is fine). If several match, ask which one.")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name"], "properties": {
    "name": {"type": "string"}, "emails": {"type": "boolean"}}}


def attach(tools: Dict[str, Any], run: Callable[..., Any] = subprocess.run) -> None:
    from .toolbox import Tool, ToolOutput

    def tool(args: Dict[str, Any], ctx) -> ToolOutput:
        try:
            return ToolOutput(lookup(args.get("name") or "", emails=bool(args.get("emails")), run=run))
        except (ValueError, PermissionError, RuntimeError) as exc:
            return ToolOutput.error(str(exc))
        except subprocess.TimeoutExpired:
            return ToolOutput.error("the Contacts app took too long to answer")

    tools["contacts"] = Tool("contacts", DESCRIPTION, SCHEMA, tool, frozenset({"voice", "phone", "cli"}))
