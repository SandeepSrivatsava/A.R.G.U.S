#!/usr/bin/env python3
"""Argus news headlines: fetch a few RSS feeds and print the top stories.

Ported from the original personal_assistant/news_updater.py, with these fixes:
- the Reuters feed died in June 2020; Reuters stories now come from a Google News search feed
- feeds are fetched in parallel with a timeout, and failures are reported instead of hidden
- the same story from several feeds is shown once
- certificate checks work on macOS Python builds (uses truststore or certifi when present)

Standard library only, so it runs under any Python 3.8+, including Hermes's own interpreter
for script-only cron jobs.

Examples:
  news.py                              top 8 headlines as text (good for Telegram)
  news.py --top 5 --format json        machine-readable, for the agent
  news.py --topic "electric cars"      headlines about one topic
  news.py --offline saved_feed.xml     parse a saved feed instead of fetching (tests)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Iterable, List, Optional, Sequence, Tuple

DEFAULT_FEEDS: List[Tuple[str, str]] = [
    ("Google News", "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"),
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("Reuters", "https://news.google.com/rss/search?q=when:24h+site:reuters.com&hl=en-US&gl=US&ceid=US:en"),
]
TOPIC_FEED = "https://news.google.com/rss/search?q={query}+when:2d&hl=en-US&gl=US&ceid=US:en"
USER_AGENT = "argus-news/1.0 (+https://github.com/NousResearch/hermes-agent)"
MAX_FEED_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 10


@dataclass
class Article:
    title: str
    source: str
    link: str
    published: Optional[str] = None
    summary: str = ""


def ssl_context() -> ssl.SSLContext:
    """Prefer the OS trust store (truststore), then certifi, then Python's default."""
    try:
        import truststore  # type: ignore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch(url: str, timeout: float = TIMEOUT_SECONDS) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
        data = response.read(MAX_FEED_BYTES + 1)
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("feed is larger than 5 MB")
    return data


def clean_text(text: Optional[str]) -> str:
    """Strip HTML tags and entities and collapse whitespace."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def first_sentences(text: str, count: int = 2) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(sentences[:count]).strip()


def iso_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.strip()).isoformat()
    except (TypeError, ValueError, IndexError):
        return value.strip()


def element_text(parent: ET.Element, tag: str) -> str:
    """All text inside a child element, including text inside raw HTML tags some feeds leave unescaped."""
    child = parent.find(tag)
    return "".join(child.itertext()) if child is not None else ""


def parse_feed(data: bytes, feed_name: Optional[str] = None) -> List[Article]:
    """Parse RSS 2.0 items. Google News titles end in ' - Publisher'; that suffix becomes the source.
    Stories with no publisher of their own are credited to feed_name, or to the feed's own title."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"not valid RSS/XML ({exc})") from exc
    if not feed_name:
        channel = root.find("channel")
        feed_name = clean_text(element_text(channel, "title")) if channel is not None else ""
        feed_name = feed_name or "News"
    articles: List[Article] = []
    for item in root.iter("item"):
        title = clean_text(element_text(item, "title"))
        if not title:
            continue
        source = clean_text(element_text(item, "source"))
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)].strip()
        summary = first_sentences(clean_text(element_text(item, "description")))
        # Google News descriptions only repeat the headline; drop those.
        if summary.lower().startswith(title.lower()[:40]):
            summary = ""
        articles.append(
            Article(
                title=title,
                source=source or feed_name,
                link=(item.findtext("link") or "").strip(),
                published=iso_date(item.findtext("pubDate")),
                summary=summary,
            )
        )
    return articles


def story_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:60]


def merge(feeds: Sequence[List[Article]], top: int) -> List[Article]:
    """Interleave feeds (each feed keeps its own editorial order) and drop repeated stories."""
    kept = {}
    merged: List[Article] = []
    longest = max((len(f) for f in feeds), default=0)
    for i in range(longest):
        for feed in feeds:
            if i >= len(feed):
                continue
            article = feed[i]
            key = story_key(article.title)
            if not key:
                continue
            if key in kept:
                # Same story from another feed: borrow its summary if ours has none.
                if not kept[key].summary and article.summary:
                    kept[key].summary = article.summary
                continue
            if len(merged) < top:
                kept[key] = article
                merged.append(article)
    return merged


def feeds_from_env() -> Optional[List[Tuple[str, str]]]:
    """ARGUS_NEWS_FEEDS='Name|https://...;Name2|https://...' overrides the default feeds."""
    raw = os.environ.get("ARGUS_NEWS_FEEDS", "").strip()
    if not raw:
        return None
    feeds = []
    for part in raw.split(";"):
        if "|" in part:
            name, url = part.split("|", 1)
            feeds.append((name.strip(), url.strip()))
    return feeds or None


def collect(feeds: Iterable[Tuple[str, str]], top: int) -> Tuple[List[Article], List[str]]:
    feeds = list(feeds)
    results: List[List[Article]] = [[] for _ in feeds]
    errors: List[str] = []

    def load(index_and_feed):
        index, (name, url) = index_and_feed
        return index, name, parse_feed(fetch(url), name)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(feeds))) as pool:
        futures = [pool.submit(load, pair) for pair in enumerate(feeds)]
        for future, (name, _url) in zip(futures, feeds):
            try:
                index, _, articles = future.result()
                results[index] = articles
            except Exception as exc:  # one broken feed must not sink the briefing
                errors.append(f"{name}: {exc}")
    return merge(results, top), errors


def as_text(articles: List[Article], heading: str) -> str:
    lines = [heading, ""]
    for number, article in enumerate(articles, 1):
        lines.append(f"{number}. {article.title} ({article.source})")
        if article.summary:
            lines.append(f"   {article.summary}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Print today's top news headlines.")
    parser.add_argument("--top", type=int, default=8, help="how many stories (default 8)")
    parser.add_argument("--topic", help="only stories about this topic (uses Google News search)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--offline", metavar="FILE", action="append",
                        help="read a saved RSS file instead of fetching (repeatable)")
    args = parser.parse_args(argv)
    top = max(1, min(args.top, 50))

    errors: List[str] = []
    if args.offline:
        parsed = []
        for path in args.offline:
            try:
                with open(path, "rb") as handle:
                    parsed.append(parse_feed(handle.read()))
            except (OSError, ValueError) as exc:
                errors.append(f"{path}: {exc}")
        articles = merge(parsed, top)
    else:
        if args.topic:
            query = urllib.parse.quote_plus(args.topic)
            feeds = [(f"Google News: {args.topic}", TOPIC_FEED.format(query=query))]
        else:
            feeds = feeds_from_env() or DEFAULT_FEEDS
        articles, errors = collect(feeds, top)

    for error in errors:
        print(f"warning: feed failed: {error}", file=sys.stderr)
    if not articles:
        print("No articles could be fetched right now (all feeds failed or were empty).", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps([asdict(a) for a in articles], ensure_ascii=False, indent=2))
    else:
        today = datetime.now().strftime("%a %d %b")
        heading = f"Top headlines: {args.topic}, {today}" if args.topic else f"Top headlines, {today}"
        print(as_text(articles, heading))
    return 0


if __name__ == "__main__":
    sys.exit(main())
