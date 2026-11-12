# Argus Eye: the iPhone app (Meta glasses)

Argus Eye connects your **Meta glasses** to Argus on your Mac:

* It knows when your glasses are connected and tells Argus ("Sandeep is wearing his glasses").
* **Look**: takes a photo with the glasses camera and Argus describes it, out loud through the glasses.
* **Hold to talk**: say anything; "what am I looking at?" automatically includes a glasses photo.
* While the app is open, saying "Argus, what am I looking at?" **at your Mac** also uses the glasses camera.

It uses Meta's official [Wearables Device Access Toolkit](https://wearables.developer.meta.com) (camera)
and ordinary Bluetooth for the glasses' microphones and speakers. Supported: Ray-Ban Meta (Gen 1/Gen 2),
Ray-Ban Meta Optics and Meta Ray-Ban Display, as listed by Meta.

**Status: written but not yet built.** This repo was put together in a cloud workspace without Xcode, so
the app hasn't been compiled. Expect to fix small compile errors the first time; the code follows the
toolkit's 1.0.1 API.

## What you need

* A Mac with **Xcode** (free from the App Store; ~40 GB) and **XcodeGen**: `brew install xcodegen`.
* An Apple ID in Xcode. A free account runs the app on your own iPhone for 7 days at a time. Meta's
  Wi-Fi streaming needs the Hotspot Configuration capability, which may need the paid Apple Developer
  Program ($99/year).
* A free **Meta Wearables Developer Center** account with a project. That gives you the Meta App ID and
  client token. Turn on **Developer Mode** in the Meta AI app (Settings → your glasses → Developer Mode).
* Glasses software v20 or newer, Tailscale on the iPhone, and Argus's phone gateway on (see
  [../docs/IPHONE.md](../docs/IPHONE.md)).

## Build and install

```bash
cd ~/argus/ios
cp Config/Secrets.example.xcconfig Config/Secrets.xcconfig   # then fill in your team ID, bundle ID, Meta app ID and token
xcodegen                                                     # creates ArgusEye.xcodeproj
open ArgusEye.xcodeproj
```

In Xcode, plug in your iPhone, pick it as the run destination, and press **Run**. On the iPhone, trust
the developer certificate when asked (Settings → General → VPN & Device Management).

Then in the app: **Settings** → enter your Mac's address (`http://100.x.y.z:8765`) and the token from
`argusctl phone-token` → **Link my glasses** (the Meta AI app opens to confirm) → **Done**.

## Privacy

* The camera is used only when you press Look, say "what am I looking at", or Argus asks while the app is
  open. The glasses' capture LED lights up each time, as always.
* Speech is turned into text on the iPhone. Photos go over Tailscale to your Mac, then to Claude to be
  described. Nothing is stored on the phone.
* The token is kept in the iPhone's Keychain. Meta's toolkit analytics are switched off in `project.yml`.
* Anything risky still needs your OK at the Mac (Touch ID, or your voice there). The phone can't approve.

## Glances ("where did I put my keys?")

With `glances_minutes = 3` under `[glasses]` on the Mac, the app sends a photo every few minutes while it is
open and the glasses are connected, so Argus can note where your things are. The Mac tells the app how often
(or not at all); nothing to set in the app. The app has to be open: iOS doesn't let it use the glasses'
camera from the background for long.
