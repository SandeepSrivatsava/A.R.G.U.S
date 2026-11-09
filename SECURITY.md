# Argus security

Argus listens all the time and can use your Mac, so it is built to fail safe. This page lists every
protection, where it is enforced, and what it does *not* protect against.

## 1. Your microphone

| Protection | Where |
|---|---|
| Voice detection and speech recognition run on the Mac (sherpa-onnx, Silero VAD, Moonshine). No audio leaves the Mac, ever. | `argus/hearing.py` |
| Audio is never written to disk (the only exception is `argusctl enroll`, which keeps a voiceprint: a list of numbers, not a recording). | `argus/hearing.py` |
| A sentence is only sent to Claude if it contains the name, comes within 8 s of a reply, or conversation mode is on. Everything else is dropped and only *counted*; its text is never logged. | `argus/listener.py`, `argus/daemon.py` |
| While Argus is talking or working, only sentences containing its name count, so it doesn't answer itself. | `argus/listener.py` |
| The microphone closes while the Mac is locked (macOS's orange dot goes away). | `argus/guard.py` |
| "Argus, go to sleep": nothing is sent until "Argus, wake up". "Argus, turn off the microphone" or the menu closes the mic until you turn it back on. | `argus/wake.py` |
| Optional voice lock: requests from voices that don't match your voiceprint are ignored. | `argus/hearing.py` (`VoicePrint`) |

## 2. Acting on your Mac

| Protection | Where |
|---|---|
| Hermes runs with `approvals.mode: manual`: dangerous commands and every desktop action (click, type, key, scroll, open app) stop and wait for you. | Hermes |
| Approvals are never by voice. Argus shows a dialog on the Mac; "Allow" then needs Touch ID or your Mac password, so a TV, a call, a recording or the AI itself clicking the dialog can't approve. | `argus/approvals.py` |
| Argus only ever answers *once* or *for this task*. It can't grant "always", even if asked to (the client refuses). | `argus/approvals.py`, `argus/hermes.py` |
| No answer within 2 minutes = denied. Hermes itself treats silence as "no". | `argus/approvals.py`, Hermes |
| Privacy rules run before every desktop and browser action (a Hermes `pre_tool_call` hook, set to **fail closed**: if the check can't run, the action is blocked): password managers, Keychain Access and System Settings can't be looked at or touched (also when they're merely in front); websites you list are blocked in URLs, typed text and browser tasks; nothing on screen while the Mac is locked or while "Allow Argus to see and use the screen" is off; if the front app can't be identified, the screen isn't touched. | `argus/hook.py`, `argus/guard.py` |
| Telegram cannot use the screen and apps unless you opted in during setup. The iPhone gateway can't approve anything at all. | `setup.sh`, `argus/phone.py` |
| Hermes's own limits stay on: cua-driver's protected boundary (no OS permission prompts, no killing other apps), hard-blocked key combos (log out, empty trash), blocked dangerous text patterns. | Hermes / cua-driver |
| The model is told to treat web pages, emails, files and screenshots as information, not instructions (prompt-injection defence); approvals are the backstop if it's fooled. | `hermes/SOUL.md.template`, `argus/brain.py` |

## 3. Network exposure

* The Hermes API listens on **127.0.0.1 only** and needs a random 192-bit key; no browser origins (CORS) are allowed. Argus's client also refuses to talk to any non-local address.
* The iPhone gateway listens **only on the Mac's Tailscale address** (your private, encrypted network); it refuses to start on any other interface. Every request needs a 256-bit token (constant-time comparison); 5 bad tokens lock that device out for 15 minutes; bodies are capped at 16 MB; slow clients are cut off after 60 s; only PNG/JPEG screenshots are accepted; at most 2 requests run at once.
* Telegram uses outgoing long-polling (no open port) and only answers your numeric user ID.
* Nothing opens a port on your router.

## 4. Secrets

* Keys are typed with hidden input and written only to `~/.hermes/.env` (permissions 600) through stdin, never as command-line arguments, which other programs can read. They are never in this repo or in `config.yaml`.
* `auth.adopt_external_logins: false`: Hermes never borrows your Claude Code / Claude Pro login.
* `~/.argus` is 700; its settings, activity log, phone token, voiceprint and control socket are 600.

## 5. Supply chain

* Hermes comes from its official installer; setup prints the installer's checksum.
* Argus's Python packages are installed from `requirements-macos.lock` with `--require-hashes --only-binary :all:` (every file checked against a pinned SHA-256; nothing compiled from source).
* The speech and voice models are downloaded from the sherpa-onnx GitHub releases and checked against SHA-256 hashes pinned in `setup.sh`; a mismatch stops the install.
* `Argus.app` is compiled on your Mac from `macos/launcher.c` (about 50 lines) and signed locally.

## 6. The activity log

`~/.argus/logs/audit.jsonl` (`argusctl log`) records: requests you made (text), tools Hermes ran
(name and a short preview), every approval and its answer, every block by the privacy rules, microphone
and lock changes, phone-gateway requests and bad tokens. It never records audio or speech that wasn't
for Argus. It rotates at 5 MB.

## 7. What this does *not* protect against

* **Anyone in the room can talk to Argus** unless voice lock is on, and voice lock is a convenience
  filter, not strong authentication (a recording of your voice could pass). Approvals still need your
  finger or password.
* **What Argus sees, Claude sees.** When you ask it to look at the screen, the screenshot goes to
  Anthropic's API (as with any cloud AI). Keep private things out of view, or block the app.
* **A stolen, unlocked Mac** or a compromised user account gives the same access you have.
* **Your Telegram account**: if someone takes it over, they can chat with Argus (but not approve desktop
  actions unless you allowed Telegram to use the screen). Turn on Telegram's two-step verification.
* **The model can be wrong** or be misled by content it reads. Read approval dialogs before allowing.

## 8. Kill switches

* Menu bar ◉ → Microphone (off) or Quit Argus; or say "Argus, turn off the microphone".
* `argusctl quit` stops the voice app; `hermes gateway stop` stops the brain and Telegram.
* Revoke the Claude key at platform.claude.com; revoke the bot with @BotFather (`/revoke`).
* Delete `~/.argus/phone_token` and run `argusctl restart` to cut off the iPhone shortcuts (re-run
  `argusctl phone-token` to make a new one).

Found a problem? Open a private issue in this repository.
