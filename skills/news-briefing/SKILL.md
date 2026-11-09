---
name: news-briefing
description: Today's top news headlines (Google News, BBC, NPR, Reuters) or headlines about one topic, from a fast free RSS script. Use for "what's the news", morning briefings, or "any news about X".
version: 1.0.0
author: argus
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [News, Briefing, RSS, Headlines]
---

# News briefing

Fetches current headlines from public RSS feeds with a small Python script. It costs no API credits and needs no keys.

## When to Use

- The user asks for news, headlines, "what's happening today", or a briefing.
- A scheduled morning briefing needs headlines.
- The user asks for news about a topic, company, or person ("any news about Tesla?").

## Quick Reference

| Need | Command |
|---|---|
| Top stories | `python3 "${HERMES_HOME:-$HOME/.hermes}/scripts/argus_news.py" --top 8 --format json` |
| One topic | `python3 "${HERMES_HOME:-$HOME/.hermes}/scripts/argus_news.py" --topic "<topic>" --top 6 --format json` |

If that path is missing, run `scripts/news.py` from this skill's own folder (the `skill_dir` that `skill_view` reports) with the same arguments.

## Procedure

1. Run the matching command above. The JSON is a list of stories with `title`, `source`, `link`, `published` and `summary`.
2. Pick the 3 to 5 most important stories. Give each one plain sentence and name the source.
3. If the user wants more on a story, open its `link` with the web tools and summarize the article.

## Pitfalls

- If the script exits with "No articles could be fetched", the feeds are down or the Mac is offline. Say so, then fall back to a web search for today's top news.
- Never paste the raw JSON to the user.
- Headlines about developing stories can change. Say "reports say" rather than stating them as settled facts.

## Verification

The command printed at least one story with a title and a link.
