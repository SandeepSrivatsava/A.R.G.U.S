# Argus on your iPhone and Meta glasses

Three ways, from simplest to fullest:

| | What you get | What it takes |
|---|---|---|
| **Siri shortcut** | "Hey Siri, Argus" → ask anything, hear the answer | Tailscale + one Shortcut |
| **Glasses photo shortcut** | "Hey Meta, take a photo", then "Hey Siri, Argus look" → Argus describes it | One more Shortcut |
| **Argus Eye app** | Live link to the glasses: knows when you wear them, Look button, push-to-talk through the glasses, and "Argus, what am I looking at?" at the Mac | Build it in Xcode ([ios/README.md](../ios/README.md)) |

What the phone can't do: approve risky actions (do those at the Mac: Touch ID, or your voice with the code word). It
also can't listen all the time: Apple doesn't let any app listen in the background or watch the screen.

## 1. Connect the Mac and the iPhone with Tailscale (free)

1. On the Mac, install Tailscale (Mac App Store or tailscale.com/download) and sign in.
2. On the iPhone, install Tailscale from the App Store and sign in **with the same account**. Turn it on.
3. On the Mac, run `./setup.sh` again in the `argus` folder. It notices Tailscale and turns on the phone
   gateway. At the end it prints the address, like `http://100.101.102.103:8765`.
4. Get the token (treat it like a password): `argusctl phone-token`. Send it to your phone privately,
   for example in a Notes note you delete afterwards. Don't email it.

The gateway only listens on the Mac's Tailscale address, never on Wi-Fi or the internet.

## 2. Shortcut: "Argus" (Siri)

Open **Shortcuts** → **+** and add these actions in order:

1. **Dictate Text** (Language: English, Stop Listening: After Pause).
2. **Get Contents of URL**. Set it up like this:
   * URL: `http://100.x.y.z:8765/ask`
   * Method: **POST**
   * Headers: `Authorization` = `Bearer YOUR-TOKEN`
   * Request Body: **JSON**, field `text` = **Dictated Text**
3. **Get Dictionary Value**: `reply` in **Contents of URL**.
4. **Speak Text**: **Dictionary Value**.

Name it **Argus**. Say "Hey Siri, Argus", wait for the tone, and talk. On iPhones with an Action Button
you can also assign it there.

## 3. Shortcut: "Argus look" (glasses photo or screenshot)

Your glasses' photos land in the iPhone's Photos app automatically if auto-import is on. To check, open
the Meta AI app → Glasses → Device settings → auto-import. So:

1. **Find Photos** where Media Type is Image, Sort by Creation Date, Latest First, Limit 1.
2. **Resize Image** to width 1280.
3. **Convert Image** to **JPEG**.
4. **Base64 Encode** (Line Breaks: None).
5. **Dictate Text** (optional: your question).
6. **Get Contents of URL**:
   * URL: `http://100.x.y.z:8765/look`
   * Method: **POST**
   * Header: `Authorization` = `Bearer YOUR-TOKEN`
   * JSON: `image` = **Base64 Encoded**, `text` = **Dictated Text**
7. **Get Dictionary Value** `reply` → **Speak Text**.

Name it **Argus look**. Use: "Hey Meta, take a photo", then "Hey Siri, Argus look". For the iPhone's own
screen instead, replace step 1 with **Take Screenshot** and assign it to Back Tap (Settings →
Accessibility → Touch → Back Tap → Double Tap).

## 4. The Argus Eye app

See [ios/README.md](../ios/README.md). It needs Xcode on your Mac and a free Meta developer account.
Once it's running and linked to your glasses:

* the menu bar shows "glasses on" while they're connected, and Argus knows you're wearing them;
* "Argus, what am I looking at?" at the Mac takes a photo with the glasses (the app must be open);
* in the app, **Look** and **Hold to talk** work anywhere you have Tailscale, and replies play through
  the glasses.

## Good to know

* Long jobs can take longer than Shortcuts likes to wait. Ask for those at the Mac.
* "Your Mac couldn't answer right now" usually means the Mac is asleep. Run `./scripts/stay-awake.sh on`
  to keep it awake while it's plugged in.
* Five wrong tokens lock the phone out for 15 minutes. If the token leaks: delete `~/.argus/phone_token`,
  run `argusctl restart`, then `argusctl phone-token` for a new one, and update the shortcuts and the app.
* To turn the gateway off, set `enabled = false` under `[phone]` in `~/.argus/config.toml`, then run
  `argusctl restart`.
