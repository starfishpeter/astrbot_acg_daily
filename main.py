from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Nodes, Plain
from astrbot.api.star import Context, Star

from .acg_daily.editor import SYSTEM_PROMPT, build_editor_prompt, fallback_edition, parse_edition
from .acg_daily.models import Article, DailyEdition
from .acg_daily.scraper import NewsScraper, SourceResult, deduplicate_articles, is_http_url


@dataclass
class CacheEntry:
    created_at: float
    nodes: list[Node]


class AcgDailyPlugin(Star):
    """Use /acg日报 to create an AI-edited QQ forward-message news digest."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._cache: dict[str, CacheEntry] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}

    @filter.command("acg日报", alias={"ACG日报"})
    async def acg_daily(self, event: AstrMessageEvent):
        """即时抓取已配置的 ACG 资讯源并发送合并转发日报。"""

        urls = self._source_urls()
        if not urls:
            yield event.plain_result("请先在插件配置的「资讯源链接」列表中至少添加一个 http(s) URL。")
            return

        session_key = event.unified_msg_origin
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            cached = self._cached_nodes(session_key)
            if cached is not None:
                yield event.chain_result([Nodes(nodes=cached)])
                return

            try:
                nodes = await self._create_daily_nodes(event, urls)
            except Exception as exc:
                logger.exception("ACG daily generation failed")
                yield event.plain_result(f"生成 ACG 日报失败：{exc}")
                return

            self._cache[session_key] = CacheEntry(time.monotonic(), nodes)
            yield event.chain_result([Nodes(nodes=nodes)])

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

    def _cached_nodes(self, session_key: str) -> list[Node] | None:
        cooldown = max(0, int(self.config.get("cooldown_seconds", 60)))
        entry = self._cache.get(session_key)
        if not entry or cooldown == 0 or time.monotonic() - entry.created_at > cooldown:
            return None
        return copy.deepcopy(entry.nodes)

    async def _create_daily_nodes(
        self,
        event: AstrMessageEvent,
        urls: list[str],
    ) -> list[Node]:
        scraper = NewsScraper(
            timeout_seconds=int(self.config.get("request_timeout_seconds", 10)),
            max_articles_per_source=int(self.config.get("max_articles_per_source", 10)),
        )
        results = await scraper.collect(urls)
        articles = deduplicate_articles(
            [article for result in results for article in result.articles],
            max_candidates=max(1, min(int(self.config.get("max_candidates", 40)), 80)),
        )
        if not articles:
            return self._empty_nodes(event, results)

        max_items = max(1, min(int(self.config.get("max_daily_items", 7)), 12))
        edition = await self._edit_with_current_model(event, articles, max_items)
        return self._build_nodes(event, results, articles, edition)

    async def _edit_with_current_model(
        self,
        event: AstrMessageEvent,
        articles: list[Article],
        max_items: int,
    ) -> DailyEdition:
        try:
            provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=SYSTEM_PROMPT,
                prompt=build_editor_prompt(articles, max_items),
            )
            edition = parse_edition(response.completion_text, articles, max_items)
            if edition.items:
                return edition
            return DailyEdition(edition.intro or "本次候选资讯中没有适合收录的内容。", [])
        except Exception as exc:
            logger.warning("ACG daily editor unavailable, using source-order fallback: %s", exc)
            return fallback_edition(articles, max_items)

    def _build_nodes(
        self,
        event: AstrMessageEvent,
        results: list[SourceResult],
        articles: list[Article],
        edition: DailyEdition,
    ) -> list[Node]:
        bot_uin = event.get_self_id() or "0"
        node_name = str(self.config.get("node_name", "ACG 日报")).strip() or "ACG 日报"
        success_count = sum(1 for result in results if result.articles)
        article_by_id = {article.id: article for article in articles}
        date_text = datetime.now().astimezone().strftime("%Y 年 %m 月 %d 日")
        intro = edition.intro or "为你整理今日值得关注的 ACG 动态。"
        nodes = [
            Node(
                uin=bot_uin,
                name=node_name,
                content=[
                    Plain(f"ACG 日报 | {date_text}\n"),
                    Plain(f"本次抓取：{success_count}/{len(results)} 个来源可用，候选 {len(articles)} 条。\n"),
                    Plain(intro),
                ],
            ),
        ]
        for item in edition.items:
            article = article_by_id[item.article_id]
            content = f"[{item.category}] {item.title}\n{item.summary}"
            if item.reason:
                content += f"\n关注点：{item.reason}"
            content += f"\n来源：{article.source}\n原文：{article.url}"
            nodes.append(Node(uin=bot_uin, name=node_name, content=[Plain(content)]))
        if not edition.items:
            nodes.append(
                Node(
                    uin=bot_uin,
                    name=node_name,
                    content=[Plain("本次没有筛选出适合收录的资讯。可稍后再试，或调整资讯源链接。")],
                ),
            )
        nodes.append(
            Node(
                uin=bot_uin,
                name=node_name,
                content=[Plain("内容由已配置资讯源即时抓取，并由 AI 筛选和整理。请以原文为准。")],
            ),
        )
        return nodes

    def _empty_nodes(self, event: AstrMessageEvent, results: list[SourceResult]) -> list[Node]:
        bot_uin = event.get_self_id() or "0"
        node_name = str(self.config.get("node_name", "ACG 日报")).strip() or "ACG 日报"
        unavailable = sum(1 for result in results if result.error)
        return [
            Node(
                uin=bot_uin,
                name=node_name,
                content=[
                    Plain("ACG 日报\n"),
                    Plain(f"本次未从 {len(results)} 个资讯源中提取到可用资讯，其中 {unavailable} 个来源不可用。"),
                ],
            ),
        ]
