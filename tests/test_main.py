import ast
import unittest
from pathlib import Path


class MainSourceTests(unittest.TestCase):
    def test_daily_item_limit_and_search_budget_are_twelve_and_ten(self):
        source = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        constants = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)]

        self.assertIn(12, constants)
        self.assertIn(10, constants)
        self.assertIn("max_steps=12", source)
        self.assertIn("最多 10 次联网名称核对", source)
        self.assertIn("_EDITOR_SLOW_WARNING_SECONDS = 120", source)
        self.assertIn("_await_editor_response", source)

    def test_scheduled_publish_uses_context_send_message_and_lifecycle_hooks(self):
        source = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")
        schedule_source = (Path(__file__).parent.parent / "acg_daily" / "schedule.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("async def initialize(self)", source)
        self.assertIn("async def terminate(self)", source)
        self.assertIn("parse_daily_publish_settings", source)
        self.assertIn("self.context.send_message", source)
        self.assertIn("_publish_scheduled_daily_to_group", source)
        self.assertIn("message.url_image(image)", source)
        self.assertIn("定时发布", source)
        self.assertIn("daily_publish_group_whitelist", schedule_source)
        self.assertIn("GroupMessage", schedule_source)

    def test_daily_report_renders_and_sends_one_long_image(self):
        source = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")

        self.assertIn("渲染单张长图", source)
        self.assertIn("return [image]", source)
        self.assertNotIn("items_per_page", source)

    def test_agent_format_failure_retries_without_tools_instead_of_sending_raw_candidates(self):
        source = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")

        self.assertIn("无工具重试", source)
        self.assertIn("但不会取消，将继续等待", source)
        self.assertIn("开始使用模型 %s 进行无工具重试", source)
        self.assertIn("编辑 Agent 已启动，正在等待模型首次响应", source)
        self.assertIn("Tavily 未参与", source)
        self.assertIn("已跳过未经翻译的原始候选", source)
        self.assertNotIn("fallback_edition(articles, max_items)", source)

    def test_optional_editor_provider_overrides_the_current_session_model(self):
        source = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")
        schema = (Path(__file__).parent.parent / "_conf_schema.json").read_text(encoding="utf-8")

        self.assertIn("configured_editor_provider", source)
        self.assertIn("使用配置指定的编辑模型", source)
        self.assertEqual(source.count("chat_provider_id=provider_id"), 3)
        self.assertIn('"editor_provider"', schema)
        self.assertIn('"_special": "select_provider"', schema)

    def test_ranking_titles_share_the_primary_editor_call_and_tool_budget(self):
        source = (Path(__file__).parent.parent / "main.py").read_text(encoding="utf-8")
        editor_source = (Path(__file__).parent.parent / "acg_daily" / "editor.py").read_text(encoding="utf-8")

        self.assertIn("_fetch_daily_ranking()", source)
        self.assertIn("_edit_with_current_model(event, articles, max_items, ranking)", source)
        self.assertIn("parse_edition_with_ranking", source)
        self.assertIn("排行榜标题翻译失败，已跳过榜单", source)
        self.assertIn("共用编辑模型中文化标题", source)
        self.assertIn("两者共用最多 10 次联网名称核对", source)
        self.assertIn("编辑模型已中文化", source)
        self.assertIn("新闻和排行榜作品名共用单次日报最多 10 次", editor_source)


if __name__ == "__main__":
    unittest.main()
