import asyncio
import unittest

from acg_daily.models import Article
from acg_daily.scraper import (
    _feed_entries,
    _html_entries,
    canonical_url,
    deduplicate_articles,
    validate_source_url,
)


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example Feed</title>
<item><title>First Item</title><link>https://example.com/first?utm_source=test</link>
<description>First summary</description><pubDate>Mon, 20 Jul 2026 10:00:00 +0000</pubDate></item>
</channel></rss>"""

HTML = b"""<!doctype html><html><head><meta property="og:site_name" content="Example Site"></head>
<body><article><h2><a href="/news/one">First headline</a></h2><p>First summary</p>
<time datetime="2026-07-20T10:00:00+00:00">today</time></article>
<article><h2><a href="/news/two">Second headline</a></h2><p>Second summary</p></article>
</body></html>"""


class ScraperTests(unittest.TestCase):
    def test_rss_extracts_normalized_articles(self):
        name, articles = _feed_entries(RSS, "https://example.com/feed", 10)

        self.assertEqual(name, "Example Feed")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].url, "https://example.com/first")
        self.assertEqual(articles[0].summary, "First summary")

    def test_html_extracts_article_cards(self):
        name, articles = _html_entries(HTML, "https://example.com/news", 10)

        self.assertEqual(name, "Example Site")
        self.assertEqual([article.title for article in articles], ["First headline", "Second headline"])
        self.assertEqual(articles[0].url, "https://example.com/news/one")

    def test_deduplication_uses_url_and_title(self):
        articles = [
            Article(0, "A title!", "", "https://example.com/a", "One"),
            Article(0, "A title", "", "https://other.example/a", "Two"),
            Article(0, "B title", "", "https://example.com/a", "Three"),
        ]

        deduplicated = deduplicate_articles(articles, 10)

        self.assertEqual(len(deduplicated), 1)
        self.assertEqual(deduplicated[0].id, 1)

    def test_canonical_url_removes_tracking(self):
        self.assertEqual(
            canonical_url("https://example.com/a?utm_source=x&foo=y#section"),
            "https://example.com/a?foo=y",
        )

    def test_private_source_url_is_rejected(self):
        with self.assertRaises(ValueError):
            asyncio.run(validate_source_url("http://127.0.0.1/feed"))


if __name__ == "__main__":
    unittest.main()
