"""Keeping your personal details off data-broker and people-search sites (the "Footprint" idea), and keeping
track of the removal requests until they're done.

The best single step for a Californian is the state's own Delete Request and Opt-out Platform (DROP) at
privacy.ca.gov/drop: one verified request goes to every registered data broker (500+), and since
1 August 2026 they must process it and delete again every 45 days. You verify your identity there yourself;
Argus never fills in identity checks, payment pages or ID uploads.

People-search sites (Whitepages, Spokeo and the like) also take removal requests one by one. Argus can find a
site's current opt-out page and draft the request, then you submit it (most send a confirmation email or
need a phone check). This tool keeps the list: what was sent when, what's confirmed, what came back, and
which requests need a nudge after 45 days.

Looking yourself up on these sites means sending your name (and city) to a search engine or the site, so
Argus does that only when you ask, and says what it will search for first.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

DROP_URL = "https://privacy.ca.gov/drop/"
FOLLOW_UP_DAYS = 45
STATUSES = ("found", "sent", "confirmed", "removed", "reappeared", "not listed")
KEY = "privacy:requests"
MAX_SITES = 60
# Common people-search sites. No links here on purpose: opt-out pages move, so Argus looks up the current
# one each time (web_search / fetch_page) instead of trusting a stale address.
SITES = ("Whitepages", "Spokeo", "BeenVerified", "TruePeopleSearch", "FastPeopleSearch", "USPhoneBook",
         "ThatsThem", "FamilyTreeNow", "Radaris", "Intelius", "PeopleFinders", "Nuwber", "MyLife", "ClustrMaps",
         "PeekYou", "InstantCheckmate", "TruthFinder", "SearchPeopleFree")

PLAN = (
    "1. California DROP ({url}): one free, verified request to every registered data broker. Sandeep signs in "
    "and verifies himself there; brokers must delete within 90 days and check again every 45 days.\n"
    "2. People-search sites, one by one: {sites}. For each, find the site's current opt-out page, check "
    "whether he's listed (only if he asks you to look him up), draft the request, and record it here with "
    "status 'sent'.\n"
    "3. Every week (if he wants that, set it up with the schedule tool): check 'status' here, nudge requests "
    "older than {days} days, and look again for listings that came back.\n"
    "Never fill in identity checks, ID uploads or payment pages; hand those to him."
)


def _load(store) -> List[Dict[str, Any]]:
    try:
        rows = json.loads(store.get(KEY) or "[]")
        return [r for r in rows if isinstance(r, dict) and r.get("site")] if isinstance(rows, list) else []
    except ValueError:
        return []


def _clean_site(site: Any) -> str:
    name = " ".join(str(site or "").split())[:60]
    for prefix in ("https://", "http://", "www."):
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
    return name.rstrip("/")


def track(store, site: Any, status: Any, note: Any = "", *, now: Optional[float] = None) -> Tuple[bool, str]:
    name = _clean_site(site)
    if not name:
        return False, "Which site?"
    status = str(status or "").strip().lower()
    if status not in STATUSES:
        return False, f"Status must be one of: {', '.join(STATUSES)}."
    now = time.time() if now is None else now
    rows = _load(store)
    row = next((r for r in rows if r["site"].lower() == name.lower()), None)
    if row is None:
        if len(rows) >= MAX_SITES:
            return False, f"The list is full ({MAX_SITES} sites)."
        row = {"site": name, "history": []}
        rows.append(row)
    row["status"], row["since"] = status, now
    if status == "sent":
        row["sent"] = now
    row["note"] = " ".join(str(note or "").split())[:200]
    row["history"] = (row.get("history") or [])[-9:] + [[status, now]]
    store.set(KEY, json.dumps(rows))
    return True, f"{name}: {status}."


def status(store, *, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    rows = _load(store)
    if not rows:
        return ("No removal requests tracked yet. Start with California's DROP (" + DROP_URL + "), then the "
                "people-search sites; action 'plan' has the steps.")
    lines, nudge = [], []
    for r in sorted(rows, key=lambda r: r["site"].lower()):
        since = r["since"] if isinstance(r.get("since"), (int, float)) else now
        days = int((now - since) // 86400)
        lines.append(f"{r['site']}: {r.get('status', '?')} ({days} day{'s' if days != 1 else ''} ago)"
                     + (f", {r['note']}" if r.get("note") else ""))
        sent = r["sent"] if isinstance(r.get("sent"), (int, float)) else since
        if r.get("status") == "sent" and now - sent > FOLLOW_UP_DAYS * 86400:
            nudge.append(r["site"])
        if r.get("status") == "reappeared":
            nudge.append(r["site"] + " (listed again)")
    out = "\n".join(lines)
    if nudge:
        out += "\nNeeds a follow-up: " + ", ".join(nudge)
    return out


DESCRIPTION = ("Help Sandeep get his personal details removed from data brokers and people-search sites, and "
               "track the requests. Actions: 'plan' (the steps, starting with California's DROP), 'track' (site, "
               "status: found/sent/confirmed/removed/reappeared/not listed, optional note), 'status' (the list and "
               "what needs a follow-up). Searching for his own listings sends his name to the search engine: do "
               "it only when he asks, and say first what you'll search for. Never fill identity checks or payment "
               "pages.")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["action"], "properties": {
    "action": {"type": "string", "enum": ["plan", "track", "status"]},
    "site": {"type": "string"}, "status": {"type": "string", "enum": list(STATUSES)}, "note": {"type": "string"}}}


def attach(store, tools: Dict[str, Any], clock: Callable[[], float] = time.time) -> None:
    from .toolbox import Tool, ToolOutput

    def run(args: Dict[str, Any], ctx) -> "ToolOutput":
        action = args.get("action")
        if action == "plan":
            return ToolOutput(PLAN.format(url=DROP_URL, sites=", ".join(SITES), days=FOLLOW_UP_DAYS))
        if action == "status":
            return ToolOutput(status(store, now=clock()))
        if action == "track":
            ok, said = track(store, args.get("site"), args.get("status"), args.get("note", ""), now=clock())
            return ToolOutput(said) if ok else ToolOutput.error(said)
        return ToolOutput.error("action must be plan, track or status")

    tools["privacy"] = Tool("privacy", DESCRIPTION, SCHEMA, run, frozenset({"voice", "phone", "cli", "android",
                                                                           "scheduler"}))
