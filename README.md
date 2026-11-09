# Argus

A speaking, always-listening assistant for your Mac, named after the hundred-eyed watchman of Greek myth.
Say "Argus" anywhere in a sentence and it answers out loud. It can look at your screen when you ask,
open apps, click and type for you, search the web, read the news, and remind you of things. You can
also reach it from your iPhone (Siri or a double-tap on the back of the phone) and from Telegram.

Argus is two parts:

* **The brain** is [Hermes Agent](https://github.com/NousResearch/hermes-agent) running Claude
  (Sonnet 5.5 for conversations, Haiku 5.5 for background chores) on your own Claude API key.
  It does the thinking, the web, the terminal, scheduled jobs and Telegram.
* **The ears and voice** are this repo's macOS app. Listening, speech recognition and the voice all
  run on the Mac. Only sentences addressed to Argus are sent to Claude.

```
microphone ──► voice detection ──► speech-to-text ──► "was that for Argus?" ──► Hermes + Claude ──► tools
 (on Mac)      (Silero VAD)        (Moonshine)          no: dropped, never stored                 │
                                                        yes ▼                                       │
 speakers ◄── on-device voice (Piper) ◄── reply streamed sentence by sentence ◄────────────────────┘
                                           risky action? ──► dialog on screen + Touch ID (never by voice)
```

## What you can say

| You say | Argus |
|---|---|
| "Argus, what's the news today?" / "What's on my calendar, Argus?" | Answers out loud. The name can be anywhere in the sentence. |
| (right after a reply) "And tomorrow?" | Follow-ups within 8 seconds don't need the name. |
| "Argus, let's chat" … "that's all" | Conversation mode: no name needed until you say "that's all" (or 2 minutes of quiet). |
| "Argus, open Safari and find flights to Delhi for Friday" | Uses your apps. Each kind of action asks for your OK on screen first. |
| "Argus, what am I looking at?" | Takes a look at the screen (only when asked) and tells you. |
| "Argus, stop" | Stops talking and cancels the current task, even mid-sentence. |
| "Argus, go to sleep" / "Argus, wake up" | Sleep: nothing is sent anywhere until it hears "Argus, wake up". |
| "Argus, turn off the microphone" | Closes the mic completely; turn it back on from the menu bar. |
| "Argus, new conversation" | Starts a fresh thread (it otherwise remembers the last 30 minutes). |

The menu bar icon (◉) shows what Argus is doing and has switches for the microphone, conversation
mode, sleep, letting Argus see the screen, stop, and the activity log.

## Security in one minute

The full list is in [SECURITY.md](SECURITY.md). The short version:

* **Your voice stays on the Mac.** Audio is processed locally and never recorded. Speech that isn't
  for Argus is thrown away (not stored, not sent). The microphone closes while the Mac is locked.
* **Nothing risky happens by voice alone.** Clicking, typing, deleting, sending: each needs a click on
  an on-screen dialog *and* Touch ID (or your Mac password). A TV or a video call can't approve anything.
  Approvals cover one action or one task, never "always".
* **Off-limits apps and sites.** Password managers, Keychain Access, System Settings and the banking
  sites you list can't be looked at or touched, enforced before every desktop or browser action.
* **No open doors.** The brain's API only listens on 127.0.0.1 with a random key. The iPhone gateway
  only listens on your private Tailscale network with its own token. Telegram only answers your user ID.
* **Keys never in the repo.** They live in `~/.hermes/.env` (owner-only) and are never put on a command
  line. Your Claude Pro subscription is never used (Anthropic doesn't allow it for agents); Argus uses a
  separate pay-per-use API key.

## What you need

* An Apple Silicon Mac on macOS 13 or newer (an 8 GB MacBook Air is fine) with Apple's Command Line
  Tools (`xcode-select --install`).
* A **Claude API key** with some credit, from [platform.claude.com](https://platform.claude.com)
  (separate from Claude Pro). Typical use is a few dollars a month; heavy screen tasks cost more because
  screenshots are large. You can set a monthly spend limit in the console.
* A **Telegram bot** (free, two minutes): message [@BotFather](https://t.me/BotFather), send `/newbot`,
  copy the token. Get your numeric user ID from [@userinfobot](https://t.me/userinfobot).
* For the iPhone: **Tailscale** (free) on the Mac and the iPhone. See [docs/IPHONE.md](docs/IPHONE.md).

## Install

```bash
git clone https://github.com/SandeepSrivatsava/argus.git ~/argus   # private repo: sign in when asked
cd ~/argus
./setup.sh
```

The script asks for the key, the bot token and your Telegram ID (typing is hidden), installs Hermes,
the voice app and its speech models (checksums verified), and starts everything at login. Then:

1. **Click Allow** when macOS asks whether Argus may use the microphone.
2. **Allow CuaDriver** in System Settings → Privacy & Security → Accessibility and Screen Recording
   (setup offers to open these; later: `hermes computer-use permissions grant`). This is what lets
   Argus use apps for you.
3. **Teach it your voice** (recommended): `argusctl enroll`. You read five short sentences; Argus learns
   how you pronounce its name and makes a voiceprint. To make it ignore other voices, set
   `voice_lock = true` in `~/.argus/config.toml` and run `argusctl restart`.
4. **iPhone**: follow [docs/IPHONE.md](docs/IPHONE.md) to add the Siri and Back Tap shortcuts.
5. In Telegram, open your bot and tap **Start**.

Then say: "Argus, what's the news today?"

## Everyday commands

```bash
argusctl status            # what Argus is doing
argusctl log               # recent activity (what was asked, which tools ran, approvals, blocks)
argusctl say "Hello"       # speak something
argusctl mic off|on        # microphone
argusctl screen block|allow
argusctl restart           # after editing ~/.argus/config.toml
argusctl quit              # stop the voice app (Hermes and Telegram keep running)
./scripts/doctor.sh        # full health check
./scripts/update.sh        # update Hermes, Argus and its packages (do this every week or two)
```

Settings live in `~/.argus/config.toml` (voice, follow-up time, blocked sites, voice lock, phone).
The voice is an on-device British male voice (Piper "Alan"); set `engine = "say"` under `[voice]`
to use any macOS voice instead.

## Troubleshooting

* **It doesn't react to its name.** Check the menu bar icon (◌ means the mic is off or the Mac is
  locked). Run `argusctl enroll` so it learns how you say "Argus". If your accent makes "Argus" hard,
  set `name = "Nova"` (any single word) in `~/.argus/config.toml` and restart.
* **It answers the TV.** Turn on voice lock (above), or use sleep mode while watching.
* **"I can't reach my brain right now."** Hermes isn't running: `hermes gateway restart`, then
  `./scripts/doctor.sh`.
* **Desktop actions fail.** Run `hermes computer-use doctor` and grant the permissions it lists.
* **Microphone permission was denied.** System Settings → Privacy & Security → Microphone → turn on Argus.
* Logs: `~/.argus/logs/` (activity log and app log) and `~/.hermes/logs/`.

## How this was tested

* 120 automated tests: name detection (including mishearings like "August"), listening rules
  (follow-ups, conversation mode, sleep, barge-in), the speech queue, the Hermes client, approvals
  (answers are clamped to once/this task/deny), the privacy hook, the control socket, the iPhone gateway
  (tokens, lockout, image checks), settings and logs.
* Real speech: synthesised sentences go through the real voice-detection and speech-recognition models
  into the listening loop.
* End to end (`tests/e2e_hermes.sh`, 42 checks): runs `setup.sh` twice against a real Hermes install,
  then talks to Argus with real speech models and the real Hermes agent (mock model): a question is
  answered by actually running the news script, a dangerous `rm -rf` is held for approval and denied
  (nothing deleted), a banking site is blocked by the privacy hook, speech without the name never
  reaches the model, and the iPhone gateway answers only with the right token.
* Not testable off a Mac, so check these on first run: the microphone prompt, the menu bar, the
  approval dialog with Touch ID, lock detection, and real desktop control through cua-driver.

## Uninstall

```bash
argusctl quit; launchctl bootout gui/$(id -u)/com.argus.voice; launchctl bootout gui/$(id -u)/com.argus.phone
rm -rf ~/.argus ~/Applications/Argus.app ~/Library/LaunchAgents/com.argus.*.plist ~/.local/bin/argusctl
hermes gateway uninstall   # and, to remove Hermes entirely: see its docs
```

## License

MIT. Hermes Agent, sherpa-onnx and the speech models keep their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
