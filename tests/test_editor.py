import unittest

from acg_daily.editor import SYSTEM_PROMPT, build_editor_prompt, parse_edition
from acg_daily.models import Article


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.articles = [
            Article(
                1,
                "Original announcement",
                "A source summary.",
                "https://example.com/one",
                "Example News",
            ),
            Article(
                2,
                "Second announcement",
                "Another source summary.",
                "https://example.com/two",
                "Example News",
            ),
        ]

    def test_prompt_only_contains_source_fields(self):
        prompt = build_editor_prompt(self.articles, 5)

        self.assertIn('"id":1', prompt)
        self.assertIn('"source":"Example News"', prompt)
        self.assertNotIn("https://example.com/one", prompt)

    def test_editor_prompt_requires_chinese_title_handling(self):
        self.assertIn("优先采用候选中已有的简体中文标题", SYSTEM_PROMPT)
        self.assertIn("保留原文名", SYSTEM_PROMPT)
        self.assertIn("一次日报最多调用一次搜索工具", SYSTEM_PROMPT)
        self.assertIn("偏二次元社群的日报", SYSTEM_PROMPT)
        self.assertIn("排除硬件参数", SYSTEM_PROMPT)
        self.assertIn("覆盖至少 3 个来源", SYSTEM_PROMPT)

    def test_parse_edition_rejects_unknown_and_duplicate_ids(self):
        response = """```json
        {
          "intro": "今天有值得关注的动态。",
          "items": [
            {"article_id": 404, "category": "动画", "title": "无效", "summary": "无效", "reason": "无效"},
            {"article_id": 1, "category": "动画", "title": "中文标题", "summary": "这是一段足够清晰的摘要。", "reason": "官方动态"},
            {"article_id": 1, "category": "游戏", "title": "重复标题", "summary": "重复摘要", "reason": "重复"}
          ]
        }
        ```"""

        edition = parse_edition(response, self.articles, 5)

        self.assertEqual(edition.intro, "今天有值得关注的动态。")
        self.assertEqual(len(edition.items), 1)
        self.assertEqual(edition.items[0].article_id, 1)
        self.assertEqual(edition.items[0].title, "中文标题")

    def test_parse_edition_requires_valid_items_when_model_selected_any(self):
        response = '{"intro":"test","items":[{"article_id":99}]}'

        with self.assertRaises(ValueError):
            parse_edition(response, self.articles, 5)


if __name__ == "__main__":
    unittest.main()
