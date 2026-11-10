# Argus

A speaking, always-listening assistant for your Mac, iPhone and Meta glasses, named after Argus
Panoptes, the hundred-eyed watchman of Greek myth who never slept. Say "Argus" anywhere in a sentence,
or clap twice, and it answers out loud. It can look at your screen when you ask, use your apps, run
commands, search the web, read the news, remember what you tell it, remind you of things, and see
through your Meta glasses.

Argus is its own code, written from scratch for this project. The brain talks straight to the Claude
API (no agent framework in between), and listening, speech recognition and the voice all run on your Mac.
Useful ideas and small pieces of MIT-licensed code from open-source assistants were studied, ported and
improved, with credit in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Ideas for what A.R.G.U.S.
could stand for (and other names): [docs/NAMES.md](docs/NAMES.md).

```
microphone ─► voice detection ─► speech-to-text ─► "for Argus?" ─► brain ◄──► Claude API (Sonnet 5.5)
double clap ────────────────────────────────────────►    │ no: dropped,  │
                                                          never stored    ▼ every tool call
                                                               policy: allow · ask you (dialog + Touch ID) · block
                                                                          ▼
speakers ◄─ on-device voice ◄─ reply, sentence by sentence    tools: shell, files, web, news, memory,
                                                               reminders, apps, screen, glasses camera
Telegram, Siri shortcuts and the Argus Eye iPhone app (Meta glasses) talk to the same brain.
```

## What's inside

| Part | What it does | Code |
|---|---|---|
| Brain | Streams replies from Claude, runs tools in a loop, caches the prompt to keep costs down, keeps conversations and memories in a private SQLite database, summarises long chats, tracks spend | `argus/llm.py`, `agent.py`, `prompts.py`, `store.py` |
| Hands | Shell commands, files, web pages, news, reminders, memory, opening apps and links, and the screen (Claude's computer-use tools, carried out with macOS Quartz events) | `argus/toolbox.py`, `computer.py` |
| Rules | Checks every tool call first: allowed, needs your OK, or blocked | `argus/policy.py`, `guard.py`, `approvals.py` |
| Ears | Microphone, voice detection, speech recognition, name spotting, double-clap detection, voice lock | `argus/hearing.py`, `wake.py`, `listener.py`, `clap.py` |
| Voice | On-device Piper voice (or any macOS voice); you can interrupt it | `argus/voice.py`, `brain.py` |
| Reach | Telegram bot, iPhone gateway, Meta glasses bridge, scheduled jobs | `argus/telegram.py`, `phone.py`, `glasses.py`, `scheduler.py`, `cron.py` |
| Mac app | Menu bar, Touch ID, lock detection, starts at login | `argus/app.py`, `daemon.py`, `macos/` |
| iPhone app | Argus Eye: Meta glasses camera, push-to-talk | `ios/` |

## What you can say

| You say (or do) | Argus |
|---|---|
| "Argus, what's the news today?" / "What's the weather, Argus?" | Answers out loud. The name can be anywhere in the sentence. |
| *clap clap*, then "what time is my flight?" | Two sharp claps (less than a second apart) open listening without the name. Single claps, applause and speech are ignored. |
| (right after a reply) "And tomorrow?" | Follow-ups within 8 seconds don't need the name. |
| "Argus, let's chat" … "that's all" | Conversation mode: no name needed until you say "that's all" (or 2 minutes of quiet). |
| "Argus, open Safari and find flights to Delhi for Friday" | Uses your apps. Each kind of action asks for your OK on screen first. |
| "Argus, what am I looking at?" | Looks through your Meta glasses if they're connected (Argus Eye app), otherwise at the screen. Only when asked. |
| "Argus, remember that my sister's birthday is 12 March" | Remembers across conversations. "What do you remember?" / "Forget number 3." |
| "Argus, remind me at 6 to call Mom" / "every weekday at 8, check the weather" | Reminders and scheduled tasks; spoken at the Mac and sent to Telegram. |
| "Argus, stop" | Stops talking and cancels the current task, even mid-sentence. |
| "Argus, go to sleep" / "Argus, wake up" (or a double clap) | Sleep: nothing is sent anywhere until you wake it. |
| "Argus, turn off the microphone" | Closes the microphone; turn it back on from the menu bar. |
| "Argus, new conversation" | Starts a fresh thread (it otherwise keeps the last 30 minutes in mind). |

The menu bar icon (◉) shows what Argus is doing (and "glasses on" when your glasses are connected),
with switches for the microphone, conversation mode, sleep, screen access, stop, and the activity log.
In Telegram, just message your bot; `/new` starts a fresh conversation and `/stop` cancels.

## Meta glasses

* **Now, no extra app:** say "Hey Meta, take a photo", then "Hey Siri, Argus look". The photo comes
  from the Meta AI app's auto-import and Argus describes it. Setup: [docs/IPHONE.md](docs/IPHONE.md).
* **Next, the Argus Eye app** ([ios/](ios/README.md)): uses Meta's official Wearables Device Access
  Toolkit. Argus knows when your glasses are connected, can take a photo through them when you ask
  "what am I looking at?", and you can talk to Argus through the glasses. The app is written but hasn't
  been compiled yet (that needs Xcode on your Mac and a free Meta developer account).

## Security in one minute

The full list is in [SECURITY.md](SECURITY.md). The short version:

* **Your voice stays on the Mac.** Audio is processed locally and never recorded. Speech that isn't for
  Argus is thrown away (not stored, not sent). The microphone closes while the Mac is locked.
* **Nothing risky happens by voice.** Deleting, installing, sending, clicking and typing need a click on
  an on-screen dialog *and* Touch ID, or the buttons in your own Telegram chat. A TV or a video call
  can't approve anything. Approvals cover one action or one task, never "always".
* **Hard limits.** `sudo`, wiping disks, reading your keys or Argus's own secrets, password managers,
  Keychain Access, System Settings and the banking sites you list are blocked outright, before anything runs.
  On the Mac, every command Argus runs is also boxed in by a macOS sandbox that can't touch your keys,
  and Argus's own keys are removed from anything a tool shows Claude.
* **No open doors.** The iPhone gateway only listens on your private Tailscale network and needs a
  256-bit token. Telegram only answers your user ID. Nothing listens on your Wi-Fi or the internet.
* **Keys never in the repo.** They live in `~/.argus/secrets.env` (owner-only), are typed with hidden
  input, never appear on a command line, and are removed from the environment of commands Argus runs.
  Your Claude Pro subscription is never used; Argus uses its own pay-per-use API key.

## What you need

* An Apple Silicon Mac on macOS 13 or newer (an 8 GB MacBook Air is fine) with Apple's Command Line
  Tools (`xcode-select --install`).
* A **Claude API key** with some credit, from [platform.claude.com](https://platform.claude.com)
  (separate from Claude Pro). Rough guide: a spoken question costs 1 to 3 cents, a web search adds a
  cent or two, and a ten-step task on the screen costs 10 to 30 cents. `argusctl usage` shows the running
  estimate, and you can set a monthly limit in the Claude console.
* Optional: a **Telegram bot** (free, two minutes): message [@BotFather](https://t.me/BotFather), send
  `/newbot`, copy the token. Get your numeric user ID from [@userinfobot](https://t.me/userinfobot).
* Optional, for the iPhone and glasses: **Tailscale** (free) on the Mac and the iPhone.

## Install

```bash
git clone https://github.com/SandeepSrivatsava/A.R.G.U.S.git ~/argus   # private repo: sign in when asked
cd ~/argus
./setup.sh
```

The script checks the Mac, asks for the key, the bot token and your Telegram ID (typing is hidden),
installs Python and Argus's packages (every file checked against a pinned hash), downloads the speech
models (checksums verified), builds Argus.app, and starts it at login. Then:

1. **Click Allow** when macOS asks whether Argus may use the microphone.
2. **Switch on Argus** in System Settings → Privacy & Security → **Screen Recording** and
   **Accessibility**, then run `argusctl restart`. This is what lets Argus see and use apps for you.
3. **Teach it your voice** (recommended): `argusctl enroll`. You read five short sentences; Argus learns
   how you say its name and makes a voiceprint. To make it ignore other voices, set `voice_lock = true`
   in `~/.argus/config.toml` and run `argusctl restart`.
4. **iPhone and glasses**: follow [docs/IPHONE.md](docs/IPHONE.md).
5. In Telegram, open your bot and tap **Start**.

Then say: "Argus, what's the news today?"

## Everyday commands

```bash
argusctl status            # what Argus is doing
argusctl log               # recent activity (requests, tools, approvals, blocks)
argusctl usage             # estimated Claude spend today and this month
argusctl ask "any news?"   # type a request instead of saying it (Argus answers out loud)
argusctl say "Hello"       # speak something
argusctl telegram "text"   # send yourself a message
argusctl mic off|on        # microphone
argusctl screen block|allow
argusctl doctor            # check models, keys, Claude, Telegram, the app and its permissions
argusctl restart           # after editing ~/.argus/config.toml
argusctl quit              # stop Argus
./scripts/doctor.sh        # doctor, plus file privacy, login item and stay-awake checks
./scripts/update.sh        # get the latest Argus and its packages
```

Settings live in `~/.argus/config.toml`: your name and city, the model, web search, the per-request
token budget, follow-up time, clap mode (`"double"`, `"single"` or `"off"`), voice lock, the voice,
blocked apps and sites, whether Telegram may use the screen (off by default), the phone gateway, the
glasses, and the morning headlines (7:00, free) and optional AI briefing. The voice is an on-device
British male voice (Piper "Alan"); set `engine = "say"` under `[voice]` to use a macOS voice instead.

## Troubleshooting

* **It doesn't react to its name.** Check the menu bar icon (◌ means the microphone is off or the Mac is
  locked). Run `argusctl enroll` so it learns how you say "Argus". Or clap twice and talk.
* **It answers the TV.** Turn on voice lock (above), or put it to sleep while watching.
* **Claps don't work.** Clap sharply twice, less than a second apart, in a quiet moment. Soft claps or
  claps far from the Mac's microphone may not register. `clap = "off"` under `[listening]` turns it off.
* **"I can't reach Claude right now."** Run `argusctl doctor`: it checks the internet, the key and
  your credit.
* **Screen actions fail.** `argusctl doctor` says whether Argus has Screen Recording and Accessibility.
  Switch Argus on in both (System Settings → Privacy & Security), then `argusctl restart`.
* **Microphone permission was denied.** System Settings → Privacy & Security → Microphone → turn on Argus.
* Logs: `~/.argus/logs/` (activity log `audit.jsonl` and `argus.log`).

## How this was tested

* **439 automated tests**: the Claude client (streaming, exact rebuilding of tool calls, retries, bad
  keys), the tool loop, approvals (once / this task / deny), the policy rules, screen control (scaling,
  stopping after a failed action, locked Mac, password apps), web-fetch safety, cost estimates, memory,
  reminders and cron schedules, Telegram (strangers ignored, approval buttons), the iPhone gateway
  (tokens, lockout, image checks), the glasses bridge, clap detection (double clap yes; single claps,
  triple claps, applause and speech no), name detection including mishearings like "August", the
  listening rules, settings, secrets and logs.
* **Failure cases on purpose** (about 200 of the tests): the Claude API timing out, cutting a reply
  off, sending junk, refusing the key or rate-limiting; Telegram refusing messages or failing downloads;
  crashing tools, approval dialogs and schedules; malformed requests to the iPhone gateway; broken
  settings, secrets and voiceprint files; DNS tricks and links into protected folders; commands that hang,
  can't start or flood the output; and sounds that look like claps (a "p" before a voice, a door slam,
  claps right after music). Every request Argus sends to the stand-in Claude is checked against the rules
  the real API enforces.
* **Coverage**: the tests reach 96% of Argus's code (lines and branches), 97% with the end-to-end run;
  before this round of failure tests it was 70%. What's left is mostly Mac-only (the menu bar, a few
  fallbacks for older macOS). Run `scripts/coverage.sh` (add `--e2e` for the full run) to see it yourself.
* **Real speech**: sentences spoken by the real voice go through the real voice-detection and
  speech-recognition models into the listening loop.
* **End to end** (`tests/e2e.sh`, 34 checks): runs `setup.sh` twice, then starts the whole app with the
  real speech models and a stand-in Claude server: a spoken question is answered by actually running the
  news tool, a dangerous `rm -rf` is held for approval and denied (nothing deleted), a banking site is
  blocked, a double clap opens listening, Telegram answers only its owner, a reminder is spoken and sent
  to the phone, and the iPhone gateway answers only with the right token.
* **Not testable off a Mac, so check on first run:** the microphone prompt, the menu bar, the approval
  dialog with Touch ID, lock detection, real screen control, the live Claude API with the computer
  tools, live Telegram, and the iPhone app (not yet compiled).

## Uninstall

```bash
argusctl quit; launchctl bootout gui/$(id -u)/com.argus.voice
./scripts/stay-awake.sh off      # if you turned it on during setup
rm -rf ~/.argus ~/Applications/Argus.app ~/Library/LaunchAgents/com.argus.voice.plist ~/.local/bin/argusctl
```

## License

MIT. The speech models and libraries keep their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
