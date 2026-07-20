from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp


MAX_LOOKUP_TITLES = 20
MAX_LOOKUP_GROUPS = 3
MAX_LOOKUP_QUERY_CHARS = 420
MAX_BAIDU_LOOKUP_QUERY_CHARS = 67
MAX_BANGUMI_CONCURRENCY = 5
MAX_BANGUMI_RESULTS_PER_TITLE = 3
_BANGUMI_SEARCH_URL = "https://api.bgm.tv/v0/search/subjects"
_BANGUMI_SUBJECT_TYPES = (1, 2, 4)
_BANGUMI_TYPE_LABELS = {1: "书籍/漫画", 2: "动画", 4: "游戏"}


@dataclass(frozen=True)
class BangumiCandidate:
    name: str
    name_cn: str
    subject_type: int
    date: str


@dataclass(frozen=True)
class TitleLookupReference:
    query: str
    channel: str
    candidates: tuple[str, ...]


def normalized_lookup_titles(value: object, max_title_chars: int = 120) -> list[str]:
    """Keep a bounded, de-duplicated list of model-requested titles."""

    if not isinstance(value, list):
        return []
    titles: list[str] = []
    for raw_title in value:
        title = " ".join(str(raw_title).split())[:max_title_chars]
        if title and title not in titles:
            titles.append(title)
        if len(titles) >= MAX_LOOKUP_TITLES:
            break
    return titles


def lookup_title_groups(titles: list[str], max_query_chars: int = MAX_LOOKUP_QUERY_CHARS) -> list[list[str]]:
    """Split titles into a small number of bounded search queries."""

    groups: list[list[str]] = [[]]
    current_size = 0
    for title in titles:
        added_size = len(title) + 1
        if groups[-1] and current_size + added_size > max_query_chars:
            if len(groups) >= MAX_LOOKUP_GROUPS:
                break
            groups.append([])
            current_size = 0
        groups[-1].append(title)
        current_size += added_size
    return [group for group in groups if group]


def lookup_search_arguments(
    titles: list[str],
    tool_name: str,
    tool_parameters: object,
) -> dict[str, object]:
    """Use each built-in provider's native result-limit parameter."""

    query = "、".join(f'"{title}"' for title in titles)
    if tool_name == "web_search_baidu":
        query += " 中文译名"
    else:
        query = "请核对以下作品在中国大陆 ACG 社群常用的中文译名，仅返回与这些作品有关的中文资料：" + query
    arguments: dict[str, object] = {"query": query}
    properties = tool_parameters.get("properties", {}) if isinstance(tool_parameters, dict) else {}
    for name, limit in (("max_results", 5), ("count", 4), ("limit", 4), ("top_k", 4), ("num_results", 4)):
        if name in properties:
            arguments[name] = limit
            break
    return arguments


async def run_lookup_groups(
    groups: list[list[str]],
    search_group: Callable[[list[str]], Awaitable[object]],
) -> list[object]:
    """Run independent provider requests together without failing the whole lookup."""

    return await asyncio.gather(
        *(search_group(group) for group in groups),
        return_exceptions=True,
    )


def parse_bangumi_candidates(payload: object) -> list[BangumiCandidate]:
    """Keep only concise Bangumi candidates that provide a Chinese title."""

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    candidates: list[BangumiCandidate] = []
    for subject in data:
        if not isinstance(subject, dict):
            continue
        name = " ".join(str(subject.get("name", "")).split())[:160]
        name_cn = " ".join(str(subject.get("name_cn", "")).split())[:160]
        subject_type = subject.get("type")
        if not name or not name_cn or not isinstance(subject_type, int):
            continue
        if subject_type not in _BANGUMI_SUBJECT_TYPES:
            continue
        candidate = BangumiCandidate(
            name=name,
            name_cn=name_cn,
            subject_type=subject_type,
            date=" ".join(str(subject.get("date", "")).split())[:20],
        )
        if candidate not in candidates:
            candidates.append(candidate)
        if len(candidates) >= MAX_BANGUMI_RESULTS_PER_TITLE:
            break
    return candidates


def format_bangumi_candidates(matches: dict[str, list[BangumiCandidate]]) -> str:
    """Return compact candidate names for the editor, never API payloads or tokens."""

    lines = ["Bangumi 词条候选（请结合新闻上下文选择合适译名；同名候选不代表同一作品）："]
    for query, candidates in matches.items():
        if not candidates:
            continue
        lines.append(f"- 查询「{query}」：")
        for candidate in candidates:
            kind = _BANGUMI_TYPE_LABELS[candidate.subject_type]
            date = f"；{candidate.date}" if candidate.date else ""
            lines.append(f"  - [{kind}] {candidate.name} -> {candidate.name_cn}{date}")
    return "\n".join(lines) if len(lines) > 1 else ""


def unresolved_bangumi_titles(titles: list[str], matches: dict[str, list[BangumiCandidate]]) -> list[str]:
    """Only titles with no Chinese candidate need a web-search fallback."""

    return [title for title in titles if not matches.get(title)]


def lookup_references_for_text(
    text: str,
    bangumi_matches: dict[str, list[BangumiCandidate]],
    web_fallback_titles: list[str],
    web_provider: str,
) -> list[TitleLookupReference]:
    """Relate an original title to Bangumi or web-search material it contained."""

    normalized_text = " ".join(str(text).casefold().split())
    references: list[TitleLookupReference] = []
    for query, candidates in bangumi_matches.items():
        normalized_query = " ".join(query.casefold().split())
        if normalized_query and normalized_query in normalized_text:
            references.append(
                TitleLookupReference(
                    query,
                    "Bangumi",
                    tuple(candidate.name_cn for candidate in candidates),
                )
            )
    for query in web_fallback_titles:
        normalized_query = " ".join(query.casefold().split())
        if normalized_query and normalized_query in normalized_text:
            references.append(TitleLookupReference(query, web_provider, ()))
    return references


def bangumi_search_request(title: str, access_token: str) -> tuple[str, dict[str, str], dict[str, object]]:
    """Build the request without exposing the token to editor-visible data."""

    return (
        _BANGUMI_SEARCH_URL,
        {
            "Accept": "application/json",
            "User-Agent": "astrbot-plugin-acg-daily",
            "Authorization": f"Bearer {access_token}",
        },
        {"keyword": title, "filter": {"type": list(_BANGUMI_SUBJECT_TYPES)}, "limit": 5},
    )


async def run_bangumi_lookups(
    titles: list[str],
    lookup_title: Callable[[str], Awaitable[list[BangumiCandidate]]],
) -> dict[str, list[BangumiCandidate]]:
    """Query independent titles concurrently while treating failed lookups as unresolved."""

    semaphore = asyncio.Semaphore(MAX_BANGUMI_CONCURRENCY)

    async def lookup(title: str) -> tuple[str, list[BangumiCandidate]]:
        try:
            async with semaphore:
                return title, await lookup_title(title)
        except Exception:
            return title, []

    return dict(await asyncio.gather(*(lookup(title) for title in titles)))


async def lookup_bangumi_titles(
    titles: list[str],
    access_token: str,
    timeout_seconds: int,
) -> dict[str, list[BangumiCandidate]]:
    """Search Bangumi across books, animation, and games for Chinese title candidates."""

    timeout = aiohttp.ClientTimeout(total=max(1, min(timeout_seconds, 30)))
    async with aiohttp.ClientSession(timeout=timeout) as session:

        async def lookup_title(title: str) -> list[BangumiCandidate]:
            url, headers, payload = bangumi_search_request(title, access_token)
            async with session.post(url, headers=headers, json=payload) as response:
                response.raise_for_status()
                return parse_bangumi_candidates(await response.json())

        return await run_bangumi_lookups(titles, lookup_title)


def compact_lookup_result(result: object) -> str:
    """Return only the title and short snippet from a search response."""

    if isinstance(result, BaseException):
        return ""
    text = str(result)
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return text[:1200]
    raw_results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw_results, list):
        return text[:1200]
    snippets: list[str] = []
    for entry in raw_results[:4]:
        if not isinstance(entry, dict):
            continue
        title = " ".join(str(entry.get("title", "")).split())[:160]
        snippet = " ".join(str(entry.get("snippet", "")).split())[:260]
        if title or snippet:
            snippets.append(f"- {title}: {snippet}".rstrip(": "))
    return "\n".join(snippets)
