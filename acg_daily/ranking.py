from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

import aiohttp
from bs4 import BeautifulSoup

from .scraper import PublicAddressResolver, USER_AGENT, _read_source_response


RANKING_SOURCE_DISABLED = "disabled"
RANKING_SOURCE_ANIME_PLANET = "anime-planet"
RANKING_SOURCE_ANIME_TRENDING = "anime-trending"
RANKING_SOURCE_ANIME_HACK = "anime-hack"


@dataclass(frozen=True)
class RankingEntry:
    rank: int
    title: str
    detail: str = ""


@dataclass(frozen=True)
class Ranking:
    title: str
    source: str
    entries: tuple[RankingEntry, ...]


@dataclass(frozen=True)
class RankingSource:
    url: str
    title: str
    source: str
    parser: Callable[[bytes], list[RankingEntry]]


def _anime_planet_ranking(body: bytes) -> list[RankingEntry]:
    soup = BeautifulSoup(body, "html.parser")
    entries: list[RankingEntry] = []
    for row in soup.select("table tbody tr"):
        rank_node = row.select_one(".tableRank")
        title_node = row.select_one(".tableTitle a")
        if not rank_node or not title_node:
            continue
        rank_text = rank_node.get_text(" ", strip=True)
        if not rank_text.isdigit():
            continue
        details = []
        for selector in (".tableType", ".tableYear", ".epRating"):
            node = row.select_one(selector)
            if node:
                value = node.get_text(" ", strip=True)
                if value:
                    details.append(value)
        entries.append(RankingEntry(int(rank_text), title_node.get_text(" ", strip=True), " · ".join(details)))
        if len(entries) == 10:
            break
    return entries


def _anime_trending_ranking(body: bytes) -> list[RankingEntry]:
    soup = BeautifulSoup(body, "html.parser")
    data_node = soup.select_one("script#__NEXT_DATA__")
    if data_node is None or not data_node.string:
        return []
    try:
        choices = json.loads(data_node.string)["props"]["pageProps"]["charts"][0]["choices"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        return []

    entries: list[RankingEntry] = []
    seen_positions: set[int] = set()
    for choice in choices:
        position = choice.get("position")
        name = choice.get("name")
        if (
            not isinstance(position, int)
            or not isinstance(name, str)
            or not name
            or position in seen_positions
        ):
            continue
        detail = choice.get("subText")
        entries.append(RankingEntry(position, name, detail if isinstance(detail, str) else ""))
        seen_positions.add(position)
        if len(entries) == 10:
            break
    return entries


def _anime_hack_ranking(body: bytes) -> list[RankingEntry]:
    soup = BeautifulSoup(body, "html.parser")
    for heading in soup.select("h1, h2, h3"):
        if heading.get_text(" ", strip=True) != "人気アニメランキング":
            continue
        entries: list[RankingEntry] = []
        for rank_label in heading.parent.select("li"):
            match = re.match(r"^(\d+)位", rank_label.get_text(" ", strip=True))
            if match is None:
                continue
            title_node = rank_label.select_one('a[href*="/program/"]:not([href*="/checkin/"])')
            if title_node is None:
                continue
            detail_node = rank_label.select_one('a[href*="/program/season/"]')
            entries.append(
                RankingEntry(
                    int(match.group(1)),
                    title_node.get_text(" ", strip=True),
                    detail_node.get_text(" ", strip=True) if detail_node else "",
                )
            )
            if len(entries) == 10:
                return entries
        return entries
    return []


RANKING_SOURCES = {
    RANKING_SOURCE_ANIME_PLANET: RankingSource(
        "https://www.anime-planet.com/anime/top-anime/today",
        "今日动画榜 Top 10",
        "Anime-Planet",
        _anime_planet_ranking,
    ),
    RANKING_SOURCE_ANIME_TRENDING: RankingSource(
        "https://www.anitrendz.com/charts/top-anime",
        "动画热度榜 Top 10",
        "Anime Trending",
        _anime_trending_ranking,
    ),
    RANKING_SOURCE_ANIME_HACK: RankingSource(
        "https://anime.eiga.com/ranking/program/",
        "热门动画榜 Top 10",
        "Anime Hack",
        _anime_hack_ranking,
    ),
}


async def fetch_ranking(source_key: object, timeout_seconds: int) -> Ranking | None:
    """Fetch one built-in Top 10 source without affecting news collection."""

    source = RANKING_SOURCES.get(str(source_key or "").strip())
    if source is None:
        return None

    timeout = aiohttp.ClientTimeout(total=max(1, timeout_seconds))
    connector = aiohttp.TCPConnector(resolver=PublicAddressResolver())
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=connector) as session:
        _final_url, body, _content_type = await _read_source_response(session, source.url)
    entries = source.parser(body)
    if not entries:
        raise ValueError("未解析到排行榜条目")
    return Ranking(source.title, source.source, tuple(entries[:10]))
