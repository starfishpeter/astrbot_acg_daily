import json
import unittest
from pathlib import Path

from acg_daily.editor import (
    SYSTEM_PROMPT,
    editor_response_diagnosis,
    build_editor_retry_prompt,
    build_editor_prompt,
    configured_editor_provider,
    configured_system_prompt,
    parse_edition,
    parse_edition_with_ranking,
)
from acg_daily.models import Article
from acg_daily.ranking import Ranking, RankingEntry


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

    def test_prompt_only_contains_fields_used_for_editorial_selection(self):
        prompt = build_editor_prompt(self.articles, 5)

        self.assertIn('"id":1', prompt)
        self.assertIn('"title":"Original announcement"', prompt)
        self.assertIn('"summary":"A source summary."', prompt)
        self.assertNotIn('"source"', prompt)
        self.assertNotIn("https://example.com/one", prompt)

    def test_editor_prompt_uses_a_core_acg_group_perspective(self):
        self.assertIn("中国大陆 QQ 核心二次元群", SYSTEM_PROMPT)
        self.assertIn("番剧、漫画和轻小说相关资讯最优先", SYSTEM_PROMPT)
        self.assertIn("二次元游戏相关报道", SYSTEM_PROMPT)
        self.assertIn("自然简洁的简体中文", SYSTEM_PROMPT)
        self.assertIn("只可调用一次“批量译名核对”工具", SYSTEM_PROMPT)
        self.assertIn("联网只用于核对译名", SYSTEM_PROMPT)
        self.assertIn("只可调用一次“批量译名核对”工具", SYSTEM_PROMPT)
        self.assertIn("ranking_items", SYSTEM_PROMPT)
        self.assertIn("Fate、VTuber", SYSTEM_PROMPT)
        self.assertIn("不得补充候选没有提供的事实", SYSTEM_PROMPT)
        self.assertIn("无论是否调用过工具", SYSTEM_PROMPT)
        self.assertIn("items 是始终必填的数组", SYSTEM_PROMPT)
        self.assertIn('"items":[]', SYSTEM_PROMPT)

    def test_prompt_bounds_candidate_summaries_and_omits_unused_metadata(self):
        article = Article(1, "Title", "x" * 500, "https://example.com", "Example News", "2026-07-21")

        prompt = build_editor_prompt([article], 1)

        self.assertIn('"summary":"' + "x" * 160 + '"', prompt)
        self.assertNotIn("Example News", prompt)
        self.assertNotIn("2026-07-21", prompt)

    def test_configured_system_prompt_uses_nonempty_override(self):
        self.assertEqual(configured_system_prompt("  Custom policy  "), "Custom policy")
        self.assertEqual(configured_system_prompt("\n\t"), SYSTEM_PROMPT)
        self.assertEqual(configured_system_prompt(None), SYSTEM_PROMPT)

    def test_retry_prompt_keeps_completed_title_lookups_and_requires_items(self):
        prompt = build_editor_retry_prompt("候选资讯", "Bangumi 词条候选\n- 原名 -> 中文名")

        self.assertIn("候选资讯", prompt)
        self.assertIn("Bangumi 词条候选", prompt)
        self.assertIn("包含 items", prompt)
        self.assertIn("不要再次调用工具", prompt)
        self.assertIn("完整 ranking_items 数组", prompt)

    def test_response_diagnosis_includes_full_completion_and_json_shape(self):
        completion = '{"intro":"only intro","ranking_items":[]}'

        diagnosis = editor_response_diagnosis(completion)

        self.assertIn(f"completion 字符数：{len(completion)}", diagnosis)
        self.assertIn("JSON 顶层字段：intro, ranking_items", diagnosis)
        self.assertIn("items=缺失", diagnosis)
        self.assertIn(completion, diagnosis)

    def test_configured_editor_provider_uses_an_optional_fixed_provider(self):
        self.assertEqual(configured_editor_provider("  deepseek/deepseek-v4-pro  "), "deepseek/deepseek-v4-pro")
        self.assertIsNone(configured_editor_provider("\n\t"))
        self.assertIsNone(configured_editor_provider(None))

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

    def test_editor_response_translates_ranking_with_the_news_in_one_json_object(self):
        ranking = Ranking(
            "测试榜单",
            "测试来源",
            (RankingEntry(1, "Original One", "TV · 2026"), RankingEntry(2, "Original Two", "TV · 2025")),
        )
        prompt = build_editor_prompt(self.articles, 5, ranking)
        edition, translated, error = parse_edition_with_ranking(
            '{"intro":"导语","items":[{"article_id":1,"category":"动画","title":"标题","summary":"新闻摘要","reason":"新消息"}],"ranking_items":[{"rank":1,"title":"作品一"},{"rank":2,"title":"作品二"}]}',
            self.articles,
            5,
            ranking,
        )

        self.assertIn("排行榜 JSON", prompt)
        self.assertIn('"rank":1', prompt)
        self.assertEqual(edition.items[0].title, "标题")
        self.assertEqual(error, "")
        self.assertEqual([entry.title for entry in translated.entries], ["作品一", "作品二"])


if __name__ == "__main__":
    unittest.main()
