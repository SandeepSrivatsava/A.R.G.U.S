# Third-party code, software and models

Argus's own code (everything in this repository) is MIT-licensed; see [LICENSE](LICENSE).

## Code and ideas ported into Argus

These were studied and partly ported (rewritten in Argus's own code, then extended). Their licenses
allow this; the copyright notices are kept here.

| Project | What Argus took from it | License |
|---|---|---|
| [Hermes Agent](https://github.com/NousResearch/hermes-agent), Copyright (c) 2025 Nous Research | The list of dangerous shell-command patterns (`tools/approval_detection.py`), the blocked key combinations for screen control, and the idea of per-task approval scopes. Rewritten and extended in `argus/policy.py` and `argus/agent.py`. | MIT |
| [adewaskar/jarvis](https://github.com/adewaskar/jarvis), Copyright (c) 2026 Aditya Dewaskar | The clap detector (`clap.ts`): a clap is recognised by its shape (quiet before, a near-instant rise, a fast decay) against a running noise floor. Ported to Python in `argus/clap.py` and made stricter for an always-on microphone (double clap, millisecond attack and high-frequency checks, quiet after, noise between claps cancels, applause rejected). | MIT |
| Sandeep's earlier `personal_assistant` project | The news headline fetcher (`news_updater.py`), fixed and extended in `argus/news.py`. | Sandeep's own code |

Ideas only (no code copied), for phone calls and lessons:

| Source | The idea | License |
|---|---|---|
| ["Building an AI Agent That Makes Phone Calls on a Mac"](https://desty.github.io/en/blog/23-phone-ai-agent/) (desty, 2026) | Placing a call through the iPhone with a `tel:` link and pressing the Call button found by its colour; capturing call audio with a Core Audio process tap because a virtual output device gets ducked; iPhone Mirroring dropping calls. | Blog post |
| [MuhsinunC/phone-agent](https://github.com/MuhsinunC/phone-agent) | BlackHole as the call's microphone so the other side hears the agent. | See upstream |
| [icoainc/phonebt](https://github.com/icoainc/phonebt), Copyright 2026 ICOA Inc. | A Claude agent on a real phone line with key presses for menus. | Apache-2.0 |
| Reflexion (Shinn et al., 2023) | Agents writing short notes about their own mistakes and reading them next time (`argus/lessons.py`). | Research paper |

The MIT license text for both projects above:

> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
> associated documentation files (the "Software"), to deal in the Software without restriction, including
> without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the
> following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial
> portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
> LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO
> EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
> IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR
> THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Installed by setup.sh (not included in this repository)

Each keeps its own license.

| Component | Used for | License |
|---|---|---|
| [uv](https://github.com/astral-sh/uv) 0.13.7 | Installing Python 3.12 for Argus | MIT or Apache-2.0 |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 1.12.40 | Running the speech models on the Mac | Apache-2.0 |
| [Silero VAD](https://github.com/snakers4/silero-vad) | Detecting when someone is speaking | MIT |
| [Moonshine](https://github.com/usefulsensors/moonshine) base, English (int8 export by sherpa-onnx) | Speech to text | MIT |
| [Piper](https://github.com/rhasspy/piper) voice `en_GB-alan-medium` | Argus's voice | Piper: MIT. The voice's training data and license are described in the model card installed with it |
| [WeSpeaker](https://github.com/wenet-e2e/wespeaker) ResNet34 (VoxCeleb) | Optional voice lock | Apache-2.0 (model trained on VoxCeleb; see upstream) |
| [NumPy](https://numpy.org), [python-sounddevice](https://github.com/spatialaudio/python-sounddevice) | Audio maths, microphone | BSD-3-Clause, MIT |
| [rumps](https://github.com/jaredks/rumps), [PyObjC](https://github.com/ronaldoussoren/pyobjc) | Menu bar, Touch ID, lock detection, screen control | BSD-3-Clause, MIT |
| [truststore](https://github.com/sethmlarson/truststore), [certifi](https://github.com/certifi/python-certifi) | Checking HTTPS certificates with the Mac's trust store | MIT, MPL-2.0 |
| [Tailscale](https://tailscale.com) (optional, installed by you) | Private network for the phone and the server | BSD-3-Clause client; free personal plan |

The Apple Maps helper (`macos/argus-maps.swift`) is Argus's own code, compiled on your Mac against
Apple's MapKit framework (part of macOS).

## Installed by scripts/setup_calls.sh (optional, for phone calls)

| Component | Used for | License |
|---|---|---|
| [audiotee](https://github.com/makeusabrew/audiotee), Copyright (C) 2025 Nick Payne, commit `56ac954` | Hearing the other side of a call (a Core Audio process tap); built from source on your Mac | MIT |
| [BlackHole](https://github.com/ExistentialAudio/BlackHole) 2ch (Homebrew) | The call's microphone during a call | GPL-3.0 (a separate driver; Argus only selects it as an audio device) |
| [SwitchAudioSource](https://github.com/deweller/switchaudio-osx) (Homebrew) | Switching the microphone for the call and back | MIT |

## Argus on Android (scripts/setup_android.sh, inside Termux)

Nothing here is included in this repository; Argus only runs these programs.

| Component | Used for | License |
|---|---|---|
| [Termux](https://github.com/termux/termux-app) and its packages (Python, git, clang) | Running Argus on the phone | GPL-3.0 app; each package keeps its own license |
| [Termux:API](https://github.com/termux/termux-api) and the `termux-api` commands | Texts, notifications, contacts, location, speech, the fingerprint prompt, dialogs | GPL-3.0 (separate app; Argus runs its commands) |
| [Termux:Widget](https://github.com/termux/termux-widget), [Termux:Boot](https://github.com/termux/termux-boot) | The home-screen button; starting at boot | GPL-3.0 (separate apps) |
| [wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools) 0.1.0, Copyright (C) Davide Palma (ElDavoo) | Optional: decrypting your own WhatsApp backup with your 64-digit key. Installed into its own folder and run as a separate program (`wadecrypt`); Argus doesn't import or include its code. | GPL-3.0-or-later |
| [PyCryptodome(x)](https://github.com/Legrandin/pycryptodome) 3.23.0 | AES for wa-crypt-tools (built on the phone) | BSD-2-Clause and Public Domain |
| [protobuf](https://github.com/protocolbuffers/protobuf) 5.27.5, [javaobj-py3](https://github.com/tcalmant/python-javaobj) 0.4.4 | Reading the backup's header and key file, for wa-crypt-tools | BSD-3-Clause, Apache-2.0 |
| [setuptools](https://github.com/pypa/setuptools) 80.9.0 | Building PyCryptodome on the phone | MIT |

## The iPhone app

| Component | Used for | License |
|---|---|---|
| [Meta Wearables Device Access Toolkit](https://github.com/facebook/meta-wearables-dat-ios) 1.0.1 for iOS | Connecting to Meta glasses and their camera | [Meta Wearables Developer Terms](https://wearables.developer.meta.com/terms) and Acceptable Use Policy. Downloaded by Xcode (Swift Package Manager) when you build the app; not included here. Argus Eye's code is its own and doesn't copy Meta's samples. |

## Services

* The **Claude API** (Anthropic) does the thinking; it's used with your own API key under Anthropic's
  terms.
* The news tool reads public RSS feeds (Google News, BBC, NPR); headlines and summaries belong to their
  publishers.
* Directions use, when set up with your own key or token: the **Google Routes API** (Google Maps Platform
  terms) or the **Apple Maps Server API** (Apple Developer Program); on the Mac, Apple's **MapKit**.
  With your own free key, **TomTom**'s Routing, Geocoding and Traffic Incident Details APIs (TomTom's
  developer terms) add a third set of times and the incidents on each route. Road news comes from a
  Google News search feed. Each service is used under its own terms with your own key; Argus compares
  their answers for you and shows no map.
  Navigation links open Apple Maps, Google Maps or Waze. No Waze data is read: Waze has no public service
  for routes or traffic, and its terms forbid automatic access.
* Weather data: [Open-Meteo](https://open-meteo.com) (free for non-commercial use; data licensed CC BY 4.0,
  "Weather data by Open-Meteo.com") and the US National Weather Service API (api.weather.gov, US
  government data). Used through their public web APIs; no code from either is included.
* Push notifications use [ntfy](https://ntfy.sh) (open source, Apache-2.0 / GPL-2.0), through its public
  web API: ntfy.sh or your own server. No ntfy code is included.

Flight history, with your own key: **FlightAware AeroAPI** (flightaware.com/aeroapi), under FlightAware's terms.
Mail is read through Apple's Mail app on your Mac; nothing of Apple's is included in Argus.
