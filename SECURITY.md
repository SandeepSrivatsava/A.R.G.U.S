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
| Optional voice lock: requests from voices that don't match your voiceprint are ignored. | `argus/hearing.py` (`VoicePrint`) |

## 2. What Argus may do: the rules

Every tool call Claude asks for goes through one function, `Policy.check` in `argus/policy.py`, before
anything runs. It answers **allow**, **ask you**, or **block**. Blocked means blocked: there is no
approval that overrides it, and Claude is told not to look for a way around it.

**Blocked, always** (even if you would approve):
* `sudo` and other ways of becoming administrator; turning off macOS protections (`csrutil`, `spctl`,
  `nvram`); changing privacy permissions (`tccutil`).
* Deleting your whole disk, home folder or system folders; erasing or raw-writing disks; fork bombs;
  shutting down or logging out; running scripts piped straight from the internet (`curl … | sh`).
* Reading passwords from the keychain, SSH/GPG keys, cloud and GitHub credentials, browser cookie and
  profile folders, or Argus's own secrets (`~/.argus/secrets.env`, phone token, voiceprint, database).
  This is checked both in shell commands and in the file tools.
* Anything that mentions a website on your never-touch list (in a link, a command, typed text or a search).
* Opening, looking at or touching a never-touch app (password managers, Keychain Access, System
  Settings, plus any you add), including when one is merely visible on screen.
* Screen use while the Mac is locked, while "Allow Argus to see and use the screen" is off, or when the
  app in front can't be identified. Key combinations that log out, lock, force-quit or empty the trash.
  Typing `sudo`, recursive deletes or `curl | sh` into an app.

**Needs your OK** (dialog on the Mac + Touch ID, or buttons in your Telegram chat):
* Shell commands that delete, move or overwrite files; change permissions; stop programs; change macOS
  settings; script other apps (`osascript`); install or remove software; push or reset git; send data to
  the internet; connect to other computers; send email; schedule commands; run inline scripts or nested
  shells; or write outside the workspace folder. (These patterns started from Hermes Agent's dangerous-command
  list, MIT, and were extended for macOS and Argus.)
* Writing files outside the workspace folder (`~/argus-workspace`).
* Every click, drag, scroll, key press and typed text on the screen (looking is allowed; touching asks).
* Creating a scheduled task that will run by itself later (reminders don't ask).

**Allowed without asking:** answering, web search, reading public web pages, news, memory, reminders,
reading files, working inside the workspace folder, opening apps and links, and screenshots when you ask.

## 3. Approvals

| Protection | Where |
|---|---|
| Approvals are never by voice. A spoken or typed "yes" is never an approval, and Claude is told never to ask for one. | `argus/prompts.py`, `argus/agent.py` |
| At the Mac, a dialog shows exactly what will happen; "Allow" then needs Touch ID or your Mac password, so a TV, a call, a recording, or the AI itself clicking the dialog can't approve. | `argus/approvals.py` |
| From Telegram, approvals are buttons in your private chat with your bot; only your Telegram user ID can press them, and each request has its own random code. | `argus/telegram.py` |
| Answers are *once* or *for this task* (the same kind of action, until the task ends), never "always". | `argus/agent.py`, `argus/approvals.py` |
| No answer within 2 minutes = denied. | `argus/approvals.py`, `argus/telegram.py` |
| Scheduled tasks and the iPhone can't approve anything: risky steps are refused and you're told. | `argus/app.py`, `argus/agent.py` |
| After a failed screen action, the rest of that batch is not carried out. | `argus/agent.py` |

## 4. Channels

| Channel | Screen | Approvals |
|---|---|---|
| Voice at the Mac | look and use (each use asks) | dialog + Touch ID |
| Telegram | none, unless you set `telegram_desktop = true` | buttons in your chat |
| iPhone (Siri, Argus Eye app) | screenshots only | none: risky steps are refused |
| Scheduled tasks | none | none: risky steps are refused |

The glasses camera is used only from the Mac, the phone or Telegram when you ask, never by a scheduled
task.

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
* Telegram uses outgoing long-polling (no open port) and only answers your numeric user ID, in a private
  chat. Messages from anyone else are ignored and logged.
* The control socket for `argusctl` is a file in `~/.argus` that only you can open.
* Nothing opens a port on your router or your Wi-Fi.

## 7. Secrets and your Claude account

* Keys are typed with hidden input and written only to `~/.argus/secrets.env` (permissions 600)
  through stdin, never as command-line arguments, which other programs can read. They are never in this
  repo or in `config.toml`. Argus refuses to start if the secrets file is readable by others.
* Commands Argus runs get a minimal environment (path, home, language): your API keys and tokens never
  reach them.
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

## 9. The activity log

`~/.argus/logs/audit.jsonl` (`argusctl log`) records: requests you made (text), tools that ran (name
and a short preview), every approval and its answer, every block, estimated cost per request, microphone
and lock changes, claps, Telegram strangers, phone-gateway requests and bad tokens. It never records audio
or speech that wasn't for Argus. It rotates at 5 MB.

## 10. What this does *not* protect against

* **Anyone in the room can talk to Argus** unless voice lock is on, and voice lock is a convenience
  filter, not strong authentication (a recording of your voice could pass). Approvals still need your
  finger, your password, or your Telegram account.
* **What Argus sees, Claude sees.** When you ask it to look at the screen or through your glasses, the
  picture goes to Anthropic's API (as with any cloud AI). Keep private things out of view, or block the app.
* **A stolen, unlocked Mac** or a compromised user account gives the same access you have.
* **Your Telegram account**: if someone takes it over, they can chat with Argus and press its approval
  buttons. Turn on Telegram's two-step verification, or leave Telegram out during setup.
* **Shell rules are patterns, not a sandbox.** They catch the common dangerous commands; a cleverly
  disguised command might not match. Read approval dialogs before allowing, and keep important files
  backed up (Time Machine).
* **The model can be wrong** or be misled by content it reads.

## 11. Kill switches

* Menu bar ◉ → Microphone (off) or Quit Argus; or say "Argus, turn off the microphone".
* `argusctl quit` stops everything (voice, Telegram, phone gateway, schedules).
* Revoke the Claude key at platform.claude.com; revoke the bot with @BotFather (`/revoke`).
* Delete `~/.argus/phone_token` and run `argusctl restart` to cut off the iPhone (then
  `argusctl phone-token` makes a new one).

Found a problem? Open a private issue in this repository.
