from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.tool import FunctionTool, ToolSet

from .acg_daily.editor import (
    build_editor_prompt,
    configured_system_prompt,
    fallback_edition,
    parse_edition,
)
from .acg_daily.image_report import build_daily_image_html, normalize_cover_data_uri
from .acg_daily.models import Article, DailyEdition
from .acg_daily.ranking import Ranking, fetch_ranking
from .acg_daily.schedule import DailyPublishSettings, parse_daily_publish_settings
from .acg_daily.scraper import NewsScraper, SourceResult, deduplicate_articles, is_http_url


_SCHEDULER_POLL_SECONDS = 15


@dataclass
class CacheEntry:
    created_at: float
    images: list[str]


@dataclass(frozen=True)
class _ScheduledDailyEvent:
    """The session fields the existing generation path needs for a proactive send."""

    unified_msg_origin: str


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


class LimitedSearchTool(FunctionTool):
    """Delegate to AstrBot search while bounding name checks per digest."""

    def __init__(self, tool: FunctionTool, max_calls: int = 10) -> None:
        super().__init__(
            name=tool.name,
            description=tool.description,
            parameters=tool.parameters,
        )
        self._tool = tool
        self._max_calls = max_calls
        self._call_count = 0
        self.active = tool.active

    async def call(self, context, **kwargs):
        if self._call_count >= self._max_calls:
            logger.warning("ACG 日报：已阻止额外的联网搜索调用，本次日报最多允许名称核对 %d 次。", self._max_calls)
            return f"本次日报已经完成 {self._max_calls} 次联网名称核对，不能再次搜索。请基于候选资讯和已有搜索结果完成编辑。"
        self._call_count += 1
        return await self._tool.call(context, **kwargs)


class AcgDailyPlugin(Star):
    """Use /acg日报 to create an AI-edited ACG image digest."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._cache: dict[str, CacheEntry] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._daily_publish_task: asyncio.Task | None = None
        self._last_scheduled_publish_date: str | None = None
        self._last_scheduled_publish_keys: set[tuple[str, str]] = set()

    async def initialize(self):
        self._daily_publish_task = asyncio.create_task(self._run_daily_publish_scheduler())
        logger.info("ACG 日报：定时发布调度器已启动，将每 %d 秒检查一次配置。", _SCHEDULER_POLL_SECONDS)

    async def terminate(self):
        if self._daily_publish_task is None:
            return
        self._daily_publish_task.cancel()
        try:
            await self._daily_publish_task
        except asyncio.CancelledError:
            pass
        self._daily_publish_task = None
        logger.info("ACG 日报：定时发布调度器已停止。")

    @filter.command("acg日报", alias={"ACG日报"})
    async def acg_daily(self, event: AstrMessageEvent):
        """即时抓取已配置的 ACG 资讯源并发送清晰的分页图片日报。"""

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
                logger.info("ACG 日报：命中冷却缓存，直接返回 %s 的 %d 页日报。", session_key, len(cached))
                for image in cached:
                    yield event.image_result(image)
                return

            try:
                images = await self._create_daily_images(event, urls)
            except Exception as exc:
                logger.exception("ACG 日报：生成失败。")
                yield event.plain_result(f"生成 ACG 日报失败：{exc}")
                return

            self._cache[session_key] = CacheEntry(time.monotonic(), images)
            logger.info(
                "ACG 日报：文转图完成，已为 %s 生成 %d 页图片日报。",
                session_key,
                len(images),
            )
            logger.info("ACG 日报：正在向聊天平台发送图片日报。")
            for image in images:
                yield event.image_result(image)

    async def _run_daily_publish_scheduler(self) -> None:
        """Check the editable WebUI settings without retaining stale schedules."""

        previous_status: tuple[str, ...] | None = None
        while True:
            try:
                try:
                    settings = self._daily_publish_settings()
                except ValueError as exc:
                    status = ("invalid", str(exc))
                    if status != previous_status:
                        logger.warning("ACG 日报：定时发布配置无效，已暂停：%s", exc)
                        previous_status = status
                    await asyncio.sleep(_SCHEDULER_POLL_SECONDS)
                    continue
                status = self._scheduler_status(settings)
                if status != previous_status:
                    self._log_scheduler_status(settings)
                    previous_status = status
                if settings is not None:
                    now = settings.now()
                    publish_date = now.strftime("%Y-%m-%d")
                    if publish_date != self._last_scheduled_publish_date:
                        self._last_scheduled_publish_date = publish_date
                        self._last_scheduled_publish_keys.clear()
                    if settings.time.matches(now):
                        pending_targets = tuple(
                            target
                            for target in settings.targets
                            if (target, settings.time.text) not in self._last_scheduled_publish_keys
                        )
                        if pending_targets:
                            self._last_scheduled_publish_keys.update(
                                (target, settings.time.text) for target in pending_targets
                            )
                            await self._publish_scheduled_daily(settings, pending_targets)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ACG 日报：定时发布调度器检查异常，将在下一轮继续。")
            await asyncio.sleep(_SCHEDULER_POLL_SECONDS)

    def _daily_publish_settings(self) -> DailyPublishSettings | None:
        return parse_daily_publish_settings(self.config)

    @staticmethod
    def _scheduler_status(settings: DailyPublishSettings | None) -> tuple[str, ...]:
        if settings is None:
            return ("disabled",)
        timezone = getattr(settings.timezone, "key", "服务器本地时区")
        return ("enabled", settings.time.text, *settings.targets, timezone)

    def _log_scheduler_status(self, settings: DailyPublishSettings | None) -> None:
        if settings is None:
            logger.info("ACG 日报：定时发布未启用。")
            return
        now = settings.now()
        next_run = settings.time.next_run_after(now)
        timezone = getattr(settings.timezone, "key", "服务器本地时区")
        logger.info(
            "ACG 日报：定时发布已启用，每日 %s（%s）发送至 %d 个白名单群聊；下次触发：%s。",
            settings.time.text,
            timezone,
            len(settings.targets),
            next_run.strftime("%Y-%m-%d %H:%M %Z"),
        )

    async def _publish_scheduled_daily(
        self,
        settings: DailyPublishSettings,
        targets: tuple[str, ...],
    ) -> None:
        urls = self._source_urls()
        logger.info(
            "ACG 日报：定时发布触发，将发送至 %d 个白名单群聊，当前配置 %d 个资讯源。",
            len(targets),
            len(urls),
        )
        if not urls:
            logger.warning("ACG 日报：定时发布已跳过，未配置有效资讯源。")
            return

        for target in targets:
            await self._publish_scheduled_daily_to_group(target, urls)

    async def _publish_scheduled_daily_to_group(self, target: str, urls: list[str]) -> None:
        lock = self._session_locks.setdefault(target, asyncio.Lock())
        if lock.locked():
            logger.warning("ACG 日报：定时发布已跳过，白名单群聊 %s 正在生成另一份日报。", target)
            return

        async with lock:
            started_at = time.monotonic()
            try:
                images = await self._create_daily_images_for_session(target, urls)
                logger.info(
                    "ACG 日报：定时发布已生成 %d 页图片，开始发送至 %s。",
                    len(images),
                    target,
                )
                for page_index, image in enumerate(images, start=1):
                    message = MessageChain()
                    if image.startswith(("http://", "https://")):
                        message.url_image(image)
                    else:
                        message.file_image(image)
                    sent = await self.context.send_message(
                        target,
                        message,
                    )
                    if not sent:
                        raise RuntimeError("未找到与发布目标匹配的平台适配器")
                    logger.info("ACG 日报：定时发布已发送第 %d/%d 页。", page_index, len(images))
                logger.info(
                    "ACG 日报：定时发布完成，目标 %s，共 %d 页，耗时 %.1f 秒。",
                    target,
                    len(images),
                    time.monotonic() - started_at,
                )
            except Exception:
                logger.exception("ACG 日报：定时发布失败，白名单群聊 %s。", target)

    async def _create_daily_images_for_session(self, session_key: str, urls: list[str]) -> list[str]:
        event = _ScheduledDailyEvent(session_key)
        return await self._create_daily_images(event, urls)

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

    def _cached_image(self, session_key: str) -> list[str] | None:
        cooldown = max(0, int(self.config.get("cooldown_seconds", 60)))
        entry = self._cache.get(session_key)
        if not entry or cooldown == 0 or time.monotonic() - entry.created_at > cooldown:
            return None
        return entry.images

    async def _create_daily_images(
        self,
        event: AstrMessageEvent,
        urls: list[str],
    ) -> list[str]:
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
            return await self._render_daily_images(
                DailyEdition("本次未从已配置资讯源中提取到可用资讯。", []),
                [],
                {},
                results,
            )

        max_items = max(1, min(int(self.config.get("max_daily_items", 12)), 12))
        edition = await self._edit_with_current_model(event, articles, max_items)
        selected_ids = {item.article_id for item in edition.items}
        selected = [article for article in articles if article.id in selected_ids]
        raw_cover_images = await scraper.fetch_cover_images(selected)
        cover_images = await self._prepare_cover_images(raw_cover_images)
        logger.info(
            "ACG 日报：入选 %d 条资讯，成功下载并适配 %d 张封面，将开始生成图片。",
            len(selected),
            len(cover_images),
        )
        return await self._render_daily_images(edition, articles, cover_images, results)

    async def _edit_with_current_model(
        self,
        event: AstrMessageEvent,
        articles: list[Article],
        max_items: int,
    ) -> DailyEdition:
        try:
            provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            system_prompt = configured_system_prompt(self.config.get("editor_system_prompt"))
            search_tools = self._search_tools(event)
            if search_tools:
                logger.info(
                    "ACG 日报：使用模型 %s 编辑 %d 条候选资讯，已允许其在必要时进行最多 10 次联网名称核对。",
                    provider_id,
                    len(articles),
                )
                response = await self.context.tool_loop_agent(
                    event=event,
                    chat_provider_id=provider_id,
                    system_prompt=system_prompt,
                    prompt=build_editor_prompt(articles, max_items),
                    tools=search_tools,
                    max_steps=12,
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
                    system_prompt=system_prompt,
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
        return ToolSet([LimitedSearchTool(tool)])

    async def _prepare_cover_images(self, covers: dict[int, str]) -> dict[int, str]:
        """Adapt downloaded covers for Chromium before putting them in HTML."""

        if not covers:
            return {}

        normalized: dict[int, str] = {}
        for article_id, cover in covers.items():
            try:
                normalized[article_id] = await asyncio.to_thread(normalize_cover_data_uri, cover)
            except Exception as exc:
                logger.info("ACG 日报：封面格式适配失败，将使用分类视觉卡（资讯 #%d）：%s", article_id, exc)
        logger.info("ACG 日报：已将 %d/%d 张封面压缩为渲染兼容 JPEG。", len(normalized), len(covers))
        return normalized

    async def _render_daily_images(
        self,
        edition: DailyEdition,
        articles: list[Article],
        cover_images: dict[int, str],
        results: list[SourceResult],
    ) -> list[str]:
        success_count = sum(1 for result in results if result.articles)
        date_text = datetime.now().astimezone().strftime("%Y 年 %m 月 %d 日")
        source_status = f"本次抓取 {success_count}/{len(results)} 个来源，筛选 {len(edition.items)} 条资讯"
        items_per_page = 4
        pages = [edition.items[index:index + items_per_page] for index in range(0, len(edition.items), items_per_page)] or [[]]
        ranking = await self._fetch_daily_ranking()
        images: list[str] = []
        for page_index, page_items in enumerate(pages, start=1):
            page_edition = DailyEdition(edition.intro, page_items)
            html = build_daily_image_html(
                page_edition,
                articles,
                cover_images,
                date_text,
                source_status,
                page_number=page_index,
                page_count=len(pages),
                page_start=(page_index - 1) * items_per_page + 1,
                ranking=ranking if page_index == len(pages) else None,
            )
            logger.info(
                "ACG 日报：开始调用 AstrBot 文转图服务，渲染第 %d/%d 页（%d 条资讯，HTML %d 字符）。",
                page_index,
                len(pages),
                len(page_items),
                len(html),
            )
            try:
                image = await self.html_render(
                    html,
                    {},
                    options={"type": "png", "full_page": True, "animations": "disabled"},
                )
            except Exception as exc:
                logger.exception("ACG 日报：AstrBot 文转图服务渲染失败（第 %d 页）：%s", page_index, exc)
                raise RuntimeError("AstrBot 文转图服务渲染失败") from exc
            if not image:
                logger.error("ACG 日报：AstrBot 文转图服务未返回第 %d 页图片地址。", page_index)
                raise RuntimeError("AstrBot 文转图服务未返回图片地址")
            images.append(image)
        logger.info("ACG 日报：AstrBot 文转图服务渲染成功，共获得 %d 页图片。", len(images))
        return images

    async def _fetch_daily_ranking(self) -> Ranking | None:
        source_key = str(self.config.get("ranking_source", "disabled")).strip()
        if not source_key or source_key == "disabled":
            return None
        try:
            ranking = await fetch_ranking(
                source_key,
                int(self.config.get("request_timeout_seconds", 10)),
            )
        except Exception as exc:
            logger.warning("ACG 日报：排行榜「%s」抓取失败，已跳过榜单：%s", source_key, exc)
            return None
        if ranking is None:
            logger.warning("ACG 日报：未知的排行榜来源「%s」，已跳过榜单。", source_key)
            return None
        logger.info("ACG 日报：已获取排行榜「%s」，共 %d 条。", ranking.source, len(ranking.entries))
        return ranking
