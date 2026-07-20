from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.tool import FunctionTool, ToolSet

from .acg_daily.editor import SYSTEM_PROMPT, build_editor_prompt, fallback_edition, parse_edition
from .acg_daily.image_report import build_daily_image_html
from .acg_daily.models import Article, DailyEdition
from .acg_daily.scraper import NewsScraper, SourceResult, deduplicate_articles, is_http_url


@dataclass
class CacheEntry:
    created_at: float
    image: str


class DailyEditorHooks(BaseAgentRunHooks):
    """Report the editor's limited tool use in the normal AstrBot log."""

    async def on_tool_start(self, _run_context, tool: FunctionTool, tool_args: dict | None) -> None:
        logger.info(
            "ACG 日报：编辑模型开始调用工具「%s」，参数：%s。",
            tool.name,
            tool_args or {},
        )

    async def on_tool_end(self, _run_context, tool: FunctionTool, _tool_args: dict | None, tool_result) -> None:
        result_count = len(tool_result.content) if tool_result and tool_result.content else 0
        logger.info(
            "ACG 日报：编辑模型完成工具「%s」调用，返回 %d 段结果。",
            tool.name,
            result_count,
        )


class SingleUseSearchTool(FunctionTool):
    """Delegate to one AstrBot search tool while enforcing one call per digest."""

    def __init__(self, tool: FunctionTool) -> None:
        super().__init__(
            name=tool.name,
            description=tool.description,
            parameters=tool.parameters,
        )
        self._tool = tool
        self._used = False
        self.active = tool.active

    async def call(self, context, **kwargs):
        if self._used:
            logger.warning("ACG 日报：已阻止额外的联网搜索调用，本次日报最多允许搜索一次。")
            return "本次日报已经使用过一次联网名称核对，不能再次搜索。请基于候选资讯和已有搜索结果完成编辑。"
        self._used = True
        return await self._tool.call(context, **kwargs)


class AcgDailyPlugin(Star):
    """Use /acg日报 to create an AI-edited ACG image digest."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._cache: dict[str, CacheEntry] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}

    @filter.command("acg日报", alias={"ACG日报"})
    async def acg_daily(self, event: AstrMessageEvent):
        """即时抓取已配置的 ACG 资讯源并发送单张图片日报。"""

        urls = self._source_urls()
        logger.info(
            "ACG 日报：收到来自 %s 的命令，当前配置了 %d 个资讯源。",
            event.unified_msg_origin,
            len(urls),
        )
        if not urls:
            logger.warning("ACG 日报：没有配置有效的资讯源链接。")
            yield event.plain_result("请先在插件配置的「资讯源链接」列表中至少添加一个 http(s) URL。")
            return

        session_key = event.unified_msg_origin
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            cached = self._cached_image(session_key)
            if cached is not None:
                logger.info("ACG 日报：命中冷却缓存，直接返回 %s 的上次日报。", session_key)
                yield event.image_result(cached)
                return

            try:
                image = await self._create_daily_image(event, urls)
            except Exception as exc:
                logger.exception("ACG 日报：生成失败。")
                yield event.plain_result(f"生成 ACG 日报失败：{exc}")
                return

            self._cache[session_key] = CacheEntry(time.monotonic(), image)
            logger.info(
                "ACG 日报：已为 %s 生成单张图片日报。",
                session_key,
            )
            yield event.image_result(image)

    def _source_urls(self) -> list[str]:
        configured = self.config.get("news_source_urls", [])
        if not isinstance(configured, list):
            return []
        urls: list[str] = []
        for value in configured:
            if not isinstance(value, str):
                continue
            url = value.strip()
            if url and is_http_url(url) and url not in urls:
                urls.append(url)
        return urls[:20]

    def _cached_image(self, session_key: str) -> str | None:
        cooldown = max(0, int(self.config.get("cooldown_seconds", 60)))
        entry = self._cache.get(session_key)
        if not entry or cooldown == 0 or time.monotonic() - entry.created_at > cooldown:
            return None
        return entry.image

    async def _create_daily_image(
        self,
        event: AstrMessageEvent,
        urls: list[str],
    ) -> str:
        scraper = NewsScraper(
            timeout_seconds=int(self.config.get("request_timeout_seconds", 10)),
            max_articles_per_source=int(self.config.get("max_articles_per_source", 10)),
        )
        results = await scraper.collect(urls)
        articles = deduplicate_articles(
            [article for result in results for article in result.articles],
            max_candidates=max(1, min(int(self.config.get("max_candidates", 40)), 80)),
        )
        logger.info(
            "ACG 日报：从 %d/%d 个可用资讯源获得 %d 条原始资讯，去重后剩余 %d 条候选。",
            sum(1 for result in results if result.articles),
            len(results),
            sum(len(result.articles) for result in results),
            len(articles),
        )
        if not articles:
            logger.warning("ACG 日报：没有提取到可用资讯。")
            return await self._render_daily_image(
                DailyEdition("本次未从已配置资讯源中提取到可用资讯。", []),
                [],
                {},
                results,
            )

        max_items = max(1, min(int(self.config.get("max_daily_items", 8)), 8))
        edition = await self._edit_with_current_model(event, articles, max_items)
        selected_ids = {item.article_id for item in edition.items}
        selected = [article for article in articles if article.id in selected_ids]
        cover_images = await scraper.fetch_cover_images(selected)
        return await self._render_daily_image(edition, articles, cover_images, results)

    async def _edit_with_current_model(
        self,
        event: AstrMessageEvent,
        articles: list[Article],
        max_items: int,
    ) -> DailyEdition:
        try:
            provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            search_tools = self._search_tools(event)
            if search_tools:
                logger.info(
                    "ACG 日报：使用模型 %s 编辑 %d 条候选资讯，已允许其在必要时进行一次联网名称核对。",
                    provider_id,
                    len(articles),
                )
                response = await self.context.tool_loop_agent(
                    event=event,
                    chat_provider_id=provider_id,
                    system_prompt=SYSTEM_PROMPT,
                    prompt=build_editor_prompt(articles, max_items),
                    tools=search_tools,
                    max_steps=2,
                    tool_call_timeout=30,
                    agent_hooks=DailyEditorHooks(),
                )
            else:
                logger.info(
                    "ACG 日报：使用模型 %s 编辑 %d 条候选资讯，不使用联网搜索。",
                    provider_id,
                    len(articles),
                )
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=SYSTEM_PROMPT,
                    prompt=build_editor_prompt(articles, max_items),
                )
            edition = parse_edition(response.completion_text, articles, max_items)
            logger.info(
                "ACG 日报：编辑模型返回 %d 条有效入选资讯。",
                len(edition.items),
            )
            if edition.items:
                return edition
            return DailyEdition(edition.intro or "本次候选资讯中没有适合收录的内容。", [])
        except Exception as exc:
            logger.warning("ACG 日报：编辑模型不可用，改为按来源顺序输出：%s", exc)
            return fallback_edition(articles, max_items)

    def _search_tools(self, event: AstrMessageEvent) -> ToolSet | None:
        if not self.config.get("enable_web_search", False):
            return None

        provider_settings = self.context.get_config(event.unified_msg_origin).get(
            "provider_settings",
            {},
        )
        if not provider_settings.get("web_search", False):
            logger.warning("ACG 日报：插件已允许联网核对，但 AstrBot 全局网页搜索未启用。")
            return None

        search_tool_names = {
            "tavily": "web_search_tavily",
            "bocha": "web_search_bocha",
            "brave": "web_search_brave",
            "firecrawl": "web_search_firecrawl",
            "baidu_ai_search": "web_search_baidu",
            "exa": "web_search_exa",
        }
        provider_name = str(provider_settings.get("websearch_provider", "tavily"))
        tool_name = search_tool_names.get(provider_name)
        if not tool_name:
            logger.warning("ACG 日报：不支持的全局网页搜索提供商「%s」。", provider_name)
            return None

        try:
            tool = self.context.get_llm_tool_manager().get_builtin_tool(tool_name)
        except (KeyError, TypeError) as exc:
            logger.warning("ACG 日报：无法加载网页搜索工具「%s」：%s", tool_name, exc)
            return None
        logger.info("ACG 日报：已为编辑模型注入网页搜索工具「%s」。", tool_name)
        return ToolSet([SingleUseSearchTool(tool)])

    async def _render_daily_image(
        self,
        edition: DailyEdition,
        articles: list[Article],
        cover_images: dict[int, str],
        results: list[SourceResult],
    ) -> str:
        success_count = sum(1 for result in results if result.articles)
        date_text = datetime.now().astimezone().strftime("%Y 年 %m 月 %d 日")
        source_status = f"本次抓取 {success_count}/{len(results)} 个来源，筛选 {len(edition.items)} 条资讯"
        html = build_daily_image_html(
            edition,
            articles,
            cover_images,
            date_text,
            source_status,
        )
        logger.info("ACG 日报：正在将 %d 条资讯渲染为单张图片。", len(edition.items))
        return await self.html_render(
            html,
            {},
            options={"type": "jpeg", "quality": 88, "full_page": True, "animations": "disabled"},
        )
