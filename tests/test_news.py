"""Tests for argus/news.py (the headlines used by the get_news tool and the morning job)."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPT = ROOT / "argus" / "news.py"

spec = importlib.util.spec_from_file_location("argus_news", SCRIPT)
news = importlib.util.module_from_spec(spec)
sys.modules["argus_news"] = news  # dataclasses look the module up while the class is built
spec.loader.exec_module(news)


def run_main(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = news.main(list(args))
    return code, out.getvalue(), err.getvalue()


class ParseFeedTests(unittest.TestCase):
    def test_google_news_title_suffix_becomes_source(self):
        items = news.parse_feed((FIXTURES / "google_news.xml").read_bytes(), "Google News")
        self.assertEqual(items[0].title, "Storm closes Bay Area highways")
        self.assertEqual(items[0].source, "Reuters")
        self.assertEqual(items[2].source, "AP News")

    def test_google_news_headline_echo_is_not_kept_as_summary(self):
        items = news.parse_feed((FIXTURES / "google_news.xml").read_bytes(), "Google News")
        self.assertEqual(items[0].summary, "")
        self.assertEqual(items[1].summary, "")

    def test_bbc_summary_is_cleaned_and_shortened(self):
        items = news.parse_feed((FIXTURES / "bbc.xml").read_bytes(), "BBC")
        self.assertEqual(items[0].source, "BBC")
        self.assertEqual(items[0].summary, "Heavy rain shut three highways overnight. Officials & crews worked until dawn.")
        self.assertEqual(items[1].summary, "Delegates returned on Monday. Both sides said progress was possible.")

    def test_published_date_becomes_iso(self):
        items = news.parse_feed((FIXTURES / "google_news.xml").read_bytes(), "Google News")
        self.assertTrue(items[0].published.startswith("2026-11-09T14:00:00"))
        self.assertIsNone(items[2].published)

    def test_broken_xml_raises_value_error(self):
        with self.assertRaises(ValueError):
            news.parse_feed((FIXTURES / "broken.xml").read_bytes(), "broken")


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.google = news.parse_feed((FIXTURES / "google_news.xml").read_bytes(), "Google News")
        self.bbc = news.parse_feed((FIXTURES / "bbc.xml").read_bytes(), "BBC")

    def test_same_story_from_two_feeds_appears_once(self):
        merged = news.merge([self.google, self.bbc], top=10)
        titles = [a.title for a in merged]
        self.assertEqual(titles.count("Storm closes Bay Area highways"), 1)
        self.assertEqual(len(merged), 4)

    def test_feeds_are_interleaved_and_top_is_respected(self):
        merged = news.merge([self.google, self.bbc], top=3)
        self.assertEqual([a.source for a in merged], ["Reuters", "CNBC", "BBC"])


class CommandLineTests(unittest.TestCase):
    def test_text_output_is_a_numbered_list(self):
        code, out, err = run_main("--offline", str(FIXTURES / "google_news.xml"), "--offline", str(FIXTURES / "bbc.xml"))
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("Top headlines, "))
        self.assertIn("1. Storm closes Bay Area highways (Reuters)", out)
        # No <source> per item, so the story is credited to the feed's own title.
        self.assertIn("Peace talks resume in Geneva (BBC News - World)", out)

    def test_json_output_parses(self):
        code, out, _ = run_main("--offline", str(FIXTURES / "bbc.xml"), "--format", "json", "--top", "1")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data), 1)
        self.assertEqual(set(data[0]), {"title", "source", "link", "published", "summary"})

    def test_one_broken_feed_warns_but_still_succeeds(self):
        code, out, err = run_main("--offline", str(FIXTURES / "broken.xml"), "--offline", str(FIXTURES / "bbc.xml"))
        self.assertEqual(code, 0)
        self.assertIn("warning: feed failed", err)
        self.assertIn("Peace talks resume in Geneva", out)

    def test_all_feeds_broken_exits_nonzero(self):
        code, out, err = run_main("--offline", str(FIXTURES / "broken.xml"))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("No articles could be fetched", err)


class ConfigTests(unittest.TestCase):
    def test_feed_override_from_environment(self):
        old = os.environ.get("ARGUS_NEWS_FEEDS")
        os.environ["ARGUS_NEWS_FEEDS"] = "Verge|https://www.theverge.com/rss/index.xml; NPR|https://feeds.npr.org/1001/rss.xml"
        try:
            self.assertEqual(news.feeds_from_env(), [
                ("Verge", "https://www.theverge.com/rss/index.xml"),
                ("NPR", "https://feeds.npr.org/1001/rss.xml"),
            ])
        finally:
            if old is None:
                os.environ.pop("ARGUS_NEWS_FEEDS", None)
            else:
                os.environ["ARGUS_NEWS_FEEDS"] = old

    def test_default_feeds_no_longer_use_dead_reuters_rss(self):
        self.assertFalse(any("feeds.reuters.com" in url for _, url in news.DEFAULT_FEEDS))

    def test_topic_feed_url_is_encoded(self):
        url = news.TOPIC_FEED.format(query=news.urllib.parse.quote_plus("electric cars & EVs"))
        self.assertIn("q=electric+cars+%26+EVs+when:2d", url)


if __name__ == "__main__":
    unittest.main()
