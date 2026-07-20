import asyncio
import unittest

from acg_daily.models import Article
from acg_daily.scraper import (
    _article_page_cover_url,
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

    def test_rss_removes_generic_news_prefix_from_source_name(self):
        rss = RSS.replace(b"Example Feed", b"News - MyAnimeList")

        name, _articles = _feed_entries(rss, "https://myanimelist.net/rss/news.xml", 10)

        self.assertEqual(name, "MyAnimeList")

    def test_rss_extracts_cdata_links(self):
        cdata_rss = b"""<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0"><channel><title>Bahamut GNN</title>
        <item><title><![CDATA[News title]]></title>
        <description><![CDATA[News summary]]></description>
        <link><![CDATA[https://gnn.gamer.com.tw/detail.php?sn=1]]></link>
        <pubDate>Mon, 20 Jul 2026 10:00:00 +0800</pubDate></item>
        </channel></rss>"""

        name, articles = _feed_entries(cdata_rss, "https://gnn.gamer.com.tw/rss.xml", 10)

        self.assertEqual(name, "Bahamut GNN")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].url, "https://gnn.gamer.com.tw/detail.php?sn=1")

    def test_rdf_rss_extracts_articles(self):
        rdf_rss = b"""<?xml version="1.0" encoding="utf-8"?>
        <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
          xmlns="http://purl.org/rss/1.0/">
          <channel rdf:about="https://example.com/"><title>Anime News</title></channel>
          <item rdf:about="https://example.com/news/one">
            <title>Anime announcement</title><link>https://example.com/news/one</link>
          </item>
        </rdf:RDF>"""

        name, articles = _feed_entries(rdf_rss, "https://example.com/feed.rdf", 10)

        self.assertEqual(name, "Anime News")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].title, "Anime announcement")

    def test_rdf_rss_matches_animeanime_structure(self):
        rdf_rss = b"""<?xml version="1.0" encoding="utf-8"?>
        <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
          xmlns:dc="http://purl.org/dc/elements/1.1/"
          xmlns="http://purl.org/rss/1.0/">
          <channel rdf:about="https://animeanime.jp/"><title>Anime Anime</title></channel>
          <item rdf:about="https://animeanime.jp/article/2026/07/20/1.html">
            <title>Anime announcement</title>
            <link>https://animeanime.jp/article/2026/07/20/1.html</link>
            <dc:date>2026-07-20T05:00:03Z</dc:date>
          </item>
        </rdf:RDF>"""

        name, articles = _feed_entries(rdf_rss, "https://animeanime.jp/rss/index.rdf", 10)

        self.assertEqual(name, "Anime Anime")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].published_at, "2026-07-20T05:00:03Z")

    def test_atom_enclosure_extracts_cover_image(self):
        atom = b"""<?xml version="1.0" encoding="utf-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom"><title>LN News</title>
          <entry><title>Anime news</title>
            <link rel="enclosure" type="image/jpeg" href="https://images.example/cover.jpg" />
            <link rel="alternate" href="https://example.com/news/one" />
          </entry>
        </feed>"""

        name, articles = _feed_entries(atom, "https://example.com/feed", 10)

        self.assertEqual(name, "LN News")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].cover_url, "https://images.example/cover.jpg")

    def test_article_page_cover_prefers_open_graph_image(self):
        page = b"""<!doctype html><html><head>
        <meta property="og:image" content="/images/hero.jpg">
        </head><body><article><img src="/images/body.jpg"></article></body></html>"""

        cover_url = _article_page_cover_url(page, "https://example.com/news/item")

        self.assertEqual(cover_url, "https://example.com/images/hero.jpg")

    def test_html_extracts_ann_cards_with_lazy_thumbnail(self):
        page = b"""<!doctype html><html><head><title>Anime News Network</title></head><body>
        <div class="mainfeed-day"><div class="herald box news">
          <div class="thumbnail lazyload" data-src="/thumbnails/news.jpg"></div>
          <div class="wrap"><h3><a href="/news/item">Anime announcement</a></h3>
          <time datetime="2026-07-20T04:05:36+00:00">today</time>
          <div class="snippet"><span class="hook">Official announcement details</span></div></div>
        </div></div></body></html>"""

        name, articles = _html_entries(page, "https://www.animenewsnetwork.com/", 10)

        self.assertEqual(name, "Anime News Network")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].cover_url, "https://www.animenewsnetwork.com/thumbnails/news.jpg")
        self.assertEqual(articles[0].summary, "Official announcement details")

    def test_html_extracts_myanimelist_news_cards(self):
        page = b"""<!doctype html><html><head><title>Anime &amp; Manga News - MyAnimeList.net</title></head>
        <body><div class="news-unit">
          <a class="image-link" href="https://myanimelist.net/news/1">
            <img src="https://cdn.example/cover-small.jpg"
                 srcset="https://cdn.example/cover-small.jpg 1x, https://cdn.example/cover-large.jpg 2x">
          </a>
          <div class="news-unit-right"><p class="title"><a href="https://myanimelist.net/news/1">Anime announcement</a></p>
          <div class="text">Official announcement details</div>
          <div class="information"><p class="info">Today, 3:40 AM</p></div></div>
        </div></body></html>"""

        name, articles = _html_entries(page, "https://myanimelist.net/news", 10)

        self.assertEqual(name, "MyAnimeList.net")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].cover_url, "https://cdn.example/cover-large.jpg")
        self.assertEqual(articles[0].summary, "Official announcement details")

    def test_html_extracts_chuapp_featured_cards(self):
        page = b"""<!doctype html><html><head><title>ChuApp</title></head><body>
        <section class="wrap"><div class="everyday"><div class="big">
          <a href="/article/1.html"><img src="https://img.example/feature.jpg"><h2>ACG game announcement</h2><span>07.20</span></a>
        </div></div></section></body></html>"""

        name, articles = _html_entries(page, "https://www.chuapp.com/", 10)

        self.assertEqual(name, "ChuApp")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].url, "https://www.chuapp.com/article/1.html")
        self.assertEqual(articles[0].cover_url, "https://img.example/feature.jpg")

    def test_html_extracts_heading_wrapped_by_link(self):
        page = b"""<!doctype html><html><body><article>
        <a href="/news/one"><h2>Wrapped headline</h2></a><p>Summary</p>
        </article></body></html>"""

        _name, articles = _html_entries(page, "https://example.com/", 10)

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].title, "Wrapped headline")
        self.assertEqual(articles[0].url, "https://example.com/news/one")

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

    def test_deduplication_interleaves_sources(self):
        articles = [
            Article(0, "First A", "", "https://a.example/1", "Source A", "2026-07-20T10:00:00Z"),
            Article(0, "Second A", "", "https://a.example/2", "Source A", "2026-07-20T09:00:00Z"),
            Article(0, "First B", "", "https://b.example/1", "Source B", "2026-07-20T08:00:00Z"),
            Article(0, "Second B", "", "https://b.example/2", "Source B", "2026-07-20T07:00:00Z"),
        ]

        deduplicated = deduplicate_articles(articles, 4)

        self.assertEqual(
            [article.source for article in deduplicated],
            ["Source A", "Source B", "Source A", "Source B"],
        )

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
