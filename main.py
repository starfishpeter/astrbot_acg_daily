from __future__ import annotations

import asyncio
from contextlib import suppress
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
    configured_editor_provider,
    configured_system_prompt,
    parse_edition,
    parse_edition_with_ranking,
)
from .acg_daily.image_report import build_daily_image_html, normalize_cover_data_uri
from .acg_daily.models import Article, DailyEdition
from .acg_daily.ranking import Ranking, fetch_ranking
from .acg_daily.schedule import DailyPublishSettings, parse_daily_publish_settings
from .acg_daily.scraper import NewsScraper, SourceResult, deduplicate_articles, is_http_url
from .acg_daily.title_lookup import (
    MAX_BAIDU_LOOKUP_QUERY_CHARS,
    MAX_LOOKUP_GROUPS,
    compact_lookup_result,
    lookup_search_arguments,
    lookup_title_groups,
    normalized_lookup_titles,
    run_lookup_groups,
)


_SCHEDULER_POLL_SECONDS = 15
_DEFAULT_EDITOR_SLOW_WARNING_SECONDS = 600


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

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.tool_call_count = 0

    async def on_agent_begin(self, _run_context) -> None:
        logger.info("ACG 日报：编辑 Agent 已启动，正在等待模型首次响应。")

    async def on_tool_start(self, _run_context, tool: FunctionTool, tool_args: dict | None) -> None:
        self.tool_call_count += 1
        logger.info(
            "ACG 日报：编辑模型开始第 %d 次工具调用「%s」，参数：%s。",
            self.tool_call_count,
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

    async def on_agent_done(self, _run_context, _llm_response) -> None:
        logger.info("ACG 日报：编辑 Agent 已完成，本次实际调用 %d 次联网工具。", self.tool_call_count)

    def timeout_diagnosis(self) -> str:
        elapsed = time.monotonic() - self.started_at
        if self.tool_call_count == 0:
            return (
                f"Agent 已运行 {elapsed:.1f} 秒但未完成，未进入任何联网工具调用；"
                "Tavily 未参与。当前只能定位到 Agent 预处理或模型首次响应等待阶段，"
                "请结合 AstrBot Core 的模型提供商请求日志判断网络连接或上游模型响应情况。"
            )
        return (
            f"Agent 已运行 {elapsed:.1f} 秒但未完成，已执行 {self.tool_call_count} 次联网工具调用；"
            "超时发生在后续模型响应或 Agent 收尾阶段。"
        )


class BatchTitleLookupTool(FunctionTool):
    """Perform a few compact title searches in one model-visible tool action."""

    def __init__(self, tool: FunctionTool) -> None:
        super().__init__(
            name="batch_title_lookup",
            description=(
                "一次性核对多个动画、漫画、轻小说、游戏作品的中国大陆常用中文译名。"
                "仅在确实不确定译名时调用一次；titles 中列出全部需要核对的外文作品名，"
                "不要逐条使用其他搜索工具。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "titles": {
                        "type": "array",
                        "description": "全部需要核对中文译名的外文作品名。",
                        "items": {"type": "string"},
                    }
                },
                "required": ["titles"],
            },
        )
        self._tool = tool
        self._called = False
        self.active = tool.active

    async def call(self, context, **kwargs):
        if self._called:
            logger.warning("ACG 日报：已阻止重复的批量译名核对，请基于已有结果完成编辑。")
            return "本次日报已经完成一次批量译名核对，不能再次搜索。请基于候选资讯和已有搜索结果完成编辑。"
        self._called = True
        baidu_search = self._tool.name == "web_search_baidu"
        titles = normalized_lookup_titles(
            kwargs.get("titles"),
            MAX_BAIDU_LOOKUP_QUERY_CHARS if baidu_search else 120,
        )
        if not titles:
            return "没有收到可核对的作品名。请直接完成日报编辑。"

        groups = lookup_title_groups(
            titles,
            MAX_BAIDU_LOOKUP_QUERY_CHARS if baidu_search else 420,
        )
        logger.info(
            "ACG 日报：批量译名核对收到 %d 个作品名，将以 %d 个并发搜索组完成。",
            len(titles),
            len(groups),
        )
        searched_title_count = sum(len(group) for group in groups)
        if searched_title_count < len(titles):
            logger.warning(
                "ACG 日报：批量译名核对最多允许 %d 个搜索组，将跳过 %d 个较低优先级标题。",
                MAX_LOOKUP_GROUPS,
                len(titles) - searched_title_count,
        )
        async def search_group(group: list[str]) -> object:
            return await self._tool.call(
                context,
                **lookup_search_arguments(group, self._tool.name, self._tool.parameters),
            )

        results = await run_lookup_groups(groups, search_group)
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("ACG 日报：批量译名核对的一个搜索组失败：%s", result)
        compacted = [compact_lookup_result(result) for result in results]
        usable = [result for result in compacted if result]
        logger.info(
            "ACG 日报：批量译名核对完成，%d/%d 个搜索组返回可用结果。",
            len(usable),
            len(groups),
        )
        if not usable:
            return "批量译名核对未获得可用结果。请使用已有知识完成翻译，不要再次搜索。"
        return "批量译名核对结果（仅用于作品名翻译，请勿据此补充新闻事实）：\n" + "\n\n".join(usable)

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
        self._accept_results = True

    async def initialize(self):
        self._daily_publish_task = asyncio.create_task(self._run_daily_publish_scheduler())
        logger.info("ACG 日报：定时发布调度器已启动，将每 %d 秒检查一次配置。", _SCHEDULER_POLL_SECONDS)

    async def terminate(self):
        self._accept_results = False
        if self._daily_publish_task is not None:
            self._daily_publish_task.cancel()
            try:
                await self._daily_publish_task
            except asyncio.CancelledError:
                pass
            self._daily_publish_task = None
        logger.info("ACG 日报：插件实例已停止，进行中的旧任务完成后不会再发送结果。")

    @filter.command("acg日报", alias={"ACG日报"})
    async def acg_daily(self, event: AstrMessageEvent):
        """即时抓取已配置的 ACG 资讯源并发送单张长图日报。"""

        if not self._can_send_result("命令执行"):
            return
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
                if not self._can_send_result("缓存日报"):
                    return
                logger.info("ACG 日报：命中冷却缓存，直接返回 %s 的单张长图日报。", session_key)
                for image in cached:
                    yield event.image_result(image)
                return

            try:
                images = await self._create_daily_images(event, urls)
            except Exception as exc:
                logger.exception("ACG 日报：生成失败。")
                yield event.plain_result(f"生成 ACG 日报失败：{exc}")
                return

            if not self._can_send_result("已生成的日报"):
                return
            self._cache[session_key] = CacheEntry(time.monotonic(), images)
            logger.info(
                "ACG 日报：文转图完成，已为 %s 生成单张长图日报。",
                session_key,
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
                if not self._can_send_result("定时日报"):
                    return
                logger.info(
                    "ACG 日报：定时发布已生成单张长图，开始发送至 %s。",
                    target,
                )
                for image in images:
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
                    logger.info("ACG 日报：定时发布已发送单张长图。")
                logger.info(
                    "ACG 日报：定时发布完成，目标 %s，耗时 %.1f 秒。",
                    target,
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

    def _can_send_result(self, result_kind: str) -> bool:
        if self._accept_results:
            return True
        logger.warning(
            "ACG 日报：插件实例已重载或卸载，丢弃%s，避免旧任务重复发送。",
            result_kind,
        )
        return False

    async def _await_editor_response(
        self,
        operation,
        stage: str,
        agent_hooks: DailyEditorHooks | None = None,
    ):
        """Log a slow model request without cancelling a long daily edit."""

        warning_seconds = self._editor_slow_warning_seconds()
        task = asyncio.ensure_future(operation)
        if warning_seconds == 0:
            return await task
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=warning_seconds,
            )
        except asyncio.TimeoutError:
            diagnosis = agent_hooks.timeout_diagnosis() if agent_hooks else "尚未收到模型响应。"
            logger.warning(
                "ACG 日报：%s已等待超过 %d 秒，但不会取消，将继续等待。诊断：%s",
                stage,
                warning_seconds,
                diagnosis,
            )
            return await task
        except asyncio.CancelledError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise

    def _editor_slow_warning_seconds(self) -> int:
        """Read the optional diagnostic threshold without limiting model execution."""

        try:
            return max(0, int(self.config.get("editor_slow_warning_seconds", _DEFAULT_EDITOR_SLOW_WARNING_SECONDS)))
        except (TypeError, ValueError):
            return _DEFAULT_EDITOR_SLOW_WARNING_SECONDS

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
                None,
            )

        max_items = max(1, min(int(self.config.get("max_daily_items", 12)), 12))
        ranking = await self._fetch_daily_ranking()
        edition, ranking = await self._edit_with_current_model(event, articles, max_items, ranking)
        selected_ids = {item.article_id for item in edition.items}
        selected = [article for article in articles if article.id in selected_ids]
        raw_cover_images = await scraper.fetch_cover_images(selected)
        cover_images = await self._prepare_cover_images(raw_cover_images)
        logger.info(
            "ACG 日报：入选 %d 条资讯，成功下载并适配 %d 张封面，将开始生成图片。",
            len(selected),
            len(cover_images),
        )
        return await self._render_daily_images(edition, articles, cover_images, results, ranking)

    async def _edit_with_current_model(
        self,
        event: AstrMessageEvent,
        articles: list[Article],
        max_items: int,
        ranking: Ranking | None,
    ) -> tuple[DailyEdition, Ranking | None]:
        try:
            configured_provider_id = configured_editor_provider(self.config.get("editor_provider"))
            if configured_provider_id:
                provider_id = configured_provider_id
                logger.info("ACG 日报：使用配置指定的编辑模型 %s。", provider_id)
            else:
                provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
                logger.info("ACG 日报：使用当前会话的编辑模型 %s。", provider_id)
            system_prompt = configured_system_prompt(self.config.get("editor_system_prompt"))
            prompt = build_editor_prompt(articles, max_items, ranking)
            search_tools = self._search_tools(event)
            logger.info(
                "ACG 日报：编辑输入包含 %d 条候选、%d 条榜单，系统提示 %d 字符、候选输入 %d 字符。",
                len(articles),
                len(ranking.entries) if ranking is not None else 0,
                len(system_prompt),
                len(prompt),
            )
        except Exception as exc:
            logger.error("ACG 日报：无法准备编辑模型，已跳过未经翻译的原始候选：%s", exc)
            return DailyEdition("无法准备日报编辑模型，本次未展示未经翻译的原始候选。", []), None
        try:
            agent_hooks: DailyEditorHooks | None = None
            if search_tools:
                if ranking is not None:
                    logger.info(
                        "ACG 日报：使用模型 %s 编辑 %d 条候选资讯并翻译 %d 条排行榜标题；必要时仅进行一次批量译名核对。",
                        provider_id,
                        len(articles),
                        len(ranking.entries),
                    )
                else:
                    logger.info(
                        "ACG 日报：使用模型 %s 编辑 %d 条候选资讯，必要时仅进行一次批量译名核对。",
                        provider_id,
                        len(articles),
                    )
                agent_hooks = DailyEditorHooks()
                response = await self._await_editor_response(
                    self.context.tool_loop_agent(
                        event=event,
                        chat_provider_id=provider_id,
                        system_prompt=system_prompt,
                        prompt=prompt,
                        tools=search_tools,
                        max_steps=3,
                        tool_call_timeout=30,
                        agent_hooks=agent_hooks,
                    ),
                    "带联网工具的编辑",
                    agent_hooks,
                )
            else:
                logger.info(
                    "ACG 日报：使用模型 %s 编辑 %d 条候选资讯，不使用联网搜索。",
                    provider_id,
                    len(articles),
                )
                response = await self._await_editor_response(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        system_prompt=system_prompt,
                        prompt=prompt,
                    ),
                    "无工具编辑",
                )
            edition, translated_ranking, ranking_error = parse_edition_with_ranking(
                response.completion_text,
                articles,
                max_items,
                ranking,
            )
            logger.info(
                "ACG 日报：编辑模型返回 %d 条有效入选资讯。",
                len(edition.items),
            )
            if ranking_error:
                logger.warning("ACG 日报：排行榜标题翻译失败，已跳过榜单：%s", ranking_error)
            elif translated_ranking is not None:
                logger.info("ACG 日报：编辑模型已中文化 %d 条排行榜标题。", len(translated_ranking.entries))
            if edition.items:
                return edition, translated_ranking
            return DailyEdition(edition.intro or "本次候选资讯中没有适合收录的内容。", []), translated_ranking
        except Exception as exc:
            if not search_tools:
                logger.error("ACG 日报：编辑模型未返回合规 JSON，已跳过未经翻译的原始候选：%s", exc)
                return DailyEdition("编辑模型未返回合规的日报内容，本次未展示未经翻译的原始候选。", []), None
            logger.warning(
                "ACG 日报：带联网工具的编辑未返回合规 JSON，将使用同一模型进行一次无工具重试：%s",
                exc,
            )
        try:
            logger.info("ACG 日报：开始使用模型 %s 进行无工具重试。", provider_id)
            response = await self._await_editor_response(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=system_prompt,
                    prompt=prompt,
                ),
                "无工具重试",
            )
            edition, translated_ranking, ranking_error = parse_edition_with_ranking(
                response.completion_text,
                articles,
                max_items,
                ranking,
            )
            logger.info("ACG 日报：无工具重试成功，编辑模型返回 %d 条有效入选资讯。", len(edition.items))
            if ranking_error:
                logger.warning("ACG 日报：排行榜标题翻译失败，已跳过榜单：%s", ranking_error)
            elif translated_ranking is not None:
                logger.info("ACG 日报：无工具重试已中文化 %d 条排行榜标题。", len(translated_ranking.entries))
            return DailyEdition(edition.intro or "本次候选资讯中没有适合收录的内容。", edition.items), translated_ranking
        except Exception as retry_exc:
            logger.error(
                "ACG 日报：无工具重试仍未返回合规 JSON，已跳过未经翻译的原始候选：%s",
                retry_exc,
            )
            return DailyEdition("编辑模型未返回合规的日报内容，本次未展示未经翻译的原始候选。", []), None

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
        logger.info("ACG 日报：已为编辑模型注入一次性批量译名核对工具，底层搜索提供商为「%s」。", tool_name)
        return ToolSet([BatchTitleLookupTool(tool)])

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
        ranking: Ranking | None,
    ) -> list[str]:
        success_count = sum(1 for result in results if result.articles)
        date_text = datetime.now().astimezone().strftime("%Y 年 %m 月 %d 日")
        source_status = f"本次抓取 {success_count}/{len(results)} 个来源，筛选 {len(edition.items)} 条资讯"
        html = build_daily_image_html(
            edition,
            articles,
            cover_images,
            date_text,
            source_status,
            ranking=ranking,
        )
        logger.info(
            "ACG 日报：开始调用 AstrBot 文转图服务，渲染单张长图（%d 条资讯，HTML %d 字符）。",
            len(edition.items),
            len(html),
        )
        try:
            image = await self.html_render(
                html,
                {},
                options={"type": "png", "full_page": True, "animations": "disabled", "scale": "device"},
            )
        except Exception as exc:
            logger.exception("ACG 日报：AstrBot 文转图服务渲染失败：%s", exc)
            raise RuntimeError("AstrBot 文转图服务渲染失败") from exc
        if not image:
            logger.error("ACG 日报：AstrBot 文转图服务未返回图片地址。")
            raise RuntimeError("AstrBot 文转图服务未返回图片地址")
        logger.info("ACG 日报：AstrBot 文转图服务渲染成功，已获得单张长图。")
        return [image]

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
        logger.info("ACG 日报：已获取排行榜「%s」，共 %d 条，将与新闻共用编辑模型中文化标题。", ranking.source, len(ranking.entries))
        return ranking
