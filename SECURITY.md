# Argus security

Argus listens all the time and can use your Mac, so it is built to fail safe: when in doubt, it doesn't.
This page lists every protection, where in the code it is enforced, and what it does *not* protect against.

## 1. Your microphone

| Protection | Where |
|---|---|
| Voice detection, speech recognition and clap detection run on the Mac (sherpa-onnx, Silero VAD, Moonshine). No audio leaves the Mac, ever. | `argus/hearing.py`, `argus/clap.py` |
| Audio is never written to disk. The only exception is `argusctl enroll`, which keeps a voiceprint: a list of numbers, not a recording. | `argus/hearing.py` |
| A sentence is sent to Claude only if it contains the name, comes within 8 s of a reply or a double clap, or conversation mode is on. Everything else is dropped and only *counted*; its text is never logged. | `argus/listener.py`, `argus/daemon.py` |
| While Argus is talking or working, only sentences containing its name count, and its own voice is filtered out, so it doesn't answer itself. | `argus/listener.py`, `argus/daemon.py` |
| A double clap only opens listening (or wakes Argus from sleep); it can't approve or run anything. Single claps, applause, knocks and speech are rejected. | `argus/clap.py` |
| The microphone closes while the Mac is locked (macOS's orange dot goes away). | `argus/guard.py` |
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

* The iPhone gateway listens **only on the Mac's Tailscale address** (your private, encrypted network)
  and refuses to start on any other interface. Every request needs a 256-bit token (constant-time
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
  --only-binary :all:`: every file is checked against a pinned SHA-256 and nothing is compiled from source.
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
| Off until you run `scripts/setup_calls.sh`. Each call needs your OK (voice + code word or Touch ID), showing the number and the reason. | `argus/policy.py`, `argus/calls.py` |
| Argus always starts by saying it's your AI assistant and that the call is transcribed, before the model says anything. | `argus/calls.py` |
| Never called: emergency numbers (911, 112, 999, 988…), service numbers (N11), pay-per-call numbers (900, 976), invalid numbers, and anything outside the US and Canada unless you switch that on. At most 6 calls an hour, the same number at most twice in 15 minutes, 6 minutes per call. | `argus/calls.py` |
| The call model is told never to give card, bank, account or ID numbers or addresses and never to agree to costs; anything that passes the card-number checksum is also kept out of what's spoken. | `argus/calls.py` |
| Nothing opens to the internet: the call goes through your own iPhone. The Mac's microphone setting is switched to a virtual cable only for the call and always put back (also on errors, and at once when you say "let me talk"). Argus's own voice is muted during a call so the other side never hears Argus talking to you. | `argus/calls.py` |
| To place and end the call, Argus clicks the green Call and red End buttons that appear on screen; it only clicks a button that wasn't there before it dialled. These two clicks are part of the call you approved, not general screen use. | `argus/calls.py` |
| The call recorder (audiotee, MIT) is built on your Mac from a pinned commit of its source, and needs macOS's "System Audio Recording" permission; no audio is saved, only the transcript in Argus's history. | `scripts/setup_calls.sh` |

## 13. What this does *not* protect against

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

## 14. Kill switches

* Menu bar ◉ → Microphone (off) or Quit Argus; or say "Argus, turn off the microphone".
* `argusctl quit` stops everything (voice, phone gateway, schedules).
* Revoke the Claude key at platform.claude.com.
* Delete `~/.argus/phone_token` and run `argusctl restart` to cut off the iPhone (then
  `argusctl phone-token` makes a new one).

Found a problem? Open a private issue in this repository.
