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
| [Tailscale](https://tailscale.com) (optional, installed by you) | Private network for the iPhone | BSD-3-Clause client; free personal plan |

## The iPhone app

| Component | Used for | License |
|---|---|---|
| [Meta Wearables Device Access Toolkit](https://github.com/facebook/meta-wearables-dat-ios) 1.0.1 for iOS | Connecting to Meta glasses and their camera | [Meta Wearables Developer Terms](https://wearables.developer.meta.com/terms) and Acceptable Use Policy. Downloaded by Xcode (Swift Package Manager) when you build the app; not included here. Argus Eye's code is its own and doesn't copy Meta's samples. |

## Services

* The **Claude API** (Anthropic) does the thinking; it's used with your own API key under Anthropic's
  terms.
* The news tool reads public RSS feeds (Google News, BBC, NPR); headlines and summaries belong to their
  publishers.
