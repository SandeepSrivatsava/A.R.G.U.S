# Roadmap

Where Argus stands, what would make phase 1 solid, and what phase 2 could add. Nothing here is promised;
it's the order that looks most useful.

## Phase 1: what exists (version 2.3)

Argus on the Mac (always listening, speaks, uses the screen with approvals), reachable from an iPhone
through a Siri Shortcut and from Meta glasses; Argus on an Android phone; an always-on server for when the
Mac is off; phone calls from your own number; memory and lessons; reminders and morning headlines; the
best route from several maps services with incidents and road news.

## Phase 1 improvements (make what exists dependable)

In rough order of value:

1. **Try everything on the real devices.** The tests run against stand-ins for Claude, the maps services,
   Termux and the Mac's own parts. A week of real use on the Mac, the iPhone, an Android phone and a server
   will find what stand-ins can't: the MapKit helper's build, real answers from Google, Apple and TomTom,
   microphone quirks, the Shortcut steps.
2. **Automatic builds on a real Mac** (a GitHub Actions macOS runner): compile the Swift helper and the
   iPhone app and run the tests on every change, so Mac-only breakage shows up at once.
3. **A spending guard.** A daily and monthly limit for Claude with a spoken warning and a hard stop, and a
   count of maps requests so the free allowances aren't passed by accident.
4. **Routes, next steps:**
   * "When should I leave to be there at 9?" (arrival times), and saved places ("home", "work").
   * The commute in the morning briefing: "your usual way is 12 minutes slower today; take 280".
   * Official road reports (the state's 511 / transport department feeds) as a second incident source
     next to TomTom, and tolls in dollars.
   * Learn which routes you actually take, and prefer them when times are close.
   * Reroute warnings while driving on iPhone (today only Android's drive assist does this; on iPhone it
     needs the phase 2 app).
5. **A ready-made Shortcut.** One link that installs the "Argus" Shortcut, instead of building it by hand.
6. **Updates and backups.** `argusctl update` (fetch, check, restart) and an encrypted backup and restore
   of `~/.argus`, also for moving to a new Mac.
7. **One memory.** The Mac, the server and the Android phone each keep their own memory and lessons today.
   Sync them over Tailscale so a fact told to one is known to all.
8. **Voice polish.** Interrupt Argus by speaking while it talks, start speaking sooner on long answers, a
   custom-trained wake word for fewer misses, and more languages.
9. **Calendar, mail and reminders, read-only first.** "What's on today?", "did the landlord reply?",
   without the right to send or delete until that has earned trust.
10. **Security upkeep.** An outside review, hash-pinned installs for every dependency (WhatsApp's reader
    already is), a command that replaces all keys and tokens at once, and signed releases.

## Phase 2 (new abilities)

1. **A real iPhone app.** Siri and Shortcuts built in, Face ID approvals on the phone (so risky actions
   don't wait for the Mac), location while driving for "another route is now faster", a live trip tile,
   and push notifications without ntfy. Needs the Apple Developer Program.
2. **A real Android app.** A wake word and a proper background service instead of Termux, with the same
   approvals.
3. **Glasses, hands-free anywhere.** "What am I looking at?" away from the Mac through the phone app, live
   translation, "remember where I parked".
4. **Argus speaks first.** "Leave in ten minutes for your 3 o'clock, traffic is building", a delayed
   flight, a bill due, a parcel arriving: from your calendar, mail and the traffic check, with a strict
   limit on how often it may interrupt.
5. **One brain, many ears.** The always-on server (or a Mac mini at home) as the hub; the Mac, phone,
   glasses and car as its microphones, speakers and hands. One memory, one activity log, works while the
   laptop sleeps.
6. **Better memory.** Search by meaning over everything you've told it, kept on your own machines, and a
   weekly "here's what I learned about you" that you can correct.
7. **Home and car.** Lights, locks, thermostat (HomeKit, Matter or Home Assistant) and the car's own app
   (lock, climate, range), each behind the same approval rules.
8. **Long jobs.** "Research this tonight and tell me in the morning": work that runs for an hour, with a
   plan you approve once and a report at the end. And a safe way to add new tools (MCP servers), each
   with its own permissions.
9. **Calls, both ways.** Screen unknown callers, summarise voicemail, and take a message.
10. **Other people.** Family voices with their own, smaller permissions.
11. **Windows.** A Windows version: listening, speaking and the phone gateway are portable; screen control,
    approvals (Windows Hello instead of Touch ID) and the maps helper would need Windows versions.
12. **Works offline.** A small local model for simple commands when the internet is down. Needs a Mac with
    more memory than 8 GB.
