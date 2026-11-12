"""Your Google account, read-only: Gmail, Google Drive and Google Calendar.

How it connects: `argusctl google connect` signs in through your browser with your own Google Cloud "Desktop
app" client (docs/GOOGLE.md), using Google's sign-in for desktop apps (a one-time code sent back to
127.0.0.1 on this Mac, protected with PKCE and a random state). Argus asks only for read-only permissions:

  gmail.readonly, drive.readonly, calendar.readonly

so Google itself refuses anything else (sending, deleting, editing), whatever Argus or Claude ask for. The
refresh token is kept in secrets.env (owner-only); the hour-long access tokens only in memory. Both are
removed from anything Claude sees. `argusctl google disconnect` revokes them at Google.

Guarding what comes back:
* Security-code and password mails, and mail from your never-touch sites, are left out (as for the Mail app).
* Drive files whose names look like passwords, recovery codes or keys are left out.
* Everything is labelled as other people's words, so instructions inside a mail or document aren't followed.

Speed: the details of each search result are fetched in parallel, and connections are reused.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import json
import re
import secrets as secrets_mod
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .llm import connect
from .mail import UNTRUSTED, hidden

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
DRIVE = "https://www.googleapis.com/drive/v3"
CALENDAR = "https://www.googleapis.com/calendar/v3"
SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/drive.readonly",
          "https://www.googleapis.com/auth/calendar.readonly")
SECRET_KEYS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
MAX_RESULTS = 15
MAX_TEXT = 8000
TIMEOUT = 20.0
EXPIRED = ("Google's sign-in for Argus has ended (Google ends it after 7 days while the Google Cloud app is "
           "in Testing). On the Mac run: argusctl google connect")
_PRIVATE_FILE = re.compile(r"password|passcode|passwd|recovery|backup codes?|2fa|two[- ]factor|seed phrase|"
                           r"private key|\.pem\b|\.p8\b|\.key\b|secrets?\b|credentials?|ssn|social security", re.I)

Request = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, bytes]]


class GoogleError(RuntimeError):
    pass


def send(method: str, url: str, headers: Dict[str, str], data: Optional[bytes] = None,
         timeout: float = TIMEOUT) -> Tuple[int, bytes]:
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    conn = connect(url, timeout)
    try:
        conn.request(method, path, body=data, headers=dict({"User-Agent": "Argus/2"}, **headers))
        resp = conn.getresponse()
        return resp.status, resp.read(5_000_000)
    except (OSError, http.client.HTTPException) as exc:
        raise GoogleError(f"couldn't reach {parts.hostname} ({exc or type(exc).__name__})") from exc
    finally:
        conn.close()


def _json(raw: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _form(values: Dict[str, str]) -> Tuple[Dict[str, str], bytes]:
    return {"Content-Type": "application/x-www-form-urlencoded"}, urlencode(values).encode()


# ---- signing in ------------------------------------------------------------------------------------------------
class Auth:
    """Hands out an access token, refreshing it from the saved refresh token when it runs out."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *, request: Request = send,
                 clock: Callable[[], float] = time.time, on_token: Callable[[str], None] = lambda _t: None):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.request, self.clock, self.on_token = request, clock, on_token
        self._token, self._until = "", 0.0
        self._lock = threading.Lock()
        self.scopes: List[str] = []

    def token(self) -> str:
        with self._lock:
            if self._token and self.clock() < self._until - 60:
                return self._token
            headers, body = _form({"client_id": self.client_id, "client_secret": self.client_secret,
                                   "grant_type": "refresh_token", "refresh_token": self.refresh_token})
            status, raw = self.request("POST", TOKEN_URL, headers, body)
            data = _json(raw)
            if status != 200 or not data.get("access_token"):
                if data.get("error") in ("invalid_grant", "unauthorized_client"):
                    raise GoogleError(EXPIRED)
                if data.get("error") == "invalid_client":
                    raise GoogleError("Google didn't recognise the Argus client (run argusctl google connect again)")
                raise GoogleError(f"Google sign-in failed (HTTP {status} {data.get('error') or ''})".strip())
            self._token = str(data["access_token"])
            self._until = self.clock() + float(data.get("expires_in") or 3600)
            self.scopes = str(data.get("scope") or "").split()
            self.on_token(self._token)
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token, self._until = "", 0.0


def pkce() -> Tuple[str, str]:
    verifier = secrets_mod.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def sign_in(client_id: str, client_secret: str, *, open_browser: Callable[[str], Any], request: Request = send,
            wait: float = 300.0, login_hint: str = "") -> Dict[str, Any]:
    """The browser sign-in. Returns Google's token reply (with refresh_token)."""
    verifier, challenge = pkce()
    state = secrets_mod.token_urlsafe(24)
    got: Dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):      # noqa: N802 (the standard library's name)
            query = parse_qs(urlsplit(self.path).query)
            if urlsplit(self.path).path != "/" or not query:
                self.send_response(404)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in query.items()})
            ok = got.get("state") == state and "code" in got
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            message = ("Argus is connected to your Google account. You can close this tab."
                       if ok else "Argus couldn't connect. Go back to the Terminal to see why.")
            self.wfile.write(f"<!doctype html><title>Argus</title><p style='font:18px system-ui'>{message}</p>"
                             .encode())
            done.set()

        def log_message(self, *args):   # keep the code out of the Terminal
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    params = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
              "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"}
    if login_hint:
        params["login_hint"] = login_hint
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        open_browser(f"{AUTH_URL}?{urlencode(params)}")
        if not done.wait(wait):
            raise GoogleError("no answer from the browser within 5 minutes")
    finally:
        server.shutdown()
        server.server_close()
    if got.get("state") != state:
        raise GoogleError("the answer from the browser didn't match this sign-in (try again)")
    if "error" in got:
        raise GoogleError(f"Google said: {got['error']}" + (" (you clicked Cancel)" if got["error"] == "access_denied" else ""))
    headers, body = _form({"client_id": client_id, "client_secret": client_secret, "code": got.get("code", ""),
                           "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect})
    status, raw = request("POST", TOKEN_URL, headers, body)
    data = _json(raw)
    if status != 200 or not data.get("refresh_token"):
        raise GoogleError(f"Google didn't give Argus a sign-in token (HTTP {status} {data.get('error') or ''})".strip())
    granted = set(str(data.get("scope") or "").split())
    missing = [s.rsplit("/", 1)[1] for s in SCOPES if s not in granted]
    data["missing"] = missing
    return data


def revoke(token: str, request: Request = send) -> bool:
    headers, body = _form({"token": token})
    status, _ = request("POST", REVOKE_URL, headers, body)
    return status == 200


# ---- reading -------------------------------------------------------------------------------------------------
def _b64(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def _body_text(payload: Dict[str, Any]) -> str:
    """The readable text of a Gmail message: its text/plain part, else its HTML as text."""
    from .toolbox import html_to_text
    plain: List[str] = []
    html: List[str] = []
    stack = [payload]
    while stack:
        part = stack.pop(0)
        if not isinstance(part, dict):
            continue
        mime = str(part.get("mimeType") or "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            html.append(_b64(data))
        stack.extend(part.get("parts") or [])
    if plain:
        return "\n".join(plain)
    return html_to_text("\n".join(html)) if html else ""


def _headers(message: Dict[str, Any]) -> Dict[str, str]:
    return {str(h.get("name", "")).lower(): str(h.get("value", ""))
            for h in (message.get("payload") or {}).get("headers") or [] if isinstance(h, dict)}


def gmail_query(words: Sequence[str], days: Optional[int], query: str = "") -> str:
    parts = []
    if query.strip():
        parts.append(query.strip()[:300])
    clean = [w.replace('"', "").strip()[:60] for w in words if str(w).strip()]
    if clean:
        terms = [f'"{w}"' if " " in w else w for w in clean[:6]]
        parts.append("(" + " OR ".join(terms) + ")" if len(terms) > 1 else terms[0])
    if days:
        parts.append(f"newer_than:{int(days)}d")
    return " ".join(parts)


DOC_EXPORTS = {"application/vnd.google-apps.document": "text/plain",
               "application/vnd.google-apps.spreadsheet": "text/csv",
               "application/vnd.google-apps.presentation": "text/plain"}
KINDS = {"application/vnd.google-apps.document": "Google Doc", "application/vnd.google-apps.spreadsheet": "Google Sheet",
         "application/vnd.google-apps.presentation": "Google Slides", "application/vnd.google-apps.folder": "folder",
         "application/pdf": "PDF"}


class Google:
    def __init__(self, auth: Auth, *, request: Request = send, blocked_sites: Sequence[str] = (),
                 timezone_name: str = "", workers: int = 8):
        self.auth, self.request = auth, request
        self.blocked_sites = list(blocked_sites)
        self.timezone_name = timezone_name
        self.workers = workers

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None, raw: bool = False) -> Any:
        full = url + ("?" + urlencode(params, doseq=True) if params else "")
        for attempt in (1, 2):
            status, body = self.request("GET", full, {"Authorization": f"Bearer {self.auth.token()}",
                                                      "Accept": "*/*" if raw else "application/json"}, None)
            if status == 401 and attempt == 1:         # the access token ran out early: get a new one once
                self.auth.invalidate()
                continue
            break
        if status == 200:
            return body if raw else _json(body)
        error = _json(body).get("error")
        message = error.get("message", "") if isinstance(error, dict) else str(error or "")
        message = " ".join(message.split())[:200]
        if status == 403 and ("not been used" in message or "disabled" in message):
            api = "Gmail" if "gmail" in url else "Drive" if "drive" in url else "Calendar"
            raise GoogleError(f"the {api} API isn't switched on in your Google Cloud project (docs/GOOGLE.md, step 2)")
        if status == 403 and "insufficient" in message.lower():
            raise GoogleError("Argus wasn't given that permission when you signed in; run argusctl google connect again")
        if status == 404:
            raise GoogleError("that item isn't there any more")
        if status == 429:
            raise GoogleError("Google says too many requests; try again in a minute")
        raise GoogleError(f"Google answered HTTP {status}" + (f" ({message})" if message else ""))

    def _parallel(self, fn, items):
        with ThreadPoolExecutor(max_workers=max(1, min(self.workers, len(items) or 1))) as pool:
            return list(pool.map(fn, items))

    # -- Gmail --
    def mail_search(self, words: Sequence[str], days: Optional[int] = None, query: str = "",
                    limit: int = MAX_RESULTS) -> Tuple[List[Dict[str, str]], int]:
        q = gmail_query(words, days, query)
        if not q:
            raise GoogleError("give at least one word to look for")
        listed = self._get(f"{GMAIL}/messages", {"q": q, "maxResults": min(limit * 2, 50)})
        ids = [m.get("id") for m in listed.get("messages") or [] if isinstance(m, dict) and m.get("id")]

        def details(mid: str) -> Dict[str, Any]:
            return self._get(f"{GMAIL}/messages/{quote(mid, safe='')}",
                             {"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
        found, left_out = [], 0
        for message in self._parallel(details, ids):
            h = _headers(message)
            sender, subject = h.get("from", ""), h.get("subject", "")
            if hidden(sender, subject, self.blocked_sites):
                left_out += 1
                continue
            when = ""
            try:
                when = datetime.fromtimestamp(int(message.get("internalDate", 0)) / 1000).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError, OSError):
                pass
            found.append({"id": str(message.get("id")), "when": when, "from": sender[:120], "subject": subject[:200],
                          "snippet": str(message.get("snippet") or "")[:160]})
        return found[:limit], left_out

    def mail_read(self, mid: str) -> Tuple[Dict[str, str], str]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", str(mid or "")):
            raise GoogleError("search first, then read a result by its id")
        message = self._get(f"{GMAIL}/messages/{mid}", {"format": "full"})
        h = _headers(message)
        text = _body_text(message.get("payload") or {}).strip()
        if hidden(h.get("from", ""), h.get("subject", ""), self.blocked_sites) or hidden("", text[:400], ()):
            raise GoogleError("that message looks like a security code or password mail, which Argus never reads")
        return h, text[:MAX_TEXT]

    # -- Drive --
    def drive_search(self, words: Sequence[str], limit: int = MAX_RESULTS) -> Tuple[List[Dict[str, Any]], int]:
        clean = [str(w).replace("\\", "").replace("'", "\\'").strip()[:60] for w in words if str(w).strip()][:4]
        if not clean:
            raise GoogleError("give at least one word to look for")
        match = " or ".join(f"name contains '{w}' or fullText contains '{w}'" for w in clean)
        data = self._get(f"{DRIVE}/files", {"q": f"({match}) and trashed = false", "pageSize": min(limit * 2, 50),
                                            "orderBy": "modifiedTime desc",
                                            "fields": "files(id,name,mimeType,modifiedTime,size,owners(displayName))"})
        files, left_out = [], 0
        for f in data.get("files") or []:
            if not isinstance(f, dict):
                continue
            if _PRIVATE_FILE.search(str(f.get("name") or "")):
                left_out += 1
                continue
            files.append(f)
        return files[:limit], left_out

    def drive_read(self, file_id: str) -> Tuple[Dict[str, Any], str]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,128}", str(file_id or "")):
            raise GoogleError("search first, then read a file by its id")
        meta = self._get(f"{DRIVE}/files/{file_id}", {"fields": "id,name,mimeType,modifiedTime,size"})
        name, mime = str(meta.get("name") or ""), str(meta.get("mimeType") or "")
        if _PRIVATE_FILE.search(name):
            raise GoogleError("that file's name looks like it holds passwords or keys, so Argus doesn't read it")
        if mime in DOC_EXPORTS:
            body = self._get(f"{DRIVE}/files/{file_id}/export", {"mimeType": DOC_EXPORTS[mime]}, raw=True)
        elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
            if int(meta.get("size") or 0) > 2_000_000:
                raise GoogleError("that file is too big to read out")
            body = self._get(f"{DRIVE}/files/{file_id}", {"alt": "media"}, raw=True)
        else:
            raise GoogleError(f"Argus can read Google Docs, Sheets, Slides and text files, not {KINDS.get(mime, mime)}")
        return meta, body.decode("utf-8", "replace")[:MAX_TEXT]

    # -- Calendar --
    def events(self, start: datetime, end: datetime, query: str = "", limit: int = 25) -> List[Dict[str, Any]]:
        params = {"timeMin": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                  "timeMax": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                  "singleEvents": "true", "orderBy": "startTime", "maxResults": limit}
        if query.strip():
            params["q"] = query.strip()[:100]
        if self.timezone_name:
            params["timeZone"] = self.timezone_name
        return [e for e in self._get(f"{CALENDAR}/calendars/primary/events", params).get("items") or []
                if isinstance(e, dict) and e.get("status") != "cancelled"]


def _local_day(value: str, tz) -> datetime:
    day = datetime.strptime(value, "%Y-%m-%d")
    return day.replace(tzinfo=tz)


def describe_event(event: Dict[str, Any]) -> str:
    start, end = event.get("start") or {}, event.get("end") or {}
    if "date" in start:
        when = f"{start['date']} (all day)"
    else:
        try:
            s = datetime.fromisoformat(str(start.get("dateTime")).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(end.get("dateTime")).replace("Z", "+00:00"))
            when = f"{s:%a %Y-%m-%d %H:%M}-{e:%H:%M}"
        except (TypeError, ValueError):
            when = str(start.get("dateTime") or "?")
    line = f"{when}: {str(event.get('summary') or '(no title)')[:120]}"
    if event.get("location"):
        line += f" @ {str(event['location'])[:120]}"
    others = [a for a in event.get("attendees") or [] if isinstance(a, dict) and not a.get("self")]
    if others:
        line += f" (with {len(others)} other{'s' if len(others) > 1 else ''})"
    if event.get("hangoutLink") or "zoom.us" in str(event.get("location") or "") + str(event.get("description") or ""):
        line += " [video call]"
    return line


# ---- tools ---------------------------------------------------------------------------------------------------
PRIVATE = frozenset({"voice", "phone", "cli"})              # mail and files: only you, asking
CALENDAR_CHANNELS = frozenset({"voice", "phone", "cli", "scheduler"})   # also the morning briefing

MAIL_SEARCH = ("Search the user's Gmail (read-only). Use it for anything that arrives by email: flight and hotel "
               "bookings, orders, deliveries, appointments, replies. Give words (any of them may match, in the "
               "whole message) and/or a Gmail search query (from:, subject:, after:2026/11/01, has:attachment). "
               "Returns date, sender, subject and a snippet; open one with mail_read.")
MAIL_SEARCH_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "words": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
    "query": {"type": "string", "description": "Gmail search syntax, e.g. from:united.com subject:confirmation"},
    "days": {"type": "integer", "minimum": 1, "maximum": 3650, "description": "Only the last N days."}}}
MAIL_READ = "Read one email found by mail_search, by its id. Say only what the user needs from it."
DRIVE_SEARCH = ("Search the user's Google Drive (read-only) by words in file names and contents. Returns name, kind, "
                "last change and id; read a Doc, Sheet, Slides or text file with drive_read.")
DRIVE_READ = "Read a Google Doc, Sheet (as CSV), Slides or text file from Drive by its id (up to ~8,000 characters)."
CALENDAR_DESC = ("The user's Google Calendar events (read-only) between two dates, optionally matching words. Use it "
                 "for 'what's on today/tomorrow/this week', 'when is my dentist appointment', and to check a plan "
                 "against their schedule.")
CALENDAR_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["start"], "properties": {
    "start": {"type": "string", "description": "First day, YYYY-MM-DD (the user's time zone)."},
    "end": {"type": "string", "description": "Last day, YYYY-MM-DD (default: the same day)."},
    "query": {"type": "string", "description": "Only events whose text contains this."}}}
ID_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["id"], "properties": {"id": {"type": "string"}}}


def attach(settings, secrets: Dict[str, str], tools: Dict[str, Any], *, request: Request = send,
           on_token: Callable[[str], None] = lambda _t: None) -> Optional[Google]:
    """Adds the Google tools when an account is connected. Gmail replaces the Mail app's mail_search/mail_read."""
    from .toolbox import Tool, ToolOutput
    client_id, client_secret, refresh = (str(secrets.get(k) or "").strip() for k in SECRET_KEYS)
    if not (client_id and refresh):
        return None
    tz_name = getattr(settings, "timezone", "") or ""
    auth = Auth(client_id, client_secret, refresh, request=request, on_token=on_token)
    google = Google(auth, request=request, blocked_sites=getattr(settings, "blocked_sites", []), timezone_name=tz_name)

    def guarded(fn):
        def run(args: Dict[str, Any], ctx) -> ToolOutput:
            try:
                return fn(args)
            except GoogleError as exc:
                return ToolOutput.error(f"Google: {exc}.")
        return run

    def mail_search(args):
        words = args.get("words") if isinstance(args.get("words"), list) else []
        days = args.get("days") if isinstance(args.get("days"), int) and not isinstance(args.get("days"), bool) else None
        found, left_out = google.mail_search(words, days, str(args.get("query") or ""))
        note = f"\n({left_out} left out: security codes or senders on the never-touch list.)" if left_out else ""
        if not found:
            return ToolOutput("No matching emails." + note)
        lines = [f"{m['id']} | {m['when']} | {m['from']} | {m['subject']} | {m['snippet']}" for m in found]
        return ToolOutput(UNTRUSTED + "\nid | received | from | subject | snippet\n" + "\n".join(lines) + note)

    def mail_read(args):
        h, text = google.mail_read(str(args.get("id") or ""))
        return ToolOutput(f"{UNTRUSTED}\nFrom: {h.get('from', '')}\nDate: {h.get('date', '')}\nSubject: "
                          f"{h.get('subject', '')}\n\n" + (text or "(no readable text; it may be only pictures)"))

    def drive_search(args):
        words = args.get("words") if isinstance(args.get("words"), list) else [str(args.get("words") or "")]
        files, left_out = google.drive_search(words)
        note = f"\n({left_out} left out: names that look like passwords or keys.)" if left_out else ""
        if not files:
            return ToolOutput("No matching files in Drive." + note)
        lines = [f"{f.get('id')} | {str(f.get('modifiedTime', ''))[:10]} | "
                 f"{KINDS.get(str(f.get('mimeType')), str(f.get('mimeType', '')).split('/')[-1])} | {f.get('name')}"
                 for f in files]
        return ToolOutput("id | last changed | kind | name\n" + "\n".join(lines) + note)

    def drive_read(args):
        meta, text = google.drive_read(str(args.get("id") or ""))
        return ToolOutput(f"{UNTRUSTED.replace('emails', 'documents')}\nFile: {meta.get('name')}\n\n{text}")

    def calendar(args):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name) if tz_name else timezone.utc
        except Exception:
            tz = timezone.utc
        try:
            first = _local_day(str(args.get("start") or ""), tz)
            last = _local_day(str(args.get("end") or args.get("start") or ""), tz)
        except ValueError:
            return ToolOutput.error("Dates must look like 2026-11-12.")
        if last < first or (last - first).days > 62:
            return ToolOutput.error("Give a range of up to two months, end after start.")
        events = google.events(first, last + timedelta(days=1), str(args.get("query") or ""))
        if not events:
            return ToolOutput("Nothing on the calendar then.")
        return ToolOutput(UNTRUSTED.replace("emails", "calendar entries") + "\n" +
                          "\n".join(describe_event(e) for e in events))

    tools["mail_search"] = Tool("mail_search", MAIL_SEARCH, MAIL_SEARCH_SCHEMA, guarded(mail_search), PRIVATE)
    tools["mail_read"] = Tool("mail_read", MAIL_READ, ID_SCHEMA, guarded(mail_read), PRIVATE)
    words_schema = {"type": "object", "additionalProperties": False, "required": ["words"], "properties": {
        "words": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4}}}
    tools["drive_search"] = Tool("drive_search", DRIVE_SEARCH, words_schema, guarded(drive_search), PRIVATE)
    tools["drive_read"] = Tool("drive_read", DRIVE_READ, ID_SCHEMA, guarded(drive_read), PRIVATE)
    tools["calendar"] = Tool("calendar", CALENDAR_DESC, CALENDAR_SCHEMA, guarded(calendar), CALENDAR_CHANNELS)
    return google
