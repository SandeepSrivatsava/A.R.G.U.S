# Google reviews, Google search and second opinions

Argus answers with Claude, and can check Google and other AI models first:

| Ask | What Argus uses | What you need |
|---|---|---|
| "Is Udupi Palace any good?", "best dosa near me" | **place_search**: Google Maps rating, number of reviews, price, open now, Google's review summary, recent reviews | your Google Maps key, with **Places API (New)** switched on |
| "What's on in New York the week of the 12th?" | **google_search**: Gemini answers from live Google Search, with the pages it used | a free **Gemini API key** |
| "Should I go in December or April?" | **ask_council**: Gemini, ChatGPT, Grok and any others you add answer at the same time; Claude compares and decides | the same Gemini key; ChatGPT and Grok need their own (pay-per-use) keys |

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

## More brains for the council: the quick way

```bash
argusctl brains                   # who's in, who isn't, and what each costs
argusctl brains add chatgpt       # OpenAI: key from platform.openai.com/api-keys (pay-per-use)
argusctl brains add grok          # xAI: key from console.x.ai (pay-per-use)
argusctl brains add groq          # free: OpenAI's open model gpt-oss-120b, key from console.groq.com/keys
argusctl brains add mistral       # free mode: key from console.mistral.ai/api-keys
argusctl brains add openrouter    # free models: key from openrouter.ai/settings/keys
argusctl brains test              # does each one answer?
argusctl restart
```

You paste each key hidden; Argus asks the new brain one tiny question to check it answers.

**About "free ChatGPT and Grok".** There's no free API for either (November 2026): a ChatGPT Plus or SuperGrok
subscription covers their apps, not API use. Both bill per question from prepaid credit, like Claude: GPT-6
Luna costs $0.10 per million tokens (pieces of words) in and $0.50 out, so a council question is about a
twentieth of a cent; Grok 4.7 costs $2 and $6, under a cent a question. OpenAI gives free daily tokens to some accounts that
let it train on their prompts, and xAI has a similar data-sharing program; Argus doesn't need either, and for a
personal assistant it's better to leave them off. GitHub's free GPT and Grok access (GitHub Models) closed on
July 30, 2026. The free way to have an OpenAI brain is Groq, which runs OpenAI's open model gpt-oss-120b.

**Limits.** Each paid brain answers at most 40 questions a day (`paid_per_day` under `[council]`). Argus waits
12 seconds (`wait_seconds`) and then goes with the answers it has, so a slow brain never holds you up.

**What's sent.** Only the question: never memory, mail or the conversation, and email addresses, phone numbers,
long numbers and your name are removed first. Free tiers may use what's sent to improve their models; in
Mistral's admin console you can switch that off.

## Any other service

Any other service with the common OpenAI-style chat API can join. Put its key in secrets.env without typing it on a command line:

```bash
read -rs KEY && printf 'MY_MODEL_KEY=%s\n' "$KEY" >> ~/.argus/secrets.env; unset KEY
```

then add it to `~/.argus/config.toml`:

```toml
[council]
extra = ["My model | https://api.example.com/v1/chat/completions | <model name> | MY_MODEL_KEY"]
```

and `argusctl restart`. Each member gets only the question.
