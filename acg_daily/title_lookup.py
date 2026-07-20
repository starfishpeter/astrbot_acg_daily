from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable


MAX_LOOKUP_TITLES = 20
MAX_LOOKUP_GROUPS = 3
MAX_LOOKUP_QUERY_CHARS = 420
MAX_BAIDU_LOOKUP_QUERY_CHARS = 67


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

    query = "；".join(titles)
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
