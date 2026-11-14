# Argus security

Argus listens all the time and can use your Mac, so it is built to fail safe: when in doubt, it doesn't.
This page lists every protection, where in the code it is enforced, and what it does *not* protect against.
Argus on an Android phone and the always-on server have their own sections (14 and 15).

## 1. Your microphone

| Protection | Where |
|---|---|
| Voice detection, speech recognition and clap detection run on the Mac (sherpa-onnx, Silero VAD, Moonshine). No audio leaves the Mac, ever. | `argus/hearing.py`, `argus/clap.py` |
| Audio is never written to disk. The only exception is `argusctl enroll`, which keeps a voiceprint: a list of numbers, not a recording. | `argus/hearing.py` |
| A sentence is sent to Claude only if it contains the name, comes within 8 s of a reply or a double clap, or conversation mode is on. Everything else is dropped and only *counted*; its text is never logged. | `argus/listener.py`, `argus/daemon.py` |
| While Argus is talking or working, only sentences containing its name count, and its own voice is filtered out, so it doesn't answer itself. | `argus/listener.py`, `argus/daemon.py` |
| A double clap only opens listening (or wakes Argus from sleep); it can't approve or run anything. Single claps, applause, knocks and speech are rejected. | `argus/clap.py` |
| The microphone closes while the Mac is locked (macOS's orange dot goes away). | `argus/guard.py` |
| The microphone also closes while you're on a call at the Mac (another app is using a microphone), so a call is never sent to Claude. The small call helper (`argus-mic`, built from `macos/argus-mic.swift`) only reads *which* apps have a microphone open, through macOS's public audio interface; it never gets any sound. For calls on the iPhone itself, say "Argus, I'm on a call". | `argus/micwatch.py`, `argus/daemon.py` |
| "Argus, go to sleep": nothing is sent until "Argus, wake up" or a double clap. "Argus, turn off the microphone" or the menu closes the mic until you turn it back on. | `argus/wake.py` |
| Voice lock (on once you've run `argusctl enroll`): requests from voices that don't match your voiceprint aren't acted on. If someone else says "Argus", it asks you first ("Should I answer them?"); only your voice can say yes, and "talk to everyone" lasts an hour unless you say "only me". | `argus/hearing.py` (`VoicePrint`), `argus/daemon.py` |
| Guests (other voices you allowed) get a separate conversation with nothing about you in it (no memories, lessons or history), only the web and news tools, and no approvals at all. | `argus/agent.py`, `argus/brain.py`, `argus/prompts.py` |

## 2. What Argus may do: the rules

Every tool call Claude asks for goes through one function, `Policy.check` in `argus/policy.py`, before
anything runs. It answers **allow**, **ask you**, or **block**. Blocked means blocked: there is no
approval that overrides it, and Claude is told not to look for a way around it.

**Blocked, always** (even if you would approve):
* `sudo` and other ways of becoming administrator; turning off macOS protections (`csrutil`, `spctl`,
  `nvram`); changing privacy permissions (`tccutil`).
* Deleting your whole disk, home folder or system folders; erasing or raw-writing disks; fork bombs;
  shutting down or logging out; running scripts piped straight from the internet (`curl … | sh`).
* Anything in a place that holds passwords, keys or logins (listed in `argus/protected.py`): all of
  `~/.argus`, `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.docker`, `~/.kube`, the GitHub and Google Cloud command
  line logins, Keychains, Cookies, Safari, the Chrome, Brave, Edge and Firefox profiles, and `.netrc`,
  `.git-credentials`, `.npmrc`, `.pypirc`. The file tools refuse them (including through a symbolic link),
  searches skip them, a shell command that names one is blocked, and no command may start inside one.
  Reading passwords from the keychain with `security` is blocked too.
* **On the Mac, every shell command also runs inside a sandbox enforced by macOS itself** (`sandbox-exec`)
  that can't read or change those places, so even a command disguised to slip past the rules above (a
  wildcard, an encoded path) is stopped by the kernel.
* Anything that mentions a website on your never-touch list (in a link, a command, typed text or a search).
* Opening, looking at or touching a never-touch app (password managers, Keychain Access, System
  Settings, plus any you add), including when one is merely visible on screen.
* Screen use while the Mac is locked, while "Allow Argus to see and use the screen" is off, or when the
  app in front can't be identified. Key combinations that log out, lock, force-quit or empty the trash.
  Typing `sudo`, recursive deletes or `curl | sh` into an app.

**Needs your OK** (a dialog on the Mac + Touch ID):
* Shell commands that delete, move or overwrite files; change permissions; stop programs; change macOS
  settings; script other apps (`osascript`); install or remove software; push or reset git; send data to
  the internet; connect to other computers; send email; schedule commands; run inline scripts or nested
  shells; or write outside the workspace folder. (These patterns started from Hermes Agent's dangerous-command
  list, MIT, and were extended for macOS and Argus.)
* Writing files outside the workspace folder (`~/argus-workspace`).
* Every click, drag, scroll, key press and typed text on the screen (looking is allowed; touching asks).
* Creating a scheduled task that will run by itself later (reminders don't ask).
* Opening or reading a web address with a very long query (over 300 characters): that's how a page could
  trick an AI into carrying your data out.

**Allowed without asking:** answering, web search, reading public web pages, news, memory, reminders,
reading files, working inside the workspace folder, opening apps and links, and screenshots when you ask.

## 3. Approvals

| Protection | Where |
|---|---|
| Argus collects approvals itself. A "yes" inside the conversation never counts, and Claude is told never to ask for one. | `argus/prompts.py`, `argus/agent.py` |
| At the Mac, a dialog shows exactly what will happen; "Allow" then needs Touch ID or your Mac password, so a TV, a call, a recording, or the AI itself clicking the dialog can't approve. | `argus/approvals.py` |
| You can also answer out loud: Argus reads out a random code word, and only your enrolled voice (a stricter match than voice lock) saying "yes" and that word counts. The fresh word stops a TV or a recording; the voice match stops other people. "No" needs no word. Without an enrolled voice, there are no spoken approvals. Whichever answers first, voice or dialog, wins and the other is withdrawn. | `argus/approvals.py`, `argus/daemon.py` |
| Paying is Touch ID only. A click on a button labelled Buy, Pay, Place order, Subscribe and the like (read through the Mac's accessibility interface), any input on a page titled checkout, payment or billing, and typing anything that passes the card-number checksum need Touch ID, even when you allowed screen use by voice. Labels can be missing, so Claude is also told to stop before paying. | `argus/policy.py` |
| Your iPhone (through iPhone Mirroring) gets its own approval: allowing the Mac's screen doesn't extend to the phone, because Argus can't tell which iPhone app is open. | `argus/policy.py` |
| Answers are *once* or *for this task* (the same kind of action, until the task ends), never "always". | `argus/agent.py`, `argus/approvals.py` |
| No answer within 2 minutes = denied. | `argus/approvals.py` |
| Scheduled tasks, the iPhone and guests can't approve anything: risky steps are refused and you're told. | `argus/app.py`, `argus/agent.py`, `argus/brain.py` |
| A dialog that crashes, hangs or can't open counts as "no", and so does any answer other than *once* or *for this task*. | `argus/agent.py`, `argus/approvals.py` |
| After a failed screen action, the rest of that batch is not carried out. | `argus/agent.py` |

## 4. Channels

| Channel | Screen | Approvals |
|---|---|---|
| Voice at the Mac | look and use (each use asks) | dialog + Touch ID, or your voice + code word (paying: Touch ID only) |
| Guests at the Mac (voices you allowed) | none | none: nothing risky, nothing personal |
| iPhone (Siri, Argus Eye app) | screenshots only | none: risky steps are refused |
| Scheduled tasks | none | none: risky steps are refused |
| Argus on Android (on the phone) | none | a dialog on the phone, then your fingerprint |
| The always-on server (the phone's shortcut) | none | none: risky steps are refused |

The glasses camera is used only from the Mac or the phone when you ask, never by a scheduled task.
There is no text chat (Telegram was removed): you talk to Argus by voice, at the Mac or on the iPhone.

## 5. The web and prompt injection

* Argus's own page reader only fetches public internet addresses: every address a name resolves to must
  be public (no `localhost`, home network, router, or cloud metadata addresses), the connection goes to
  the address that was checked (so DNS tricks can't swap it), redirects are checked again, and pages are
  capped at 3 MB. Only `http` and `https`.
* Claude's web search runs with your never-touch sites as blocked domains.
* Claude is told to treat web pages, emails, files, screenshots and photos as information, never as
  instructions, even when they claim to come from you or from Anthropic. If it's fooled anyway, the rules
  and approvals above still apply.

## 6. Network exposure

* The phone gateway listens **only on the Mac's (or the server's) Tailscale address** (your private,
  encrypted network) and refuses to start on any other interface. Every request needs a 256-bit token (constant-time
  comparison); 5 bad tokens lock that device out for 15 minutes; bodies are capped at 16 MB; slow clients
  are cut off after 60 s; only PNG and JPEG images are accepted; at most 2 requests run at once.
* The control socket for `argusctl` is a file in `~/.argus` that only you can open.
* Nothing opens a port on your router or your Wi-Fi.

## 7. Secrets and your Claude account

* Keys are typed with hidden input and written only to `~/.argus/secrets.env` (permissions 600)
  through stdin, never as command-line arguments, which other programs can read. They are never in this
  repo or in `config.toml`. Argus refuses to start if the secrets file is readable by others.
* Commands Argus runs get a minimal environment (path, home, language): your API keys and tokens never
  reach them.
* Argus's own keys (the Claude key and the phone token) are removed from anything a
  tool returns before Claude sees it, and a tool call that contains one of them is blocked, so a web page
  can't talk Argus into sending a key anywhere.
* Argus uses its own pay-per-use Claude API key. It never borrows your Claude Pro or Claude Code login.
* Each request has a token budget (`max_turn_tokens`) and a step limit, so a runaway task stops before it
  gets expensive. `argusctl usage` shows the estimated spend; set a monthly limit in the Claude console too.
* `~/.argus` is 700; its settings, database, activity log, phone token, voiceprint and control socket are 600.

## 8. Supply chain

* Python comes from `uv`, which setup downloads at a pinned version and checks against a pinned SHA-256.
* Argus's Python packages are installed from `requirements-macos.lock` with `--require-hashes
  --only-binary :all:`: every file is checked against a pinned SHA-256, and all of them are ready-built
  except one. `rumps` (the menu bar, pure Python) exists on PyPI only as source, so it is built on your Mac
  from its hash-checked source with a hash-pinned `setuptools` (`requirements-build.lock`) installed first,
  without build isolation, so the build downloads nothing unchecked.
* The speech and voice models are downloaded from the sherpa-onnx GitHub releases and checked against
  SHA-256 hashes pinned in `setup.sh`; a mismatch stops the install.
* `Argus.app` is compiled on your Mac from `macos/launcher.c` (about 50 lines) and signed locally, so
  macOS asks about the microphone, Screen Recording and Accessibility for *Argus*, not for Terminal or
  Python.
* The iPhone app pulls Meta's toolkit at an exact version (1.0.1) through Swift Package Manager.
* Phone calls (optional, `scripts/setup_calls.sh`): BlackHole and SwitchAudioSource come from Homebrew;
  the audiotee call recorder is built on your Mac from a pinned commit of its source (git checks the
  content against the commit hash), and it has no other dependencies.
* The tests check every macOS function name Argus calls through PyObjC against the real PyObjC 13
  packages, so a typo in Mac-only code can't hide until it runs on your Mac.
* The Apple Maps helper is compiled on your Mac from `macos/argus-maps.swift` (about 100 lines, Apple's
  MapKit only) and rebuilt when that file changes.
* Android: Termux's packages come from Termux's signed package repository. The WhatsApp backup reader
  (wa-crypt-tools, GPL-3.0) is optional; it's installed into its own folder from
  `scripts/wa-crypt-tools.lock` with `--require-hashes` (every file, including the one built from source,
  checked against its PyPI hash) and run as a separate program.
* The always-on server needs no packages at all: Argus runs on the Python the server already has, copied
  to a root-owned folder it can't change.

## 9. Settings that can't silently go wrong

A typo or a wrong kind of value in `~/.argus/config.toml` (an unknown setting, `"false"` in quotes, a
number out of range, an unknown time zone or schedule) stops Argus with a plain message saying which
setting to fix, instead of starting with a value you didn't mean (`argus/config.py`).

## 10. The activity log

`~/.argus/logs/audit.jsonl` (`argusctl log`) records: requests you made (text), tools that ran (name
and a short preview), every approval and its answer, every block, estimated cost per request, microphone
and lock changes, claps, reminders held while you were away, phone-gateway requests and bad tokens. It never records audio
or speech that wasn't for Argus. It rotates at 5 MB.

## 11. Lessons

| Protection | Where |
|---|---|
| Lessons are notes the model reads, kept in Argus's private database on the Mac. They can't change the rules above: those are code. | `argus/lessons.py`, `argus/policy.py` |
| A web page, email or file could try to plant a "lesson", so lessons that mention approvals, permissions or safety, contain links, email addresses, long numbers, passwords or keys, or tell Argus to send something somewhere are refused, whether you or Argus wrote them. | `argus/lessons.py` |
| Argus's own lessons are written by a small model that sees only a log of which steps failed (tool names, short arguments and error messages), never page contents, and is told the log isn't instructions. | `argus/lessons.py` |
| `argusctl lessons` shows every lesson and where it came from; forget one, or all. | `argus/__main__.py` |

## 12. Phone calls

| Protection | Where |
|---|---|
| Off until you run `scripts/setup_calls.sh`. A call you ask for yourself dials straight away: from the Mac's microphone only while voice lock is on (only your enrolled voice is answered), or from the phone app or command line (both need your key). Anything else (a guest, a scheduled task, a loose-end check, a voice that wasn't checked) still needs your OK (voice + code word or Touch ID). `calls_ask_first = true` under `[calls]` brings back the OK for every call. | `argus/policy.py`, `argus/calls.py` |
| Argus always starts by saying it's your AI assistant and that the call is transcribed, before the model says anything. | `argus/calls.py` |
| Never called: emergency numbers (911, 112, 999, 988…), service numbers (N11), pay-per-call numbers (900, 976), invalid numbers, and anything outside the US and Canada unless you switch that on. At most 6 calls an hour, the same number at most twice in 15 minutes, 6 minutes per call. | `argus/calls.py` |
| The call model is told never to give card, bank, account or ID numbers or addresses and never to agree to costs; anything that passes the card-number checksum is also kept out of what's spoken. | `argus/calls.py` |
| Nothing opens to the internet: the call goes through your own iPhone. The Mac's microphone setting is switched to a virtual cable only for the call and always put back (also on errors, and at once when you say "let me talk"). Argus's own voice is muted during a call so the other side never hears Argus talking to you. | `argus/calls.py` |
| To place and end the call, Argus clicks the green Call and red End buttons that appear on screen; it only clicks a button that wasn't there before it dialled. These two clicks are part of the call you approved, not general screen use. | `argus/calls.py` |
| The call recorder (audiotee, MIT) is built on your Mac from a pinned commit of its source, and needs macOS's "System Audio Recording" permission; no audio is saved, only the transcript in Argus's history. | `scripts/setup_calls.sh` |

## 13. Your location and directions

| Protection | Where |
|---|---|
| The phone sends its location only with a request (the Shortcut's Get Current Location step, or Android when a request needs it). Nothing tracks you in the background; Android's drive assist (off unless you switch it on) checks GPS only while the phone is charging. | `argus/phone.py`, `argus/android_app.py` |
| The location goes to the maps services that are set up (Apple, Google, TomTom) for that one trip. Claude is told only "your current location", and navigation links leave the start empty so the maps app uses where the phone is: the coordinates never enter the conversation. | `argus/maps.py` |
| The incident check sends TomTom small boxes that follow the routes, not your position. The news search sends Google News only the names of up to three main roads on the way: never where you are, where you're going, or the route. | `argus/traffic.py` |
| Turn-by-turn steps and incident descriptions reach Claude as text, so road names along the route do. From "here", the turns in the first 250 metres are left out, because they would name the street you are on. | `argus/maps.py` (`past_the_start`) |
| Headlines and incident texts are other people's words: cut short, labelled unverified, and never able to change the ranking (headlines) or do anything but add minutes to a route (incidents). | `argus/traffic.py`, `argus/prompts.py` |
| Weather: the forecast service (Open-Meteo) and the US weather service (weather.gov) get a place rounded to two decimals (about a kilometre), never your exact position. Claude is told "where the user is now", not the coordinates. | `argus/weather.py` |
| At the Mac, "here" can mean where your phone was: the last location the phone sent is kept **in memory only** (never on disk or in logs), used for the weather for 3 hours and as a route's start for 20 minutes, and never for a guest. | `argus/weather.py` (`RecentPlace`), `argus/maps.py` |
| Trip checks run by themselves, so they get only the weather and route tools, and the text they run is built by Argus from the trip (not free text). That's why watching a trip doesn't need your approval, while other scheduled tasks do. At most 10 at a time. | `argus/weather.py`, `argus/prompts.py` |
| Waze is never read: it has no public service and forbids automatic access. Argus only makes a link that opens Waze. | `argus/maps.py` |
| The activity log records that a request came with a location, never the location itself. | `argus/phone.py` |
| A malformed location (out of range, not a number, "0,0") is ignored. | `argus/maps.py` (`clean_location`) |
| Maps keys (`GOOGLE_MAPS_API_KEY`, `APPLE_MAPS_TOKEN`, `TOMTOM_API_KEY`) live in `secrets.env` like the others. TomTom takes its key in the request address (over HTTPS); it is removed from any error message. `argusctl maps-token` signs the Apple Maps token with OpenSSL; your Maps private key is read only by OpenSSL and isn't copied. | `argus/maps.py`, `argus/__main__.py` |

## 14. Argus on Android

| Protection | Where |
|---|---|
| Everything Argus keeps (settings, keys, conversations, the notification log, the decrypted WhatsApp copy) is in Termux's private storage, which other apps can't open; `~/.argus` is 700, the files 600, `~/.argus/private` 700. | `argus/android.py`, `scripts/setup_android.sh` |
| No shell commands: Android gives Termux no sandbox to box them in, so the tool isn't there at all. The file tools refuse Argus's own folder (`~/.argus`), as on the Mac. | `argus/android_app.py`, `argus/policy.py` |
| Sending a text always asks: a dialog shows the number and the exact message, then Android's fingerprint prompt. Back, a cancelled dialog, a failed fingerprint or any error means no. Texts to short codes (fewer than 7 digits, which can cost money) and texts containing a card number are refused outright. | `argus/android.py` (`AndroidApprover`), `argus/policy.py` |
| Messages found by a search are passed to Claude labelled as other people's words, information only; Claude is told never to follow instructions in messages or notifications. Only matching messages are sent, at most 50, each cut to 500 characters. | `argus/android.py`, `argus/prompts.py` |
| WhatsApp: the backup is decrypted on the phone with the key you gave setup, into `~/.argus/private/whatsapp.db`, and again only when WhatsApp writes a newer backup. The decrypter gets a clean environment (no Argus keys); if it fails, its message is shown with the key replaced by `[key]`. The key goes to the decrypter on its command line, which on Android only Termux itself can see. | `argus/android.py` (`WhatsAppBackup`) |
| The notification log keeps message notifications for 90 days (`notification_days`), skips the system's and Termux's own, and is searched only when you ask. | `argus/android.py` (`NotificationLog`) |
| Text goes to Termux:API commands on stdin or as single arguments, never through a shell, so a message can't turn into a command. | `argus/android.py` (`Termux`) |
| **Not on-device:** speech recognition is Android's own (it may send audio to Google unless offline recognition is on). | `docs/ANDROID.md` |

## 15. The always-on server

| Protection | Where |
|---|---|
| Runs as its own `argus` user (no password, no login shell) under a locked-down systemd service: writable only `/var/lib/argus`; no new privileges, no capabilities; private `/tmp` and devices; home folders, other processes and kernel settings hidden; only IPv4, IPv6 and local sockets; a system-call filter. | `scripts/setup_server.sh` |
| Argus's code is in `/opt/argus`, owned by root: the service can't change its own code. It's started with `python3 -I`, so nothing from the environment or the current folder is loaded. | `scripts/setup_server.sh` |
| No shell commands, no screen, no apps; file tools only inside its own workspace folder; anything that would need your OK is refused, because there's no one there to approve it. | `argus/server.py`, `argus/policy.py` |
| The phone gateway, token and lockout are the same as on the Mac, on the server's Tailscale address only; it waits for Tailscale rather than listening anywhere else. | `argus/server.py`, `argus/phone.py` |
| `argus-admin` runs `argusctl` as the `argus` user with a clean environment; only an administrator (sudo) can use it. | `scripts/setup_server.sh` |
| Use a separate Claude key for the server, so it can be cancelled on its own. The server keeps its own memory; nothing on it reads the Mac. | `docs/SERVER.md` |

## 16. Push notifications

* Optional (`argusctl push on`, or the server). Messages go to an ntfy server you choose (ntfy.sh by
  default) under a topic of about 190 random bits kept in `secrets.env`. Anyone who knows the topic can read
  the messages, so treat it like a password; ntfy.sh keeps messages for up to 12 hours. For full privacy, run
  your own ntfy server inside Tailscale (plain `http` is refused anywhere else). A failed push never stops
  Argus; it's logged. (`argus/push.py`)

## 17. What this does *not* protect against

* **Voice is not strong authentication.** Voice lock and spoken approvals rely on a voiceprint, and a
  good imitation or clone of your voice could pass (the code word stops replays of old recordings, not a
  live imitation). That's why paying always needs Touch ID. If that's a concern, set
  `voice_approvals = false` under `[security]`.
* **Spotting payments is best effort.** A buy button with no readable label on a page with an ordinary
  title wouldn't be recognised; Claude is told to stop before paying, but read what you approve.
* **Phone calls carry what Argus says.** The call model could misunderstand or say something you
  wouldn't; it only knows the goal and details you gave it, and the transcript tells you what was said.
  Rules on AI phone calls differ by place; keep it to businesses you'd call yourself.
* **What Argus sees, Claude sees.** When you ask it to look at the screen or through your glasses, the
  picture goes to Anthropic's API (as with any cloud AI). Keep private things out of view, or block the app.
* **A stolen, unlocked Mac** or a compromised user account gives the same access you have.
* **Shell rules are patterns.** They catch the common dangerous commands, and the macOS sandbox guards
  your keys, but a cleverly disguised command might still reach something else. Read approval dialogs
  before allowing, and keep important files backed up (Time Machine).
* **The model can be wrong** or be misled by content it reads.
* **On Android, anything with access to your unlocked phone and Termux** has the same access Argus has.
  Keep a screen lock; the fingerprint step protects sending, not reading.
* **Push notifications pass through the ntfy server** (unless you run your own), and the topic is the only
  thing keeping them private.

## 18. Kill switches

* Menu bar ◉ → Microphone (off) or Quit Argus; or say "Argus, turn off the microphone".
* `argusctl quit` stops everything (voice, phone gateway, schedules).
* Revoke the Claude key at platform.claude.com.
* Delete `~/.argus/phone_token` and run `argusctl restart` to cut off the iPhone (then
  `argusctl phone-token` makes a new one).
* Android: `pkill -f "argus android serve"`, and remove `~/.termux/boot/argus` so it doesn't start again.
* Server: `sudo systemctl disable --now argus`.
* `argusctl push off` stops notifications from the Mac; change the topic (`argusctl push on`) if it leaked.

Found a problem? Open a private issue in this repository.

## 19. Your mail

| Protection | Where |
|---|---|
| Off until you switch it on in setup (`[mail] enabled`). Only on the Mac, and only for you: guests, scheduled tasks and trip checks don't get the mail tools. | `argus/mail.py`, `argus/app.py` |
| No mail password, app password or token is stored. Argus asks the Mail app through two fixed AppleScripts (search subjects and senders; read one message). macOS asks you once whether Argus may control Mail (Privacy & Security → Automation). | `argus/mail.py` |
| Read-only by construction: there is no script that sends, deletes, moves, marks or forwards, and Claude can't supply script text, only search words and a result's id. A test checks the scripts contain no such verbs. | `argus/mail.py`, `tests/test_mail_flights.py` |
| Left out before Claude sees anything: messages whose subject looks like a security code, sign-in link or password reset, and every message from a site on your never-touch list. A message whose text starts like a code mail can't be read either. | `argus/mail.py` (`hidden`) |
| Results are labelled as other people's words, and the prompt tells Claude never to follow instructions inside them. Sending anything onward would still need your approval. | `argus/mail.py`, `argus/prompts.py` |
| What it doesn't protect: the emails Argus reads for you are sent to Claude to answer your question, like anything else you ask about. | |

## 20. Flight history

Only the flight number is sent to FlightAware, with your own key (`FLIGHTAWARE_API_KEY` in `secrets.env`).
Without a key the tool isn't offered.

## 21. Problem reports

`argusctl report` leaves out what you said and what Argus answered, and removes keys, tokens, email addresses
and your home folder's name (`argus/diagnose.py`). The file is readable only by you. Read it before sending it.

## 22. Your Google account

| Protection | Where |
|---|---|
| Read-only permissions only (`gmail.readonly`, `drive.readonly`, `calendar.readonly`): Google refuses sending, deleting or editing whatever is asked. | `argus/google.py` (`SCOPES`) |
| Your own Google Cloud client; sign-in through your browser with PKCE and a random state, answered only on 127.0.0.1 on this Mac. A forged or mismatched answer is refused. | `argus/google.py` (`sign_in`) |
| The refresh token lives in `secrets.env` (owner-only); access tokens (one hour) only in memory, and are removed from anything Claude sees, like keys. `argusctl google disconnect` revokes them at Google. | `argus/google.py`, `argus/app.py` |
| Mail and Drive only for you asking (voice, phone, command line), never for guests or scheduled tasks; the calendar also for scheduled tasks (the morning briefing). | `argus/google.py` |
| Security-code and password mails, never-touch senders, and Drive files named like passwords, recovery codes or keys are left out; everything read is labelled as other people's words. | `argus/google.py`, `argus/mail.py` |

## 23. Lookups at the same time

Only tools that look things up, and only when the rules allow them without asking, run at the same time
(`PARALLEL_SAFE` in `argus/agent.py`). Anything that needs your OK, and anything on the screen, still runs
one step at a time, in order.

## 24. Google search, reviews and other AI models

| Protection | Where |
|---|---|
| Only the search words or the question go to Google Maps, Gemini or a council member (ChatGPT, Grok, Groq, Mistral, OpenRouter): never memory, mail, files or the conversation (each tool's description tells Claude to send nothing personal). On free tiers (Gemini, Mistral, OpenRouter's free models) the provider may use what's sent to improve its models; Mistral's admin console can switch that off, and xAI's data-sharing program should stay off. | `argus/search.py`, `argus/council.py` |
| Before a question leaves the Mac for another AI model, email addresses, phone numbers, long numbers (cards, accounts, bookings) and your name are taken out of it, whatever Claude wrote. | `argus/council.py` (`scrub`) |
| Pay-per-use brains (ChatGPT, Grok) are capped at `paid_per_day` questions a day each (40), counted on the Mac, so a loop or a busy day can't run up a bill. | `argus/council.py` (`DailyCount`) |
| Reviews, pages and other models' answers are labelled as information, never instructions; Claude stays the one that decides and answers. | `argus/search.py`, `argus/council.py` |
| Keys live in secrets.env and are removed from error messages. Council members must use https. | `argus/search.py`, `argus/council.py`, `argus/config.py` |

Claude can also write and run code with Anthropic's code-execution tool (`[brain] code_execution`, on). It runs
in Anthropic's sandbox, which has no internet and nothing of your Mac; only what Claude puts into the code goes
there. It's offered for your own requests, not for guests, scheduled jobs or the quick model.

## 25. Talking over Argus

Stopping when you talk uses the same on-device voice check as the voice lock: nothing extra is recorded or
sent. While Argus speaks, the microphone audio is checked for your voice (or its name) and then thrown away,
as before. Right after it stops, what it hears next needs no name, but the echo of its own sentence is still
filtered out (and checked against your voiceprint when you have one), so it can't talk itself into answering
its own words.

## 26. Booking by phone, loose ends, and what the glasses saw

| Protection | Where |
|---|---|
| A call is only made when you asked for it yourself (see above) or after your OK, with the existing limits (no emergency or paid numbers, 6 an hour, never card numbers). | `argus/policy.py`, `argus/calls.py` |
| A loose-end check runs on its own channel with look-up tools only (search, places, weather, flights, calendar): no files, shell, mail, screen, calls or messages, and nothing can be approved there. At most 12, each for at most 90 days, quiet at night. | `argus/followups.py` |
| Glances are off unless you set `glances_minutes`. Each glance sends one photo to Claude's quick model with a single tool (note where belongings are); people, screens and documents are not to be noted; the photo is dropped from the conversation store straight away and never written to disk or the logs. Sightings are a few words, kept 14 days in Argus's database on the Mac. At most one glance per interval; "stop watching" pauses them. | `argus/sightings.py`, `argus/phone.py` |
| `argusctl key` takes a key hidden from the screen and never prints more than its last four characters. | `argus/__main__.py` |

## 27. Restarts, crashes and the report

| Protection | Where |
|---|---|
| A stuck copy is only ever stopped when it is Argus's own process (checked by its command line), holds Argus's lock, has been running for over 90 seconds and doesn't answer on its private control socket. `argusctl restart --force` stops only processes whose command line is Argus's. | `argus/runtime.py` (`single_instance`, `is_argus`) |
| Stopping finishes within a few seconds even if part of the shutdown hangs, so the microphone and the lock are never held by a copy that is half-closed. | `argus/runtime.py` (`stop_on_signals`) |
| `logs/crash.log` (a native crash) and `logs/threads.log` (a snapshot taken by `argusctl report` when Argus doesn't answer) hold only which code each thread was running: file names, line numbers and function names, no data. Both are owner-only, and the report removes your home folder's name from them. | `argus/runtime.py` (`capture_crashes`, `dump_threads`) |
| A safe start (after two failed starts) only drops the voice models and glances; every protection stays on. | `argus/app.py`, `argus/runtime.py` (`Startup`) |
| The copy of the logs in `~/Downloads/Logs` (on by default, `[logs] folder`) is owner-only (folders 700, files 600) and deleted after 30 days; it holds what the activity log holds (your requests, never keys), so it's as private as your Downloads folder. Only folders named like a date are ever deleted. | `argus/logkeeper.py` |
| `run/startup.json` and `run/council-today.json` are owner-only and hold no personal data (a process id, a phase, counts). | `argus/runtime.py`, `argus/council.py` |

## 28. Argus reading its logs and fixing its own code

| Protection | Where |
|---|---|
| `own_logs` opens only Argus's log files (activity log summaries, `argus.log`, `crash.log`, `calls.log`, the voice logs, `threads.log`), read-only, with keys, emails and your home folder's name removed. Keys, the database and the voiceprint stay out of reach. | `argus/selfcare.py` |
| A fix is made in a separate copy of the code (a git worktree in `~/argus-fixes`) on its own `argus-fix/…` branch. The code Argus runs from isn't touched, and nothing it writes runs for real until you merge it. | `argus/selfcare.py` (`SelfFix`) |
| The safety code can never be changed by a fix: `policy.py`, `approvals.py`, `approve.applescript`, `protected.py`, `guard.py`, `audit.py`, `selfcare.py`, `toolbox.py`, `tests/conftest.py`, and nothing outside `argus/`, `tests/`, `docs/` and `README.md` (no setup scripts, macOS app or dependency lists). Checked on every edit and again on the whole change before it's committed. | `argus/selfcare.py` (`editable`, `check_submit`) |
| Tests of a fix run inside the same kernel sandbox as Argus's commands (your keys and `~/.argus` unreadable, except Argus's own Python), with an environment that holds no keys. | `argus/selfcare.py` (`test_sandbox`), `argus/toolbox.py` |
| Submitting needs your OK (code word or Touch ID) and is refused if the change adds a key, an email address or a phone number, because the branch is pushed to GitHub. It only ever pushes its own branch, never main. | `argus/policy.py`, `argus/selfcare.py` (`leaks`) |
| Merging into main needs Touch ID every time, never a spoken yes, from Argus or from `argusctl fixes merge`. It refuses when `~/argus` has uncommitted changes or isn't on main. | `argus/policy.py` (`touch_only`), `argus/approvals.py`, `argus/__main__.py` |
| Only you can start a fix (your voice, the phone app, the command line); scheduled tasks, loose-end checks and guests can't. | `argus/policy.py` |

## 29. Echo cancellation, names, end of turn and recall

| Protection | Where |
|---|---|
| Echo cancellation runs on the Mac: the argus-ear helper turns on Apple's voice processing and passes the microphone straight to Argus over a pipe. Nothing is recorded or sent anywhere; it closes with the microphone (locked Mac, a call, mic off) and quits when Argus quits. | `macos/argus-ear.swift`, `argus/ear.py` |
| Name correction and end-of-turn detection work on the words already heard, on the Mac. The list of names is built from what you told Argus and what it looked up; names you add are kept in its database. | `argus/names.py`, `argus/turns.py` |
| `recall` searches Argus's own database on the Mac (the last 30 days of conversations, your saved facts, lessons). Conversations with guests are never searched. What it finds goes to Claude like the rest of the conversation. | `argus/recall.py`, `argus/store.py` |
