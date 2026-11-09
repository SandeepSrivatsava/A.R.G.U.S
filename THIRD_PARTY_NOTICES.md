# Third-party software and models

Argus's own code (everything in this repository) is MIT-licensed; see LICENSE. It installs, but does
not include, the following. Each keeps its own license.

| Component | Used for | License |
|---|---|---|
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous Research), with cua-driver | The brain: agent, tools, approvals, Telegram, schedules, desktop control | MIT (cua-driver: see its repository) |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 1.12.40 | Running the speech models on the Mac | Apache-2.0 |
| [Silero VAD](https://github.com/snakers4/silero-vad) | Detecting when someone is speaking | MIT |
| [Moonshine](https://github.com/usefulsensors/moonshine) base, English (int8 export by sherpa-onnx) | Speech to text | MIT |
| [Piper](https://github.com/rhasspy/piper) voice `en_GB-alan-medium` | Argus's voice | Piper: MIT. Voice trained on the Mycroft Mimic 3 "apope" dataset; see the model card installed with it |
| [WeSpeaker](https://github.com/wenet-e2e/wespeaker) ResNet34 (VoxCeleb) | Optional voice lock | Apache-2.0 (model trained on VoxCeleb; see upstream) |
| [rumps](https://github.com/jaredks/rumps), [PyObjC](https://github.com/ronaldoussoren/pyobjc) | Menu bar, Touch ID, lock detection | BSD-3-Clause, MIT |
| [python-sounddevice](https://github.com/spatialaudio/python-sounddevice), [NumPy](https://numpy.org) | Microphone, audio maths | MIT, BSD-3-Clause |
| [Tailscale](https://tailscale.com) (optional, installed by you) | Private network for the iPhone | BSD-3-Clause client; free personal plan |

The news-briefing skill reads public RSS feeds (Google News, BBC, NPR); headlines and summaries belong
to their publishers.
