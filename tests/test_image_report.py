import unittest

from acg_daily.image_report import build_daily_image_html
from acg_daily.models import Article, DailyEdition, EditedItem


class ImageReportTests(unittest.TestCase):
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
        self.assertIn('class="lead-story"', html)
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
        self.assertIn("漫画", html)
        self.assertIn("ACG NEWS", html)


if __name__ == "__main__":
    unittest.main()
