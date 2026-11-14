# Argus

A speaking, always-listening assistant for your Mac, your phone (iPhone or Android) and Meta glasses,
named after Argus Panoptes, the hundred-eyed watchman of Greek myth who never slept. Say "Argus" anywhere
in a sentence, or clap twice, and it answers out loud. It can look at your screen when you ask, use your
apps, run commands, search the web, read the news, remember what you tell it, remind you of things, find
the fastest route with live traffic, see through your Meta glasses, phone a restaurant from your own
number, and learn from your corrections and its own mistakes. On Android it also lives on the phone
itself (your texts, WhatsApp and notifications), and an optional always-on server keeps it answering when
the Mac is off.

Argus is its own code, written from scratch for this project. The brain talks straight to the Claude
API (no agent framework in between), and listening, speech recognition and the voice all run on your Mac.
Useful ideas and small pieces of MIT-licensed code from open-source assistants were studied, ported and
improved, with credit in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Ideas for what A.R.G.U.S.
could stand for (and other names): [docs/NAMES.md](docs/NAMES.md).

```
microphone ─► voice detection ─► speech-to-text ─► "for Argus?" ─► brain ◄──► Claude API (Haiku, Sonnet or Opus 5.5 by the request)
double clap ────────────────────────────────────────►    │ no: dropped,  │
                                                          never stored    ▼ every tool call
                                                policy: allow · ask you (Touch ID, or your voice + a code word) · block
                                                                          ▼
speakers ◄─ on-device voice ◄─ reply, sentence by sentence    tools: shell, files, web, news, memory,
                                                               reminders, apps, screen, glasses, phone calls
Siri shortcuts and the Argus Eye iPhone app (Meta glasses) talk to the same brain. Everything is by voice.
Android: the same brain runs on the phone (Termux). Mac off: an always-on server answers the phone.
```

## What's inside

| Part | What it does | Code |
|---|---|---|
| Brain | Streams replies from Claude, runs tools in a loop, caches the prompt to keep costs down, keeps conversations, memories and lessons in a private SQLite database, summarises long chats, tracks spend | `argus/llm.py`, `agent.py`, `prompts.py`, `store.py`, `lessons.py` |
| Hands | Shell commands, files, web pages, news, reminders, memory, opening apps and links, and the screen (Claude's computer-use tools, carried out with macOS Quartz events) | `argus/toolbox.py`, `computer.py` |
| Rules | Checks every tool call first: allowed, needs your OK, or blocked | `argus/policy.py`, `guard.py`, `approvals.py` |
| Ears | Microphone, voice detection, speech recognition, name spotting, double-clap detection, voice lock | `argus/hearing.py`, `wake.py`, `listener.py`, `clap.py` |
| Voice | On-device Piper voice (or any macOS voice); you can interrupt it | `argus/voice.py`, `brain.py` |
| Reach | Phone gateway, Meta glasses bridge, reminders and scheduled jobs, phone calls from your number, push notifications | `argus/phone.py`, `glasses.py`, `scheduler.py`, `cron.py`, `calls.py`, `push.py` |
| Maps | The best route from several maps services (Apple Maps, Google Routes, TomTom), incidents and road news on each, navigation links for Apple Maps, Google Maps and Waze | `argus/maps.py`, `argus/traffic.py`, `macos/argus-maps.swift` |
| Android | Argus on the phone: texts, WhatsApp backup and notification search, fingerprint approvals, drive assist | `argus/android.py`, `android_app.py` |
| Server | Always-on Argus on a Linux server, for when the Mac is off | `argus/server.py`, `scripts/setup_server.sh` |
| Mac app | Menu bar, Touch ID, lock detection, starts at login | `argus/app.py`, `daemon.py`, `macos/` |
| iPhone app | Argus Eye: Meta glasses camera, push-to-talk | `ios/` |

## What you can say

| You say (or do) | Argus |
|---|---|
| "Argus, what's the news today?" / "What's the weather, Argus?" | Answers out loud. The name can be anywhere in the sentence. The weather is for where your phone is. |
| "Argus, I'm going to Tahoe Saturday to ski" | The forecast there for when you arrive, whether it suits skiing, and an offer to check again the evening before and an hour before you leave. |
| *clap clap*, then "what time is my flight?" | Two sharp claps (less than a second apart) open listening without the name. Single claps, applause and speech are ignored. |
| (right after a reply) "And tomorrow?" | Follow-ups within 8 seconds don't need the name. |
| "Argus, let's chat" … "that's all" | Conversation mode: no name needed until you say "that's all" (or 2 minutes of quiet). |
| "Argus, open Safari and find flights to Delhi for Friday" | Uses your apps. Each kind of action asks for your OK first: say "yes" and the code word Argus reads out, or use Touch ID. Paying always needs Touch ID. |
| "Argus, call Luigi's and book a table for two at seven tomorrow" | Phones them from your own number (through your iPhone), says it's your AI assistant, books it and tells you how it went. "Argus, hang up" / "Argus, let me talk" at any point. |
| "No, when I say the report I mean the PDF in Work" | Saves that as a lesson and follows it from then on. It also writes its own lessons when something fails and it finds the fix. "What have you learned?" |
| Someone else: "Argus, what's the capital of France?" | Once it knows your voice, it asks you first: "Someone else is asking me something. Should I answer them?" "Argus, talk to everyone" / "Argus, only me". |
| "Argus, find the address Ravi sent me on WhatsApp" | Searches WhatsApp on the Mac, or your iPhone's apps through iPhone Mirroring (slow, but it can see everything there). On Android, Argus searches your texts, WhatsApp's full history and your notifications on the phone itself. |
| "Hey Siri, Argus" → "fastest way to the airport?" | Live traffic for up to three routes from where your phone is, which one is faster and by how much, and your maps app opens with it. |
| "Argus, what am I looking at?" | Looks through your Meta glasses if they're connected (Argus Eye app), otherwise at the screen. Only when asked. |
| "Argus, I'm flying to Chicago on Friday" | If you let it read your mail: finds the booking, tells you the airline, flight and time, and asks whether you want the details (confirmation code, terminal, seat). |
| "Argus, what's on tomorrow?" / "Find my lease in Drive" | Your Google Calendar and Drive, read-only, once connected (docs/GOOGLE.md). |
| "Has that flight been late or cancelled in the last two weeks?" | Day by day: on time, late by how much, cancelled or diverted, and a summary (FlightAware key; otherwise it searches the web). |
| "Argus, write a Python script that renames my photos by date" | Hard work goes to the heavy model ("Let me think that through properly"); everyday questions go to the quick one. |
| "Argus, remember that my sister's birthday is 12 March" | Remembers across conversations. "What do you remember?" / "Forget number 3." |
| "Argus, remind me at 6 to call Mom" / "every weekday at 8, check the weather" | Reminders and scheduled tasks, spoken at the Mac. If you're away (Mac locked or microphone off), Argus keeps them and tells you when you're back. |
| "Argus, stop" | Stops talking and cancels the current task, even mid-sentence. |
| "Argus, go to sleep" / "Argus, wake up" (or a double clap) | Sleep: nothing is sent anywhere until you wake it. |
| "Argus, turn off the microphone" | Closes the microphone; turn it back on from the menu bar. |
| "Argus, new conversation" | Starts a fresh thread (it otherwise keeps the last 30 minutes in mind). |

The menu bar icon (◉) shows what Argus is doing (and "glasses on" when your glasses are connected),
with switches for the microphone, conversation mode, sleep, screen access, stop, and the activity log.

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
* **Risky actions need your OK, your way.** Deleting, installing, sending, calling, clicking and typing
  ask first. Answer with Touch ID on the screen, or out loud: only your enrolled voice saying "yes" plus a
  random code word Argus reads out counts, so a TV, a recording or someone else in the room can't approve.
  Anything that pays or buys (a Buy or Pay button, a checkout page, a card number) needs Touch ID.
  Approvals cover one action or one task, never "always".
* **It answers only you.** Once it knows your voice, Argus asks you before answering anyone else, and a
  guest gets general answers only: nothing about you, no access to the Mac.
* **Lessons can't change the rules.** What Argus learns is notes for the model, filtered so a web page or
  file can't plant one; the safety rules are code.
* **Hard limits.** `sudo`, wiping disks, reading your keys or Argus's own secrets, password managers,
  Keychain Access, System Settings and the banking sites you list are blocked outright, before anything runs.
  On the Mac, every command Argus runs is also boxed in by a macOS sandbox that can't touch your keys,
  and Argus's own keys are removed from anything a tool shows Claude.
* **No open doors.** The phone gateway (on the Mac or the server) only listens on your private Tailscale
  network and needs a 256-bit token. Nothing listens on your Wi-Fi or the internet.
* **Your location and messages.** The phone's location is used only for the trip you asked about: it goes
  to the maps service, not to Claude or the logs. On Android your messages stay on the phone; only what a
  search finds goes to Claude, marked as other people's words (information, never instructions), and
  sending a text needs your fingerprint.
* **The always-on server is boxed in.** It runs as its own user that can write only its own folder, runs no
  shell commands, and refuses anything that would need your OK.
* **Keys never in the repo.** They live in `~/.argus/secrets.env` (owner-only), are typed with hidden
  input, never appear on a command line, and are removed from the environment of commands Argus runs.
  Your Claude Pro subscription is never used; Argus uses its own pay-per-use API key.

## What only Argus does

* **It gets it done, on the phone.** "Find a good Indian place for 7 tonight": Argus checks Google's ratings
  and reviews, tells you its pick, and on your OK calls the restaurant from your own number, books the table
  and tells you how it went. (Calls need `scripts/setup_calls.sh` once; when you say "call them" it dials
  without asking again, as long as voice lock is on.
  While it's on the call it can't talk to you.)
* **It follows up by itself.** When a plan leaves something open ("New York: no hotel yet"), Argus notes the
  loose end and looks into it every few days: prices, dates getting close, a reply that hasn't come. It
  speaks up only when there's something worth hearing ("Hotel prices for your dates went up; want me to look
  now?"). "What's still open?" lists them; "forget the hotel one" closes one. Checks can only look things up,
  and stay quiet from 9 at night to 8 in the morning.
* **It sees what you see, and remembers where things are.** Through your Meta glasses (the Argus Eye iPhone
  app): "what's this?", "is this place any good?" (it reads the name and checks Google's reviews for the one in
  front of you), and "where did I put my keys?". For the last one Argus must have seen them: it notes
  belongings each time it looks, and with **glances** switched on (`glances_minutes = 3` under `[glasses]`) the
  app sends a photo every few minutes while the glasses are on. Only a few words are kept ("keys: hallway
  shelf"), for two weeks, on your Mac; the photo is never stored. "Argus, stop watching" pauses it.

## Keys and extra brains, without running setup

```bash
argusctl key                        # which keys are saved (never shows them)
argusctl key FLIGHTAWARE_API_KEY    # add or replace one; you paste it hidden
argusctl brains                     # the other AI models Argus asks for a second opinion
argusctl brains add chatgpt         # add one (you paste its key hidden; Argus checks it answers)
argusctl brains test                # does each one answer right now?
argusctl brains remove grok         # take one out
```

Claude always gives the answer; for decisions, advice and disputed questions it asks these at the same time
and weighs what they say. Restart after adding one (`argusctl restart`).

| Brain | Add with | Cost |
|---|---|---|
| Gemini (Google) | `argusctl brains add gemini` | free tier |
| ChatGPT (OpenAI's GPT-6 Luna) | `argusctl brains add chatgpt` | pay-per-use: about $0.0005 a question |
| Grok (xAI's Grok 4.7) | `argusctl brains add grok` | pay-per-use: under a cent a question |
| Groq (OpenAI's open model, gpt-oss-120b) | `argusctl brains add groq` | free: 1,000 questions a day |
| Mistral (Medium) | `argusctl brains add mistral` | free (rate-limited) |
| OpenRouter (free models) | `argusctl brains add openrouter` | free: 50 questions a day |

* **ChatGPT and Grok have no free API.** A ChatGPT Plus or SuperGrok subscription doesn't include API use;
  both bill per question like Claude, from a little prepaid credit. The free way to get an OpenAI brain is
  Groq, which runs OpenAI's open model. (GitHub's free GPT and Grok access closed in July 2026.)
* Paid brains are capped at 40 questions a day each (`paid_per_day` under `[council]`), and Argus doesn't
  wait for a slow one: after 12 seconds (`wait_seconds`) it goes with the answers it has.
* Only the question is sent, never your memory, mail or the conversation, and email addresses, phone numbers,
  long numbers and your name are taken out first. Free tiers may keep questions to improve their models
  (Mistral: switch that off in its admin console; xAI: leave its data-sharing program off).

## Talking with Argus

* **Talk over it, like with a person.** Run `argusctl enroll` once (five sentences, a minute): from then on,
  while Argus is speaking, just start talking and it stops at once and listens, and what you say needs no
  "Argus". It tells your voice from its own coming out of the speakers by your voiceprint, and has to be sure
  (two checks in a row, about a second). Until you enroll, say its name: "Argus, stop" or "Argus, what about
  tomorrow?". (Comparing words alone made it stop itself mid-sentence on a laptop's speakers and then answer
  its own words, so that's only used with `barge_in = "words"` under `[listening]`, for headphones.)
  `barge_in = "name"` keeps it to the name even after enrolling. If it still talks on when you speak, lower
  `talk_over_threshold` under `[listening]` (0.38; Argus's own voice scores far below that).
* **A back-and-forth, not a lecture.** Tell it "I'm going to New York next month" and it reacts, asks what
  kind of trip it is, then offers two or three ideas and narrows down from your answer. It remembers what
  you like (budget, food, how you travel), so next time it asks less.
* **A natural American voice**, made on the Mac by Kokoro (an open, Apache-2.0 voice model), at 1.2× speed.
  Change it in one step: `argusctl voice af_sky` (it saves the setting, restarts Argus and says hello in the
  new voice); `argusctl voice` lists them (men: am_michael, am_adam; women: af_bella, af_sarah, af_nicole,
  af_sky), `argusctl voice af_sky --try` plays it first, `argusctl voice --speed 1.1` changes the pace and
  `argusctl voices` plays them all. While one sentence plays, the next is being made. If your Mac can't make
  the natural voice fast enough (gaps between sentences), Argus notices after a few sentences and switches by
  itself to a quick American voice, unless you picked the voice with `argusctl voice <name>`: then it keeps
  that one (`keep_natural`); `argusctl voice --natural` goes back to it too.
* **It can write and run code.** For things that need working out (comparing many options, numbers, data, a
  schedule), Claude writes Python and runs it in Anthropic's sandbox, which has no internet and can't reach your
  Mac (`code_execution = false` under `[brain]` turns it off). Searching is still done with Google, Gemini, the
  web and the other AI models together: "find me a good movie" asks what you're in the mood for, looks in all of
  them at once and gives two or three picks.
* **Google next to Claude.** Restaurant reviews and ratings from Google Maps, Google's own search results
  (through Gemini, free) and second opinions from other AI models, looked up at the same time and combined
  into one answer: [docs/GOOGLE_SEARCH.md](docs/GOOGLE_SEARCH.md).

Why Python and not Go: `python scripts/bench_turn.py` measures Argus's own work for a whole spoken turn at
about 5 ms; a real turn takes 2 to 5 seconds, almost all of it the network, Claude and the voice models
(which are C++ either way). Go would save under 1% of the wait.

## The right model for each request

Argus picks the Claude model by what you ask, on the Mac, before anything is sent (so it costs no time):

| Kind of request | Model | Price next to the main model |
|---|---|---|
| Everyday: the time, weather, traffic, news, timers, small talk | Haiku 5.5 (quickest) | about a twentieth |
| Using the Mac, mail and flights, feelings and advice, longer requests, photos | Sonnet 5.5 (main) | 1× |
| Coding, analysis, reports, long writing | Opus 5.5 (heavy) | about 2× |

* If the quick model sees that a request is beyond it, it hands the request up by itself; you hear one answer.
* A short reply ("yes", "and tomorrow?") stays with the model that asked.
* The activity log and `argusctl report` show which model answered each request. Change the models under `[brain]`
  (`light_model`, `model`, `heavy_model`; for example `heavy_model = "claude-fable-5-2"` for the strongest,
  at about 4×), or set `routing = "off"` to always use the main model.

## Your Google account (read-only)

`argusctl google connect` links your Gmail, Google Drive and Google Calendar, read-only: Google itself refuses
anything that would send, delete or change. Ten minutes in Google Cloud the first time:
[docs/GOOGLE.md](docs/GOOGLE.md). Then you can ask "what's on tomorrow?", "find my lease in Drive", "did the
landlord reply?", or "I'm flying Friday" (it finds the booking in Gmail). `argusctl google status` checks it;
`argusctl google disconnect` revokes it.

## Your mail (read-only) and your flights

* **Mail through the Mail app** (when Google isn't connected) is off until you say yes in setup (`[mail] enabled`). Argus asks the Mac's **Mail app**, which
  already has your accounts, so no mail password or key is stored. It can only search subjects and senders
  and read one message; there is no way for it to send, delete, move or mark mail. Security-code and
  password mails, and mail from your never-touch sites (your bank), are left out before Claude sees
  anything. The first time, macOS asks "Argus wants to control Mail": click Allow. If your Gmail isn't in
  the Mail app yet: System Settings → Internet Accounts → Google → switch on Mail, then open Mail once.
* **Flight history** uses FlightAware's AeroAPI with your own key (setup asks; flightaware.com/aeroapi,
  Personal plan). One question costs about one to three cents and the plan includes free use each month.
  It covers the last 14 days and the next two. Without a key, Argus searches the web instead.

## Seeing what went wrong

```bash
argusctl status            # what Argus is doing right now, in plain words
argusctl errors            # recent problems in plain words
argusctl errors --watch    # leave this open in a Terminal window: new problems appear as they happen
argusctl report            # writes argus-report.txt to your Desktop: problems, timings, settings
argusctl logs              # every log, in ~/Downloads/Logs (a folder per day, deleted after 30 days)
```

* **All the logs, in one folder.** Argus copies everything new in each of its logs (activity, app log, what it
  printed when starting, crashes) to `~/Downloads/Logs/<date>/` every 10 minutes and when it stops, and deletes
  day folders older than 30 days by itself. Change it under `[logs]` (`folder`, `keep_days`; `folder = ""`
  turns it off). The first time, macOS may ask whether Argus may use the Downloads folder: allow it. The
  activity log includes what you asked, so keep that folder to yourself; send `argusctl report` instead.

* `argusctl restart` waits until Argus really answers again and says so ("Argus is back and listening"), or
  says why it didn't: where the start stopped, how it ended and what it printed. `argusctl restart --force`
  stops every copy of Argus first, for when one is stuck.
* A restart can't leave Argus off any more: a new copy waits for the old one to finish closing, a copy that
  holds on without answering for minutes is stopped, and stopping always finishes within a few seconds.
* If two starts in a row fail, the next one starts safely (the Mac's own voice, no extras) so Argus still
  answers, and `argusctl status` and the report say so.
* The report starts with **Right now**: whether Argus is answering, and if not, why (and where it is stuck,
  from a snapshot of what each part is doing). It also has what Argus printed when starting, any crash, and
  when it started and stopped.
* If the microphone gives only silence (macOS took the permission away), the menu bar and `argusctl status`
  say so: System Settings → Privacy & Security → Microphone → Argus.

* The report never contains what you said or what Argus answered, and keys, tokens, email addresses and
  your home folder's name are removed. Read it, then send it when you ask for help.
* When a request fails, Argus says why in a sentence, a notification shows the reason, and the menu bar
  has **Show recent problems**.

## Fast answers, and second tries

* **Spoken answers skip the long think.** For anything you say out loud, Argus asks Claude for a quick
  answer (`voice_effort = "low"` under `[brain]`) and lets it think only between steps, such as after a
  weather lookup, not before the first word. Typed and harder jobs use `effort = "medium"`. Raise either
  (`"medium"`, `"high"`) if you'd rather wait for more careful answers.
* **Lookups run at the same time.** When Claude needs several things that don't depend on each other
  (your calendar and your mail, the weather in two places), they run together instead of one by one.
  Gmail search fetches its results in parallel too. Anything that needs your OK still runs alone.
* **Weather is quick the second time.** The forecast and the warnings are fetched at the same time, and a
  forecast is reused for 10 minutes, so "what about tomorrow?" doesn't fetch it again.
* **"One moment."** If a tool takes more than a second and a half, Argus says so, so silence never means
  it didn't hear you.
* **See where the time goes:** `argusctl timing` shows, for your last few spoken questions, how long it took
  from the end of your sentence to Argus's first word, split into the pause, speech to text, each Claude
  request and each tool.
* **It starts talking while Claude is still writing.** Replies are spoken sentence by sentence as they
  stream in, and a long first sentence starts at its first comma.
* **No waiting for a connection.** Argus keeps its connection to Claude open between requests and opens
  one while you're still talking (nothing is sent until you've finished), so a question doesn't pay for a
  fresh secure handshake.
* **A pause of 0.6 seconds ends what you're saying.** Set `end_of_speech_seconds` lower for snappier
  answers, or higher if you pause mid-sentence.
* **Timeouts try again by themselves.** If Claude doesn't answer in time, the connection drops or the API
  is busy, Argus retries after half a second, then 1 and 2 seconds, without you doing anything. If an
  answer breaks off halfway, Argus asks Claude to carry on from where it stopped, so you never hear the
  start twice. Web pages and news feeds get one quick second try, and a slow news feed is skipped rather
  than keeping you waiting.
* **"Argus, stop" is instant**, even when a request is still waiting on the network.

## It learns

The model itself never changes; nobody trains it on your requests. What Argus learns is a short list of
lessons it reads before every request, kept in its database on your Mac (no server or cloud needed):

* **From you:** correct it ("no, when I say the report I mean the PDF in Work", "book on OpenTable first")
  and it saves the correction as a rule.
* **From itself:** when a request ran into errors and it then found a way that worked, a small, cheap
  model looks at what happened and writes one rule, so the same mistake doesn't happen twice (about a
  hundredth of a cent each time).
* `argusctl lessons` lists them; "Argus, forget lesson 3" or `argusctl lessons forget L3` removes one.
  Lessons that mention approvals or safety, links, addresses, long numbers or keys, or tell Argus to send
  something somewhere, are refused.

## Phone calls from your own number

Argus can phone a business for you (book a table, ask about opening hours) from your own number:

1. You ask: "Argus, call Luigi's and book a table for two at seven tomorrow; anything from 6:30 to 8 is fine."
2. It looks the number up, says who it's calling and why, and waits for your OK (yes + the code word,
   or Touch ID).
3. Your Mac places the call through your iPhone ("Calls from iPhone"), so they see your number. Argus
   starts with "Hi, this is (your name)'s AI assistant calling. This call is transcribed for (your name).", then
   talks: one thing at a time, confirms the details back, and says goodbye.
4. You hear the call on the Mac's speakers. Say "Argus, let me talk" to take over with your own
   microphone, or "Argus, hang up". Afterwards it tells you how it went.

It never gives card, bank or account numbers, never agrees to deposits or fees (it says you'll call back),
never leaves voicemails, never calls emergency, service (N11) or pay-per-call numbers, and makes at most
6 calls an hour. Every call, made or tried, is written to `~/.argus/logs/calls.log` (who, why, how it went
and what was said; copied to the logs folder too). Calls are off until you run `scripts/setup_calls.sh` (it installs BlackHole, a virtual
audio cable, SwitchAudioSource, and builds the audiotee call recorder; macOS 14.2 or newer). Tested here
with a simulated phone line; the first real call on your Mac is the real test. Rules on AI phone calls
differ by place (the US treats AI voices as "artificial voices"), so keep it to businesses you'd call
yourself; this isn't legal advice.

## iPhone or Android? Setup asks first

`./setup.sh` starts by asking which phone you use, and sets everything up for it:

| | iPhone | Android |
|---|---|---|
| Talking to Argus away from the Mac | Siri Shortcut (and the Argus Eye app) | Argus runs on the phone itself: home-screen button |
| Your messages | iPhone Mirroring on the Mac (slow) | Texts, WhatsApp's full history (its encrypted backup, with your key) and app notifications, searched on the phone |
| Directions and traffic | Apple Maps; free live traffic through the Mac | Google Maps; live traffic with a Google Maps key |
| When the Mac is off | An always-on server ([docs/SERVER.md](docs/SERVER.md)) | Works anyway (it's on the phone) |
| Approvals away from the Mac | None (risky things wait for the Mac) | A dialog and your fingerprint |
| Calls from your own number | Yes (Mac + iPhone) | No (Android doesn't let apps hear calls); it opens the dialer |
| Meta glasses | Yes (Argus Eye app) | Not yet |
| Listening for "Argus" all the time | At the Mac | At the Mac; on the phone you press a button |

Run `./setup.sh` again (or `./setup.sh --phone android`) to switch. Guides: [docs/IPHONE.md](docs/IPHONE.md),
[docs/ANDROID.md](docs/ANDROID.md).

## Directions and live traffic

Ask "fastest way to the airport?" or "how's the traffic to work?" and Argus works out the best way there:

1. **Asks every maps service that is set up, at the same time:** Apple Maps, Google Maps and TomTom, each
   with live traffic and its other routes.
2. **Lines the answers up.** The same road from two services counts as one way to go (matched by the
   route's shape on the map, not by its name), and each way gets every service's time for it. When they
   agree, the time is solid; when they differ by minutes, Argus says so.
3. **Checks what's in the way:** accidents, closures, closed lanes and road works on each route (from
   TomTom, matched to the road and the direction you're driving), and the last day's news headlines about
   those roads.
4. **Picks one and says why.** Usually the fastest. A route with an accident or a closure on it, or one
   that only a single service suggests, has to be clearly faster to win. You hear the road, the time and
   the reason; ask for the turns and you get them step by step.
5. **Opens your maps app with the trip.** If Google Maps or Waze are on your phone too, the Shortcut gets
   a link for each. When your usual app's own first choice is clearly worse than the best route and
   another of your apps would take the best one, Argus opens that app and tells you.

**About Waze:** Waze offers no service that other software may ask for routes or traffic, and its terms
forbid reading its data automatically, so Argus can't include Waze's own times. Two things make up for it:
Waze drivers' reports also show up in Google Maps' traffic (same company), and Argus can open the trip in
Waze.

Your phone's location comes with the request (the Siri Shortcut, or the Android app); it goes to the maps
services for that trip only, never to Claude or the activity log.

* **Where the traffic times come from:** free from Apple Maps through MapKit on the Mac (setup builds a
  small helper); the Google Routes API (a free-tier Google Maps key); TomTom (a free key from
  developer.tomtom.com: 2,500 requests a day, no card; one trip check uses about 3 to 12), which also
  brings the accidents, closures and road works; or the Apple Maps Server API (an Apple Developer token:
  `argusctl maps-token`). Setup asks for the keys; run `./setup.sh` again to add one later. One service
  gives you its routes; two or three give you the comparison. Without any, Argus still opens the maps app,
  which shows traffic itself.
* **Settings** (`[maps]` in `~/.argus/config.toml`): `app` is the navigation app that opens, `apps` your
  other ones, `traffic` is `"auto"` (compare every service), one service's name, or `"off"`, and `news`
  switches the headline check.
* **What it can't do:** a link can't tell a maps app which of its routes to take, so the app opens with its
  own first choice; Argus tells you which road to pick if that differs. Headlines are unverified: they're
  read to you as headlines and never change the ranking. Incidents come from TomTom only, so without a
  TomTom key there are times but no accident list.
* **Android drive assist** (optional): when the phone is charging and moving at driving speed, Argus asks
  "Where are you headed?", gives you the best route, and speaks up if traffic later makes another route at
  least five minutes faster. On iPhone, a CarPlay/Bluetooth automation can ask the same question
  ([docs/IPHONE.md](docs/IPHONE.md)).

## Weather where you are, and where you're going

* **"What's the weather?"** is answered for where your phone is (it sends its location with the request;
  at the Mac, Argus uses where your phone was in the last few hours). Only if neither is known does it
  use the home city you gave setup, and it says so.
* **Going somewhere:** "I'm driving to Tahoe on Saturday at 8" gets the forecast for Tahoe for when you
  arrive. If Argus doesn't know what you're going for, it asks ("skiing?") and then says plainly whether
  the weather suits it and what to bring. The route answer always includes the weather when you arrive,
  halfway on long drives, and any official US weather warning at the destination.
* **A heads-up before you leave:** Argus offers to keep an eye on the trip. It checks again the evening
  before and an hour before you leave (then with live traffic too), and tells you out loud at the Mac, or
  on your phone if you're away: "Still good for skiing: fresh snow overnight, chains may be required on
  I-80, leave ten minutes early."
* **Where it comes from:** the forecast from [Open-Meteo](https://open-meteo.com) (free, no key, up to 16
  days ahead) and official warnings from the US National Weather Service (weather.gov). Places are sent
  rounded to about a kilometre.

## When the Mac is off

* **Android:** Argus runs on the phone, so nothing changes.
* **iPhone:** run Argus on a small always-on Linux server (a few dollars a month) with
  `sudo scripts/setup_server.sh`: same Shortcut, answers anywhere, reminders as phone notifications (ntfy).
  It's locked down (its own user, can only write its own folder, no shell commands, nothing that needs your
  OK) and keeps its own memory. See [docs/SERVER.md](docs/SERVER.md).
* **Reminders while you're away from the Mac** can also reach your phone: `argusctl push on`.

## Your iPhone

Apple doesn't let any app read other apps' data on the iPhone, and a developer account doesn't change
that; jailbreaking would, but it's rarely possible on current iOS versions and would undo the phone's
security.
The legitimate ways in, from the Mac:

* **iPhone Mirroring** (macOS 15 and iOS 18 or newer): your iPhone's screen in a window on the Mac. Argus
  can use it like any app ("find what Ravi sent me on WhatsApp"), slowly, and it asks separately before
  touching your iPhone. Close it before calls: it drops them.
* **Mac copies:** Messages (if your texts sync to the Mac), Mail, and WhatsApp for Mac.

## Your Android phone

Android lets an app read your texts and notifications (with your permission), and WhatsApp's own backup can
be read with the 64-digit key you choose in WhatsApp. So on Android, Argus runs on the phone itself in
Termux (free, open source): it searches your messages, gives directions from where you are, reminds you out
loud, and asks for your fingerprint before sending a text. Install: [docs/ANDROID.md](docs/ANDROID.md).

## What you need

**Which computer?** Argus runs on a **Mac**. Setup doesn't ask about the computer: it checks it, says
"This computer: a MacBook Air (macOS)", and then asks only which phone you carry. There is **no Windows
version yet** (Argus needs macOS for always listening, Touch ID approvals, using apps on the screen and
Apple Maps); on Windows, `setup.cmd` explains the options. Without a Mac, Argus can run on an Android
phone on its own, or on a small always-on Linux server.

| You have | Run | Argus runs on |
|---|---|---|
| A Mac and an iPhone | `./setup.sh` on the Mac, choose **1) iPhone** | the Mac; the iPhone reaches it with Siri |
| A Mac and an Android phone | `./setup.sh` on the Mac, choose **2) Android**; then setup on the phone | the Mac, and the phone too |
| Only an Android phone | setup in Termux on the phone ([docs/ANDROID.md](docs/ANDROID.md)) | the phone |
| Windows, or no Mac at all | an always-on Linux server ([docs/SERVER.md](docs/SERVER.md)) for your phone | the server |

* An Apple Silicon Mac on macOS 13 or newer (an 8 GB MacBook Air is fine) with Apple's Command Line
  Tools (`xcode-select --install`).
* A **Claude API key** with some credit, from [platform.claude.com](https://platform.claude.com)
  (separate from Claude Pro). Rough guide: a spoken question costs 1 to 3 cents, a web search adds a
  cent or two, and a ten-step task on the screen costs 10 to 30 cents. `argusctl usage` shows the running
  estimate, and you can set a monthly limit in the Claude console.
* Optional, for the phone and glasses: **Tailscale** (free) on the Mac and the phone.
* Optional, for live traffic: nothing on the Mac (Apple Maps is free there); a Google Maps API key for
  Android or the server.
* Android: the free Termux apps from F-Droid ([docs/ANDROID.md](docs/ANDROID.md)). Always-on server: a
  small Debian or Ubuntu server ([docs/SERVER.md](docs/SERVER.md)).

## Install

```bash
git clone https://github.com/SandeepSrivatsava/A.R.G.U.S.git ~/argus
cd ~/argus
./setup.sh
```

The script first asks which phone you use (1 iPhone, 2 Android), then checks the Mac, asks for the Claude
key (typing is hidden), your name and city, and an optional Google Maps key, installs Python and Argus's
packages (every file checked against a pinned hash), downloads the speech models (checksums verified),
builds Argus.app and the Apple Maps helper, and starts it at login. Then:

1. **Click Allow** when macOS asks whether Argus may use the microphone.
2. **Switch on Argus** in System Settings → Privacy & Security → **Screen Recording** and
   **Accessibility**, then run `argusctl restart`. This is what lets Argus see and use apps for you.
3. **Teach it your voice**: `argusctl enroll`. You read five short sentences; Argus learns how you say
   its name and makes a voiceprint. From then on you can talk over it (it stops for your voice, not its
   own), and with voice lock on it answers only you (it asks you about anyone else) and you can approve
   things by voice. Without it, everyone counts as you and approvals are Touch ID only.
4. **Your phone**: iPhone and glasses, [docs/IPHONE.md](docs/IPHONE.md); Android,
   [docs/ANDROID.md](docs/ANDROID.md) (in Termux on the phone, the same `./setup.sh` installs Argus there).

Then say: "Argus, what's the news today?"

### Updating, and running setup again

There's no automatic update yet. To get a new version:

```
cd ~/argus && git pull && ./setup.sh
```

When setup has run before, it starts by listing your saved answers (phone, maps apps, keys, name, home city,
blocked sites, briefing, keeping the Mac awake) and asks **"Keep these answers and only ask what's new?"**
Press Enter, and only questions you've never answered are asked (a key you skipped stays skipped). Answer
`n` to go through every question: each shows your saved answer, and Enter keeps it. Your keys, memory and
lessons are kept either way. `./setup.sh --change-keys` asks for the keys again.

## Everyday commands

```bash
argusctl status            # what Argus is doing, in plain words (--json for everything)
argusctl log               # recent activity (requests, tools, approvals, blocks)
argusctl voice af_sky      # change the voice (and --speed 1.1); it restarts and says hello in it
argusctl voices [name]     # hear the American voices, to pick one
argusctl brains [test]     # other AI models for second opinions: add chatgpt / grok / gemini / groq ...
argusctl google connect    # Gmail, Drive, Calendar, read-only (docs/GOOGLE.md); also: status, disconnect
argusctl errors [--watch]  # recent problems in plain words (or live)
argusctl report            # a problem report on your Desktop that is safe to share
argusctl timing [n]        # how long the last spoken answers took, step by step
argusctl usage             # estimated Claude spend today and this month
argusctl ask "any news?"   # type a request instead of saying it (Argus answers out loud)
argusctl say "Hello"       # speak something
argusctl mic off|on        # microphone
argusctl screen block|allow
argusctl lessons           # what Argus has learned (forget L3 / clear)
argusctl push on|test|off  # reminders on your phone while you're away (ntfy)
argusctl maps-token ...    # a year-long Apple Maps token from your Maps key (docs/SERVER.md)
argusctl doctor            # check models, keys, Claude, the app and its permissions
argusctl calls             # check every piece phone calls need, and what to do about anything missing
argusctl restart           # after editing ~/.argus/config.toml; says when Argus is back (or why not)
argusctl quit              # stop it until the next restart or login
./scripts/doctor.sh        # doctor, plus file privacy, login item and stay-awake checks
./scripts/update.sh        # get the latest Argus and its packages
```

Settings live in `~/.argus/config.toml`: your name and city, the model, web search, the per-request
token budget, follow-up time, clap mode (`"double"`, `"single"` or `"off"`), voice lock, the voice,
blocked apps and sites, how long a pause ends what you're saying (`end_of_speech_seconds`, 0.6), your phone
(`[phone] type`) and its gateway, maps (`[maps]`: which app links open, where traffic comes from, miles or
km), push notifications, the glasses, and the morning headlines (7:00, free) and optional AI briefing. The voice is
made on the Mac (Kokoro, American); `argusctl voice` changes it, and `argusctl voice mac` uses the Mac's own voice.

## Troubleshooting

* **After a restart it doesn't open or answer.** Run `argusctl restart`: it now waits and tells you whether
  Argus came back, and if not, why. Still stuck? `argusctl restart --force` stops every copy and starts it
  fresh. Then `argusctl report` and send the file: its first section says what Argus is doing right now.
  (Opening Argus from Finder while it's already running just shows a note: it's the ◉ in the menu bar.)
* **It cuts itself off, or answers its own words.** That was its own voice from the speakers counted as you.
  It now stops only for your voiceprint or its name: run `argusctl enroll` once so you can just talk over it.
* **Settings typed into the Terminal do nothing.** Lines like `kokoro_voice = "af_sky"` belong in
  `~/.argus/config.toml`, not the Terminal. For the voice, use `argusctl voice af_sky` instead.
* **"Bootstrap failed: 5: Input/output error" at step 8.** macOS hadn't finished stopping the old Argus.
  Setup now waits and tries again; if it still fails, run `launchctl bootout gui/$(id -u)/com.argus.voice`,
  wait a few seconds, and run `./setup.sh` again.
* **It says "that didn't work" to everything.** Run `argusctl errors` (or `argusctl report` and send the file).
  If Claude refuses a conversation (error 400), Argus now starts a fresh one and asks again by itself.
* **It's slow to answer.** Run `argusctl timing` right after a slow answer. "Claude ... to start" is time
  waiting on Claude; a big number there usually means a higher `voice_effort` or a slow network. A tool
  name with a big number (weather, route) is that service being slow.
* **It stopped listening during a call.** That's on purpose: while FaceTime, Zoom, Teams, WhatsApp or any
  other app uses a microphone, Argus closes its own and the menu says "paused (on a call: ...)". It
  listens again a few seconds after the call. For calls on the iPhone itself, which the Mac can't see,
  say "Argus, I'm on a call", then "Argus, I'm off the phone" (or clap twice). A note on screen says which app
  paused it. If that app isn't on a call (WhatsApp can hold the microphone after a voice note), run
  `argusctl ignore-app WhatsApp`, or set `pause_during_calls = false`.
* **"Tailscale isn't connected" during setup.** Not an error: setup finished. It only means the iPhone
  can't reach the Mac yet. Install Tailscale on both, sign in with the same account, then `argusctl restart`
  ([docs/IPHONE.md](docs/IPHONE.md)).
* **It doesn't react to its name.** Check the menu bar icon (◌ means the microphone is off or the Mac is
  locked). Run `argusctl enroll` so it learns how you say "Argus". Or clap twice and talk.
* **It answers the TV.** Run `argusctl enroll` so it knows your voice (voice lock), or put it to sleep.
* **It won't take my spoken "yes".** Say "yes" and the code word it read out, in your normal voice. If it
  keeps refusing, enroll again, or use Touch ID.
* **On a call, they can't hear Argus.** In FaceTime's Video menu, check the microphone isn't fixed to
  one particular mic. `argusctl calls` checks the call setup piece by piece.
* **Claps don't work.** Clap sharply twice, less than a second apart, in a quiet moment. Soft claps or
  claps far from the Mac's microphone may not register. `clap = "off"` under `[listening]` turns it off.
* **"I can't reach Claude right now."** Run `argusctl doctor`: it checks the internet, the key and
  your credit.
* **Screen actions fail.** `argusctl doctor` says whether Argus has Screen Recording and Accessibility.
  Switch Argus on in both (System Settings → Privacy & Security), then `argusctl restart`.
* **Microphone permission was denied.** System Settings → Privacy & Security → Microphone → turn on Argus.
* Logs: `~/.argus/logs/` (activity log `audit.jsonl`, `argus.log`, what a start printed in `voice.err.log`, native
  crashes in `crash.log`). `argusctl report` gathers what matters from all of them.

## How this was tested

* **935 automated tests**: a retired Gemini model setting moving on to Gemini 3.8 Flash; a pause for an app says which and `argusctl ignore-app`;  one sentence of the voice made at a time (the natural voice was measured too slow because several were made at once), code execution only for you;  the logs folder (each line once, a folder per day, old days deleted and nothing else);  restarting (a new copy waiting for the old one, a stuck copy stopped and only ever
  Argus's own, stopping while the main thread and the shutdown are stuck, a native crash and a hang leaving a
  trace, two failed starts bringing a safe one, `argusctl restart` saying whether Argus came back or why not,
  against a stand-in launchd), talking over Argus without a voiceprint (only its name stops it, and the echo of
  its own sentence isn't taken for a question), `argusctl voice`, a microphone that gives only silence, the
  report's "Right now", ChatGPT and Grok next to the free brains (only the question leaves, without names or
  numbers; a slow one left out; a daily cap on paid ones; `argusctl brains add/test/remove`); finding a place and phoning it to book (with your OK), loose ends that speak only when there's news and can only look things up, glances that keep a few words and drop the photo, the quick-voice fallback, `argusctl key` and `brains`; talking over Argus (your voice vs its own echo), the Kokoro voice making the next sentence while speaking, Google reviews, Google search through Gemini and the council against stand-ins, the one-time voice switch for older installs; Gmail, Drive and Calendar against a stand-in Google (read-only sign-in with PKCE, forged sign-in answers refused, codes and password files left out, access tokens never reaching Claude), lookups running at the same time; picking the model for a request and handing it up, mail search with a stand-in Mail app (codes and blocked senders left out, scripts that can only read), a flight's two-week history against a stand-in FlightAware, `argusctl errors` and the report (nothing private in it); starting at login when macOS is slow to stop the old Argus, a conversation Claude refuses being replaced and the question asked again;: how fast spoken answers are asked for (quick effort, thinking only between steps, earlier thinking left out), keeping quiet during calls (which apps count as a call, short gaps, Argus's own calls), "I'm on a call", `argusctl timing`;: the Claude client (streaming, exact rebuilding of tool calls, retries, bad
  keys), the tool loop, approvals (once / this task / deny; by voice with the code word, and by Touch ID,
  whichever answers first), spotting payments (Buy and Pay buttons, checkout pages, card numbers), lessons
  (from corrections and from its own mistakes, and planted lessons refused), guests (asked about, allowed
  for a while, refused), phone calls on a simulated line (booking from hello to goodbye, no answer,
  hang-ups, phone menus, the time limit, losing Claude mid-call, "hang up" and "let me talk", numbers it
  must never call), the policy rules, screen control (scaling,
  stopping after a failed action, locked Mac, password apps), web-fetch safety, cost estimates, memory,
  reminders and cron schedules, the iPhone gateway
  (tokens, lockout, image checks), the glasses bridge, clap detection (double clap yes; single claps,
  triple claps, applause and speech no), name detection including mishearings like "August", the
  listening rules, settings, secrets and logs. And the newer parts: directions (Google Routes, Apple Maps
  and the MapKit helper, against stand-in services: requests, alternatives, traffic delays, errors, token
  refresh), the best-route check (the same road from three services recognised as one, TomTom times and
  incidents matched to the road and the driving direction, road news, the ranking and its reasons, a
  service failing or putting the destination somewhere else, opening the app that takes the best route),
  navigation links that never carry your location, the phone's location through the gateway,
  push notifications, the Apple Maps token (a real ES256 signature, checked with OpenSSL), Argus on Android
  with stand-in Termux commands (searching texts, the notification log and WhatsApp backups, both database
  layouts, a real WhatsApp crypt15 backup made and read with wa-crypt-tools, fingerprint approvals, drive
  assist from "where are you headed?" to "another route is now faster"), server mode (installed by its
  setup script and started the way systemd starts it), and the setup scripts themselves (the phone
  question, switching phones, the Android and server installers, shellcheck).
* **Failure cases on purpose** (about 200 of the tests): the Claude API not answering in time, going
  silent or breaking off mid-reply (retried, or picked up where it stopped), sending junk, refusing the key
  or rate-limiting; dropped and stale connections; "Argus, stop" while a request hangs; slow news feeds;
  crashing tools, approval dialogs and schedules; malformed requests to the iPhone gateway; broken
  settings, secrets and voiceprint files; DNS tricks and links into protected folders; commands that hang,
  can't start or flood the output; and sounds that look like claps (a "p" before a voice, a door slam,
  claps right after music). Every request Argus sends to the stand-in Claude is checked against the rules
  the real API enforces.
* **Coverage**: the tests reach 97% of Argus's code (lines and branches), with or without the end-to-end run.
  What's left is mostly Mac-only (the menu bar, the screen and audio pieces of phone calls, a few
  fallbacks for older macOS). Run `scripts/coverage.sh` (add `--e2e` for the full run) to see it yourself.
* **Real speech**: sentences spoken by the real voice go through the real voice-detection and
  speech-recognition models into the listening loop.
* **End to end** (`tests/e2e.sh`, 35 checks): runs `setup.sh` twice, then starts the whole app with the
  real speech models and a stand-in Claude server: a spoken question is answered by actually running the
  news tool, a dangerous `rm -rf` is held for approval and denied (nothing deleted), a banking site is
  blocked, a double clap opens listening, a reminder is spoken (and one that comes while the Mac is
  locked is spoken once you're back), the iPhone gateway answers only with the right token, and a
  "fastest way to the airport?" with the phone's location comes back with an Apple Maps link while the
  location itself never reaches Claude or the logs.
* **Not testable off a Mac, so check on first run:** the microphone prompt, the menu bar, the approval
  dialog with Touch ID, lock detection, real screen control, the live Claude API with the computer
  tools, spotting Buy buttons through the Mac's accessibility interface, a real phone call (placing it,
  pressing the Call button, hearing the other side, being heard), the iPhone app (not yet compiled), the
  MapKit helper and the call helper (Swift, built on the Mac), the Mail app scripts (AppleScript) and how fast they are on a big mailbox, live FlightAware answers, how much faster spoken answers really are, the live Google and Apple maps services, a real Android phone
  with Termux, and a real cloud server with systemd.

## What's next

[docs/ROADMAP.md](docs/ROADMAP.md): what would make this first phase dependable, and what a second phase could add (real phone apps, Argus speaking first, one memory across devices).

## Uninstall

```bash
argusctl quit; launchctl bootout gui/$(id -u)/com.argus.voice
./scripts/stay-awake.sh off      # if you turned it on during setup
rm -rf ~/.argus ~/Applications/Argus.app ~/Library/LaunchAgents/com.argus.voice.plist ~/.local/bin/argusctl
```

Android and the server: see the end of [docs/ANDROID.md](docs/ANDROID.md) and [docs/SERVER.md](docs/SERVER.md).

## License

MIT. The speech models and libraries keep their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
