import json
import unittest
from pathlib import Path

from acg_daily.editor import (
    SYSTEM_PROMPT,
    build_editor_prompt,
    configured_system_prompt,
    parse_edition,
)
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
        self.assertIn("必须以简体中文为主体", SYSTEM_PROMPT)
        self.assertIn("不得以外文作品名作为标题主体", SYSTEM_PROMPT)
        self.assertIn("单次日报最多 10 次", SYSTEM_PROMPT)
        self.assertIn("综合 ACG 日报", SYSTEM_PROMPT)
        self.assertIn("排除硬件参数", SYSTEM_PROMPT)
        self.assertIn("Steam 促销", SYSTEM_PROMPT)
        self.assertIn("成人向或露骨题材", SYSTEM_PROMPT)
        self.assertIn("重大公开个人消息", SYSTEM_PROMPT)
        self.assertIn("覆盖至少 3 个来源", SYSTEM_PROMPT)
        self.assertIn("游戏取舍首先看二次元关联度", SYSTEM_PROMPT)
        self.assertIn("泛 MMO、泛黑暗奇幻 ARPG、普通独立游戏", SYSTEM_PROMPT)

    def test_configured_system_prompt_uses_nonempty_override(self):
        self.assertEqual(configured_system_prompt("  Custom policy  "), "Custom policy")
        self.assertEqual(configured_system_prompt("\n\t"), SYSTEM_PROMPT)
        self.assertEqual(configured_system_prompt(None), SYSTEM_PROMPT)

    def test_schema_default_matches_editor_prompt(self):
        schema_path = Path(__file__).parent.parent / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(schema["editor_system_prompt"]["type"], "text")
        self.assertEqual(schema["editor_system_prompt"]["default"], SYSTEM_PROMPT)

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
