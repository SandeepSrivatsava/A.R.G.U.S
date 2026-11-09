# Argus on your iPhone

Two shortcuts give you Argus on the phone:

* **"Hey Siri, Argus"**: say your request; Argus answers out loud.
* **Double-tap the back of the phone**: Argus looks at what's on your iPhone screen and tells you about
  it (or answers a question about it).

The phone talks to your Mac over **Tailscale**, a free private network between your own devices, so
the Mac is never exposed to the internet, and every request needs Argus's secret token.

What the phone can't do: approve risky actions (do those at the Mac with Touch ID, or in Telegram),
and it isn't always listening. Apple doesn't let any app listen in the background or watch the screen
without a dedicated iPhone app; that's the planned next phase (it needs Xcode and a $99/year Apple
developer account).

## 1. Connect the Mac and the iPhone with Tailscale (free)

1. On the Mac: install Tailscale from the Mac App Store (or tailscale.com/download) and sign in.
2. On the iPhone: install Tailscale from the App Store and sign in **with the same account**.
   Turn it on (it can stay on; it uses very little battery).
3. On the Mac, run `./setup.sh` again in the `argus` folder. It notices Tailscale and starts the phone
   gateway. At the end it prints the address, like `http://100.101.102.103:8765`.
4. Get the token (treat it like a password): `argusctl phone-token`. To send it to your phone
   privately, copy it into an AirDrop note or a Notes note you delete afterwards. Don't email it.

## 2. Shortcut 1: "Argus" (Siri)

Open **Shortcuts** → **+** and add these actions in order:

1. **Dictate Text** (Language: English, Stop Listening: After Pause).
2. **Get Contents of URL**
   * URL: `http://100.x.y.z:8765/ask` (your Mac's Tailscale address)
   * Tap the arrow to show more: Method **POST**
   * Headers: add `Authorization` with the value `Bearer YOUR-TOKEN` (the word Bearer, a space, the token)
   * Request Body: **JSON**; add a field `text` (Text) and set it to the **Dictated Text** variable
3. **Get Dictionary Value**: Get **Value** for `reply` in **Contents of URL**
4. **Speak Text**: **Dictionary Value**

Name the shortcut **Argus**. Now say "Hey Siri, Argus", wait for the listening sound, and talk.
Optional: on iPhones with an Action Button, set it to run the Argus shortcut (Settings → Action Button).

## 3. Shortcut 2: "Argus Look" (Back Tap, sees your screen)

1. **Take Screenshot**
2. **Convert Image** to **JPEG** (keeps it small; Argus only accepts PNG or JPEG)
3. **Base64 Encode** (tap to show more: Line Breaks **None**)
4. **Dictate Text** (optional: lets you ask about the screen; leave it out to just get a description)
5. **Get Contents of URL**
   * URL: `http://100.x.y.z:8765/look`, Method **POST**
   * Header `Authorization` = `Bearer YOUR-TOKEN`
   * Request Body: **JSON** with `image` (Text) = **Base64 Encoded**, and `text` (Text) = **Dictated Text**
6. **Get Dictionary Value**: `reply` in **Contents of URL**
7. **Speak Text**: **Dictionary Value**

Name it **Argus Look**. Then on the iPhone: Settings → Accessibility → Touch → **Back Tap** →
Double Tap → **Argus Look**.

The screenshot is only taken when you trigger the shortcut, it's sent over Tailscale to your Mac and
from there to Claude to be described, and it isn't stored (the activity log only notes that a "look"
request happened).

## Good to know

* Long jobs ("research these five laptops") can take longer than Shortcuts likes to wait. Use Telegram
  for those; it shows progress and lets you approve steps with buttons.
* If Argus says "Your Mac couldn't answer right now", the Mac may be asleep. `./scripts/stay-awake.sh on`
  keeps it awake while plugged in.
* Five wrong tokens lock the phone out for 15 minutes. If the token ever leaks, delete
  `~/.argus/phone_token`, run `argusctl restart`, then `argusctl phone-token` for a new one, and update
  both shortcuts.
* Turn the gateway off at any time: set `enabled = false` under `[phone]` in `~/.argus/config.toml`
  and run `argusctl restart`.
