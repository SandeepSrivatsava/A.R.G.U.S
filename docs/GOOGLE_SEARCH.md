# Google reviews, Google search and second opinions

Argus answers with Claude, and can check Google and other AI models first:

| Ask | What Argus uses | What you need |
|---|---|---|
| "Is Udupi Palace any good?", "best dosa near me" | **place_search**: Google Maps rating, number of reviews, price, open now, Google's review summary, recent reviews | your Google Maps key, with **Places API (New)** switched on |
| "What's on in New York the week of the 12th?" | **google_search**: Gemini answers from live Google Search, with the pages it used | a free **Gemini API key** |
| "Should I go in December or April?" | **ask_council**: Gemini (and any others you add) answer at the same time; Claude compares and decides | the same Gemini key |

Claude's own `web_search` runs alongside, and Argus tells you where sources disagree.

## Google Maps reviews

1. https://console.cloud.google.com → the project with your Google Maps key → search **Places API (New)** →
   **Enable**.
2. Nothing to change in Argus: it already has the key. `argusctl restart`.

Reviews are a separately priced kind of Places request, with a monthly free allowance per Google account;
check the Google Maps Platform pricing page and set a budget alert in Google Cloud if you like.

## Gemini (free)

1. https://aistudio.google.com/apikey → **Create API key** (no card needed).
2. `./setup.sh` asks for it (or press Enter to skip), then `argusctl restart`.

The free tier of Gemini 2.5 Flash includes Grounding with Google Search, with a daily limit of free
searches. Only the question is sent, never your memory, mail or the conversation; on the free tier Google
may use what's sent to improve its products. Change the model with `gemini_model` under `[brain]`.

## More brains for the council

Any service with the common OpenAI-style chat API (many have free tiers, such as Groq or OpenRouter's free
models) can join. Put its key in secrets.env without typing it on a command line:

```bash
read -rs KEY && printf 'GROQ_API_KEY=%s\n' "$KEY" >> ~/.argus/secrets.env; unset KEY
```

then add it to `~/.argus/config.toml`:

```toml
[council]
extra = ["Groq Llama | https://api.groq.com/openai/v1/chat/completions | <model name> | GROQ_API_KEY"]
```

and `argusctl restart`. Each member gets only the question.
