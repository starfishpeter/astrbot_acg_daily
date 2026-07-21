import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from acg_daily.models import Article
from acg_daily.scraper import (
    _read_limited_body,
    _article_page_cover_url,
    _feed_entries,
    _html_entries,
    canonical_url,
    deduplicate_articles,
    format_source_diagnostics,
    validate_source_url,
    NewsScraper,
    SourceResult,
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
    def test_limited_body_reader_reads_all_chunks_until_eof(self):
        class Content:
            def __init__(self):
                self.chunks = [b"first", b"-second", b""]

            async def read(self, _limit):
                return self.chunks.pop(0)

        body = asyncio.run(_read_limited_body(Content(), 20, "response"))

        self.assertEqual(body, b"first-second")

    def test_rss_extracts_normalized_articles(self):
        name, articles = _feed_entries(RSS, "https://example.com/feed", 10)

        self.assertEqual(name, "Example Feed")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].url, "https://example.com/first")
        self.assertEqual(articles[0].summary, "First summary")

    def test_source_diagnostics_summarizes_entries_covers_and_failures_without_article_urls(self):
        article = Article(1, "Title", "", "https://example.com/news/one", "Example Feed")
        results = [
            SourceResult("https://example.com/feed", "Example Feed", [article]),
            SourceResult("https://broken.example/news", "https://broken.example/news", [], "HTTP 403"),
        ]

        text = format_source_diagnostics(results, {"https://example.com/feed": 1}, 1)

        self.assertIn("配置 2 个来源；可用 1 个；原始资讯 1 条；去重候选 1 条。", text)
        self.assertIn("Example Feed：资讯 1 条；封面可下载 1/1。", text)
        self.assertIn("broken.example：抓取失败（HTTP 403）", text)
        self.assertNotIn("https://example.com/news/one", text)

    def test_rss_removes_generic_news_prefix_from_source_name(self):
        rss = RSS.replace(b"Example Feed", b"News - Example Publisher")

        name, _articles = _feed_entries(rss, "https://example.com/feed", 10)

        self.assertEqual(name, "Example Publisher")

    def test_rss_extracts_cdata_links(self):
        cdata_rss = b"""<?xml version="1.0" encoding="utf-8"?>
          <rss version="2.0"><channel><title>Example Publisher</title>
        <item><title><![CDATA[News title]]></title>
        <description><![CDATA[News summary]]></description>
          <link><![CDATA[https://example.com/detail?sn=1]]></link>
        <pubDate>Mon, 20 Jul 2026 10:00:00 +0800</pubDate></item>
        </channel></rss>"""

        name, articles = _feed_entries(cdata_rss, "https://example.com/feed", 10)

        self.assertEqual(name, "Example Publisher")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].url, "https://example.com/detail?sn=1")

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

    def test_rdf_rss_extracts_publication_date(self):
        rdf_rss = b"""<?xml version="1.0" encoding="utf-8"?>
        <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
          xmlns:dc="http://purl.org/dc/elements/1.1/"
          xmlns="http://purl.org/rss/1.0/">
          <channel rdf:about="https://example.com/"><title>Example News</title></channel>
          <item rdf:about="https://example.com/article/2026/07/20/1.html">
            <title>Anime announcement</title>
            <link>https://example.com/article/2026/07/20/1.html</link>
            <dc:date>2026-07-20T05:00:03Z</dc:date>
          </item>
        </rdf:RDF>"""

        name, articles = _feed_entries(rdf_rss, "https://example.com/feed.rdf", 10)

        self.assertEqual(name, "Example News")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].published_at, "2026-07-20T05:00:03Z")

    def test_atom_enclosure_extracts_cover_image(self):
        atom = b"""<?xml version="1.0" encoding="utf-8"?>
          <feed xmlns="http://www.w3.org/2005/Atom"><title>Example Feed</title>
          <entry><title>Anime news</title>
            <link rel="enclosure" type="image/jpeg" href="https://images.example/cover.jpg" />
            <link rel="alternate" href="https://example.com/news/one" />
          </entry>
        </feed>"""

        name, articles = _feed_entries(atom, "https://example.com/feed", 10)

        self.assertEqual(name, "Example Feed")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].cover_url, "https://images.example/cover.jpg")

    def test_article_page_cover_prefers_open_graph_image(self):
        page = b"""<!doctype html><html><head>
        <meta property="og:image" content="/images/hero.jpg">
        </head><body><article><img src="/images/body.jpg"></article></body></html>"""

        cover_url = _article_page_cover_url(page, "https://example.com/news/item")

        self.assertEqual(cover_url, "https://example.com/images/hero.jpg")

    def test_article_page_cover_reads_post_entry_lazy_image(self):
        page = b"""<!doctype html><html><body>
        <div class="post-entry"><img src="/images/small.jpg"
          data-srcset="/images/small.jpg 300w, /images/large.jpg 1200w"></div>
        </body></html>"""

        cover_url = _article_page_cover_url(page, "https://example.com/news/item")

        self.assertEqual(cover_url, "https://example.com/images/large.jpg")

    def test_article_page_cover_uses_figure_before_non_content_images(self):
        page = b"""<!doctype html><html><body>
        <div class="advertisement"><img src="https://ads.example/banner.jpg"></div>
        <div id="content-zone"><figure><img src="/images/cover.jpg"></figure></div>
        </body></html>"""

        cover_url = _article_page_cover_url(page, "https://example.com/news/item")

        self.assertEqual(cover_url, "https://example.com/images/cover.jpg")

    def test_article_page_cover_uses_main_fallback_without_advertising(self):
        page = b"""<!doctype html><html><body><main>
        <div class="advertisement"><img src="https://ads.example/banner.jpg"></div>
        <div id="publisher-content"><ul class="image-grid">
          <li><img data-src="/images/cover.jpg"></li>
        </ul></div>
        </main></body></html>"""

        cover_url = _article_page_cover_url(page, "https://example.com/news/item")

        self.assertEqual(cover_url, "https://example.com/images/cover.jpg")

    def test_article_page_cover_accepts_gnn_webp_and_png_urls(self):
        for image_name in ("cover.WEBP", "cover.PNG"):
            with self.subTest(image_name=image_name):
                page = (
                    '<!doctype html><html><head><meta property="og:image" content="'
                    f'https://p2.bahamut.com.tw/B/2KU/01/{image_name}"></head></html>'
                ).encode()

                cover_url = _article_page_cover_url(
                    page,
                    "https://gnn.gamer.com.tw/detail.php?sn=308461",
                )

                self.assertEqual(
                    cover_url,
                    f"https://p2.bahamut.com.tw/B/2KU/01/{image_name}",
                )

    def test_article_page_cover_returns_empty_for_ann_page_without_images(self):
        page = b"""<!doctype html><html><body><main><article>
        <h1>Magical Girl Witch Trials Soundtrack</h1><p>Streaming announcement.</p>
        </article></main></body></html>"""

        cover_url = _article_page_cover_url(
            page,
            "https://www.animenewsnetwork.com/press-release/example",
        )

        self.assertEqual(cover_url, "")

    def test_fetch_cover_image_accepts_gnn_webp_and_png_responses(self):
        class Content:
            def __init__(self):
                self.pending = b"cover"

            async def read(self, _limit):
                chunk, self.pending = self.pending, b""
                return chunk

        class Response:
            status = 200
            content_length = 5

            def __init__(self, media_type):
                self.headers = {"Content-Type": media_type}
                self.content = Content()

        class Request:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, *_args):
                return None

        class Session:
            def __init__(self, response):
                self.response = response

            def get(self, *_args, **_kwargs):
                return Request(self.response)

        scraper = NewsScraper(timeout_seconds=10, max_articles_per_source=10)
        url = "https://p2.bahamut.com.tw/B/2KU/01/cover"
        for media_type in ("image/webp", "image/png"):
            with self.subTest(media_type=media_type), patch(
                "acg_daily.scraper.validate_source_url",
                new=AsyncMock(),
            ):
                cover = asyncio.run(
                    scraper._fetch_cover_image(Session(Response(media_type)), url),
                )

            self.assertEqual(cover, f"data:{media_type};base64,Y292ZXI=")

    def test_detail_page_request_retries_before_fetching_gnn_cover(self):
        article = Article(
            1,
            "GNN article",
            "",
            "https://gnn.gamer.com.tw/detail.php?sn=308461",
            "GNN",
        )
        page = b"""<meta property="og:image" content="https://p2.bahamut.com.tw/cover.WEBP">"""
        scraper = NewsScraper(timeout_seconds=10, max_articles_per_source=10)
        session = object()

        with (
            patch(
                "acg_daily.scraper._read_source_response",
                new=AsyncMock(side_effect=[ValueError("HTTP 403"), (article.url, page, "text/html")]),
            ) as read_response,
            patch.object(
                scraper,
                "_fetch_cover_image",
                new=AsyncMock(return_value="data:image/webp;base64,dGVzdA=="),
            ) as fetch_cover,
            patch("acg_daily.scraper.asyncio.sleep", new=AsyncMock()),
        ):
            cover, is_detail_cover = asyncio.run(scraper._fetch_article_cover_image(session, article))

        self.assertEqual(read_response.await_count, 2)
        fetch_cover.assert_awaited_once_with(session, "https://p2.bahamut.com.tw/cover.WEBP")
        self.assertEqual(cover, "data:image/webp;base64,dGVzdA==")
        self.assertTrue(is_detail_cover)

    def test_html_extracts_visual_cards_with_class_based_headings(self):
        page = b"""<!doctype html><html><head><title>Example Site</title></head><body>
        <div class="story-card"><a href="/news/one">
          <img src="https://img.example/cover.jpg"><div class="story-heading">Anime announcement</div>
        </a></div></body></html>"""

        name, articles = _html_entries(page, "https://example.com/", 10)

        self.assertEqual(name, "Example Site")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].title, "Anime announcement")
        self.assertEqual(articles[0].url, "https://example.com/news/one")
        self.assertEqual(articles[0].cover_url, "https://img.example/cover.jpg")

    def test_html_extracts_headline_card_date_and_cover(self):
        page = b"""<!doctype html><html><head><title>Example Site</title></head><body>
        <ul class="headline-list"><li class="headline-item"><a href="/news/one">
          <div class="headline-image"><img src="https://img.example/cover.jpg"></div>
          <div class="headline-meta"><div class="headline-text">Anime announcement</div>
          <div class="headline-date">2026-07-20 18:00</div></div>
        </a></li></ul></body></html>"""

        _name, articles = _html_entries(page, "https://example.com/", 10)

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].published_at, "2026-07-20 18:00")
        self.assertEqual(articles[0].cover_url, "https://img.example/cover.jpg")

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

    def test_non_global_carrier_grade_nat_source_url_is_rejected(self):
        with self.assertRaises(ValueError):
            asyncio.run(validate_source_url("http://100.64.0.1/feed"))


if __name__ == "__main__":
    unittest.main()
