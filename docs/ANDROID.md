# Argus on Android

On Android, Argus runs **on the phone itself**, so it works wherever you are, with or without the Mac. It
can read your texts, WhatsApp's full history (from WhatsApp's own encrypted backup) and what your other
apps showed as notifications; knows where you are when you ask for directions; gives you the fastest route
with live traffic; and reminds you of things out loud.

When you run `./setup.sh` on the Mac it asks first which phone you use. Pick **Android** and the Mac is set
up for it (Google Maps links, no iPhone-only features). Then put Argus on the phone with the steps below.

## What you get

| You say (press the Argus button first) | Argus |
|---|---|
| "Find the wifi password Priya sent me" | Searches your texts, WhatsApp and notifications, and tells you the exact detail, who sent it and when. |
| "What was the flight number in that airline email last month?" | Same search, limited to the last month. |
| "How's the traffic to the airport?" / "Fastest way to Powell's?" | Asks Google Maps and TomTom (whichever have a key) for live-traffic routes from where you are, checks accidents, closures, road works and road news on each, says which way is best and why, and opens Google Maps (or Waze, if you chose it) with the trip. |
| "Text Mom I'll be 10 minutes late" | Shows what it will send; you approve with a dialog and your fingerprint. |
| "Call the dentist" | Opens the dialer with the number; you press call. |
| "Remind me at 6 to pick up the parcel" | A notification at 6, read aloud. |
| (you start driving with the phone charging) | Drive assist, if you switched it on: "Where are you headed?" You answer; it gives you the best route, opens Maps, and speaks up if traffic later makes another route at least 5 minutes faster. |

Not possible on Android (Android doesn't allow it for apps like this): listening all the time for "Argus"
(you press a button instead), and Argus talking on a phone call for you (Android keeps call audio from other
apps). Phone calls from your own number by Argus need an iPhone and the Mac.

## Install (about 15 minutes)

1. **Install four free apps** from [F-Droid](https://f-droid.org) (or each one's GitHub releases page; take
   all four from the same place): **Termux**, **Termux:API**, **Termux:Widget** and **Termux:Boot**.
   Don't use the old Termux on the Play Store. Open each app once.
2. **In Termux, get Argus.** The repository is private, so sign in to GitHub first:

   ```bash
   pkg install git gh
   gh auth login                      # choose GitHub.com, HTTPS, and log in with a browser
   gh repo clone SandeepSrivatsava/A.R.G.U.S argus
   cd argus && ./setup.sh
   ```

   `setup.sh` sees that it's running in Termux and installs Argus on the phone
   (`scripts/setup_android.sh`). It asks for:
   * your **Claude API key** (typing is hidden; a separate key from the Mac's is a good idea, so you can
     cancel one without the other);
   * your name and city;
   * a **Google Maps API key** for live traffic (optional; see below);
   * your **WhatsApp backup key** (optional; see below);
   * whether to switch on **drive assist**.
3. **Allow what Android asks** during setup: storage (for WhatsApp's backups), SMS, contacts, location
   ("Allow all the time" for drive assist). Then, once, in Android's Settings:
   * Notifications → Device & app notifications (or Notification access) → **Termux:API** on, so Argus can
     keep the notification log;
   * Apps → Termux:API → **Display over other apps** → allow, so approval dialogs can appear;
   * Apps → Termux and Termux:API → Battery → **Unrestricted**, so reminders aren't frozen.
4. **Add the button:** long-press the home screen → Widgets → Termux:Widget → pick **Argus**. Tap it and
   talk. You can also type: `argus-android ask "what's on today?"`.

Argus starts by itself after a restart (Termux:Boot). `argus-android status` shows whether it's running.

### Live traffic: a Google Maps key (optional)

Without a key, directions still work: Argus opens Google Maps, which shows traffic itself. With a key,
Argus can compare routes and tell you which is faster.

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a project, and switch on the
   **Routes API**.
2. Create an API key (APIs & Services → Credentials). Restrict it to the Routes API.
3. Give it to setup (`./setup.sh --change-keys` asks again).

Google gives a monthly free allowance that covers normal personal use; traffic-aware routes count as the
"Pro" tier. Set a budget alert in the console if you like. Prices and free tiers change; check Google's
pricing page.

### Accidents and a second opinion: a TomTom key (optional)

A free key from developer.tomtom.com (2,500 requests a day, no card) adds TomTom's live-traffic times next
to Google's, and the list of accidents, closures and road works on each route. Setup asks for it; to add it
later, run `bash scripts/setup_android.sh` again. Waze's own times can't be read by other software, so
Waze is only an app the trip can open in (setup asks whether you have it).

### WhatsApp's full history (optional)

Android doesn't let one app read another's data, and WhatsApp keeps its messages to itself. But WhatsApp
writes a daily backup of all your chats to the phone, and if you choose the end-to-end encrypted backup
with a **64-digit key**, that key is all that's needed to read it:

1. In WhatsApp: Settings → Chats → Chat backup → **End-to-end encrypted backup** → turn on → **Use
   64-digit encryption key instead** → write the key down somewhere safe (a password manager).
2. Give the key to setup. Setup then installs **wa-crypt-tools** (an open-source WhatsApp backup reader,
   GPL-3.0) into its own folder, with every file checked against a pinned hash.
3. Argus finds WhatsApp's newest backup (WhatsApp makes one every night, or tap Back up), decrypts it on
   the phone into Argus's private folder, and searches it. It decrypts again only when WhatsApp has written
   a newer backup, so the search covers up to the last backup; newer messages are in the notification log.

Messages that arrive while Argus runs are also kept from their notifications (90 days; set
`notification_days` under `[android]`).

## What stays private

* Everything Argus reads stays on the phone, in Termux's private storage (`~/.argus`, which other apps
  can't open). The decrypted WhatsApp copy and the notification log are in `~/.argus/private`.
* Only the messages that match a search go to Claude, to answer you. Claude is told they're other
  people's words: information, never instructions.
* Your location is read only when a request needs it (directions, drive assist). It goes to Google's
  routing service, not to Claude, and isn't written to the activity log.
* Sending a text always needs your OK: a dialog that says exactly what will be sent and to whom, then your
  fingerprint. Texts to short codes (which can cost money) and texts with card numbers are refused.
* Argus can't run shell commands on Android (Android gives Termux no sandbox to box them in), and it never
  reads its own key files.
* **Speech recognition is Android's**, not Argus's: on many phones it can run offline (Pixel phones, and
  "offline speech recognition" in Google's settings); otherwise Google processes what you say. The Mac's
  Argus keeps all audio on the Mac.

## Talking to the Mac from Android (optional)

The Mac's Argus has its own gateway on your private Tailscale network. To reach it from Android, install
**Tailscale** and **HTTP Shortcuts** (free, open source), then make a shortcut:

* Method POST, URL `http://<your Mac's Tailscale address>:8765/ask`
* Header `Authorization: Bearer <token>` (on the Mac: `argusctl phone-token`)
* Body (JSON): `{"text": "{{ask}}"}` where `ask` is a "text input" variable
* Response handling: show or speak the `reply` field; if there's an `open` field, open it (that's a
  navigation link).

The phone's own Argus and the Mac's keep separate memories.

## Troubleshooting

* **"Argus isn't running."** Run `argus-android serve &` in Termux, or restart the phone. Check
  `~/.argus/logs/android.log`.
* **It stops after a while.** Set Termux and Termux:API's battery use to Unrestricted. On Android 12 to 14,
  Android may also stop Termux's background programs: in Developer options, turn on "Disable child process
  restrictions" if you see that.
* **Approvals never appear.** Allow "Display over other apps" for Termux:API.
* **WhatsApp search says no backup was found.** Tap Back up in WhatsApp, and make sure Termux has storage
  access (`termux-setup-storage`).
* **"couldn't decrypt the WhatsApp backup".** The 64-digit key must be the one WhatsApp shows for the
  current backup. Run `./setup.sh --change-keys` to enter it again.
* **No live traffic.** `argus-android status` shows which traffic service is set up; add a Google Maps key
  with `./setup.sh --change-keys`.

## Uninstall

```bash
pkill -f "argus android serve"
rm -rf ~/.argus ~/.shortcuts/tasks/Argus ~/.termux/boot/argus "$PREFIX/bin/argus-android" ~/argus
```
