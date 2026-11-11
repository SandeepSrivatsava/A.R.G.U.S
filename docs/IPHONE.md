# Argus on your iPhone and Meta glasses

(Using Android? See [ANDROID.md](ANDROID.md). `./setup.sh` asks which phone you have.)

Three ways, from simplest to fullest:

| | What you get | What it takes |
|---|---|---|
| **Siri shortcut** | "Hey Siri, Argus" → ask anything, hear the answer, get directions with live traffic | Tailscale + one Shortcut |
| **Glasses photo shortcut** | "Hey Meta, take a photo", then "Hey Siri, Argus look" → Argus describes it | One more Shortcut |
| **Argus Eye app** | Live link to the glasses: knows when you wear them, Look button, push-to-talk through the glasses, and "Argus, what am I looking at?" at the Mac | Build it in Xcode ([ios/README.md](../ios/README.md)) |

What the phone can't do: approve risky actions (do those at the Mac: Touch ID, or your voice with the code word). It
also can't listen all the time: Apple doesn't let any app listen in the background or watch the screen.
When the Mac is off, point the same Shortcut at an always-on Argus server instead: [SERVER.md](SERVER.md).

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
2. **Get Current Location** (lets Argus give directions from where you are; the first time, allow
   Shortcuts to use your location "While Using the App").
3. **Get Contents of URL**. Set it up like this:
   * URL: `http://100.x.y.z:8765/ask`
   * Method: **POST**
   * Headers: `Authorization` = `Bearer YOUR-TOKEN`
   * Request Body: **JSON**, field `text` = **Dictated Text**, and field `location` (Dictionary) with
     `lat` = **Current Location → Latitude** and `lon` = **Current Location → Longitude**
4. **Get Dictionary Value**: `reply` in **Contents of URL**, then **Speak Text**: **Dictionary Value**.
5. **Get Dictionary Value**: `open` in **Contents of URL**. **If** it **has any value** → **Open URLs**:
   **Dictionary Value** (that's a navigation link: it opens your maps app with the trip). End If.

**If Google Maps or Waze are on your iPhone too** (you told setup so), Argus also sends `links`: the same
trip for each app. To choose every time, use this instead of step 5: **Get Dictionary Value**: `links` in
**Contents of URL** → **If** it **has any value** → **Choose from List**: **Dictionary Value** (it shows
the app names) → **Open URLs**: **Chosen Item** → **Otherwise** → the two actions of step 5 → End If.
With step 5 as it is, the app Argus recommends opens by itself.

Name it **Argus**. Say "Hey Siri, Argus", wait for the tone, and talk. On iPhones with an Action Button
you can also assign it there.

The location goes only to the Mac (over Tailscale), and from there to the maps service for the trip you
asked about. Claude doesn't see it, and it isn't written to the activity log. Leave step 2 out if you don't
want directions from where you are.

### Directions and "where are you headed?" in the car

* Ask: "Hey Siri, Argus" → "fastest way to the airport?" Argus asks Apple Maps, Google Maps and TomTom
  (whichever are set up) at once, lines their routes up, checks accidents, closures, road works and road
  news on each, tells you the best way and why, and your maps app opens with the trip. Ask "give me the
  turns" for the steps.
* Apple Maps' times come free from the Mac (setup builds a small MapKit helper). Add a Google Maps key for
  Google's times and a free TomTom key for a third opinion and the accident list: run `./setup.sh` again
  on the Mac and it asks. An always-on server uses keys only ([SERVER.md](SERVER.md)).
* Waze's own times can't be read by other software (Waze offers no way, and forbids it), so Waze is one of
  the apps the trip can open in. Its drivers' reports also show up in Google Maps' traffic.
* The maps app opens with its own first choice of route. If Argus recommended another road, pick that one
  on the app's route screen; Argus tells you which.
* To be asked automatically when you start driving: Shortcuts → Automation → **+** → **CarPlay** (or
  **Bluetooth** with your car) → When Connects → Run Immediately → add **Ask for Input** ("Where are you
  headed?") and then the same steps as above, sending the text "Directions to " + **Provided Input**.
  Apple Maps keeps rerouting around new traffic once you're driving.

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
* "Argus couldn't answer right now" usually means the Mac is asleep. Run `./scripts/stay-awake.sh on`
  to keep it awake while it's plugged in, or set up an always-on server ([SERVER.md](SERVER.md)).
* Reminders while you're away from the Mac can come to the phone as notifications: `argusctl push on`
  ([SERVER.md](SERVER.md#reminders-from-the-mac-while-youre-away)).
* Five wrong tokens lock the phone out for 15 minutes. If the token leaks: delete `~/.argus/phone_token`,
  run `argusctl restart`, then `argusctl phone-token` for a new one, and update the shortcuts and the app.
* To turn the gateway off, set `enabled = false` under `[phone]` in `~/.argus/config.toml`, then run
  `argusctl restart`.
