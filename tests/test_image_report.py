import base64
import io
import unittest

from PIL import Image

from acg_daily.image_report import build_daily_image_html, normalize_cover_data_uri
from acg_daily.models import Article, DailyEdition, EditedItem
from acg_daily.ranking import Ranking, RankingEntry


class ImageReportTests(unittest.TestCase):
    def test_normalize_cover_converts_transparent_png_to_bounded_rgb_jpeg(self):
        source = Image.new("RGBA", (1600, 1000), (255, 0, 0, 0))
        source.putpixel((800, 500), (0, 80, 255, 255))
        source_bytes = io.BytesIO()
        source.save(source_bytes, format="PNG")
        cover = "data:image/png;base64," + base64.b64encode(source_bytes.getvalue()).decode("ascii")

        normalized = normalize_cover_data_uri(cover)

        self.assertTrue(normalized.startswith("data:image/jpeg;base64,"))
        image_bytes = base64.b64decode(normalized.split(",", 1)[1], validate=True)
        with Image.open(io.BytesIO(image_bytes)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.mode, "RGB")
            self.assertLessEqual(image.width, 960)
            self.assertLessEqual(image.height, 720)

    def test_normalize_cover_rejects_invalid_data_uri(self):
        with self.assertRaises(ValueError):
            normalize_cover_data_uri("data:image/jpeg;base64,not valid base64")

    def test_report_uses_cover_and_escapes_editor_content(self):
        article = Article(
            1,
            "Original title",
            "",
            "https://example.com/private-link",
            "Example <Source>",
        )
        edition = DailyEdition(
            "<daily intro>",
            [
                EditedItem(
                    1,
                    "动画",
                    "A < B",
                    "Summary & detail",
                    "Worth <watching>",
                ),
            ],
        )

        html = build_daily_image_html(
            edition,
            [article],
            {1: "data:image/jpeg;base64,aGVsbG8="},
            "2026 年 7 月 20 日",
            "抓取状态",
        )

        self.assertIn('src="data:image/jpeg;base64,aGVsbG8="', html)
        self.assertIn('class="story-card feature with-cover accent-1"', html)
        self.assertIn("次元放送局", html)
        self.assertIn("Example &lt;Source&gt;", html)
        self.assertIn("A &lt; B", html)
        self.assertIn("Summary &amp; detail", html)
        self.assertNotIn("private-link", html)
        self.assertNotIn("https://example.com", html)

    def test_report_uses_placeholder_without_cover(self):
        edition = DailyEdition("", [EditedItem(1, "漫画", "标题", "摘要", "")])
        article = Article(1, "标题", "", "https://example.com", "来源")

        html = build_daily_image_html(edition, [article], {}, "日期", "状态")

        self.assertIn('class="cover-placeholder cover-placeholder-featured"', html)
        self.assertIn('class="story-card feature without-cover accent-1"', html)
        self.assertIn("漫画", html)
        self.assertIn("NO IMAGE / ACG DESK", html)

    def test_report_uses_placeholder_for_ann_article_without_cover(self):
        edition = DailyEdition("", [EditedItem(1, "游戏", "魔法少女的魔女审判原声带上线", "摘要", "")])
        article = Article(
            1,
            "Magical Girl Witch Trials Soundtrack",
            "",
            "https://www.animenewsnetwork.com/press-release/example",
            "Anime News Network",
        )

        html = build_daily_image_html(edition, [article], {}, "日期", "状态")

        self.assertIn('class="cover-placeholder cover-placeholder-featured"', html)
        self.assertNotIn('class="cover-image"', html)

    def test_report_uses_global_item_numbers_in_one_long_image(self):
        article = Article(5, "原标题", "", "https://example.com", "来源")
        edition = DailyEdition("导语", [EditedItem(5, "游戏", "标题", "摘要", "")])

        html = build_daily_image_html(
            edition,
            [article],
            {},
            "日期",
            "状态",
        )

        self.assertIn("DAILY ONE SHOT", html)
        self.assertIn("01", html)

    def test_normalize_cover_accepts_a_recoverable_truncated_jpeg(self):
        source = Image.new("RGB", (120, 80), "#22aab0")
        source_bytes = io.BytesIO()
        source.save(source_bytes, format="JPEG", quality=90)
        truncated = source_bytes.getvalue()[:-16]
        cover = "data:image/jpeg;base64," + base64.b64encode(truncated).decode("ascii")

        normalized = normalize_cover_data_uri(cover)

        self.assertTrue(normalized.startswith("data:image/jpeg;base64,"))

    def test_report_renders_an_optional_top_ten_ranking(self):
        ranking = Ranking(
            "热门动画榜 Top 10",
            "Anime Hack",
            (RankingEntry(1, "《测试动画》", "2026年夏动画"),),
        )

        html = build_daily_image_html(DailyEdition("", []), [], {}, "日期", "状态", ranking=ranking)

        self.assertIn('class="ranking-block"', html)
        self.assertIn("热门动画榜 Top 10", html)
        self.assertIn("Anime Hack", html)
        self.assertIn("《测试动画》", html)


if __name__ == "__main__":
    unittest.main()
