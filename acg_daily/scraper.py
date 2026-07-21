from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
import feedparser
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver
from bs4 import BeautifulSoup

from .models import Article

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TAVILY_EXTRACT_CHARS = 15_000
# Covers are decoded and recompressed before rendering. Allow a reasonable
# source-file size here so common 1200px+ publisher artwork is not discarded.
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 3
# Some public feeds, including Bahamut GNN, block unknown bot user agents while
# serving their public RSS normally to browser-compatible clients.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref"}
logger = logging.getLogger("astrbot")


@dataclass(frozen=True)
class SourceResult:
    url: str
    source_name: str
    articles: list[Article]
    error: str = ""


class PublicAddressResolver(AbstractResolver):
    """Resolve source hosts only to public addresses during the actual connect."""

    def __init__(self) -> None:
        self._resolver = DefaultResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_UNSPEC,
    ) -> list[dict[str, object]]:
        addresses = await self._resolver.resolve(host, port, family)
        public_addresses = [
            address
            for address in addresses
            if _is_public_ip(str(address["host"]))
        ]
        if not public_addresses:
            raise OSError("hostname does not resolve to a public IP address")
        return public_addresses

    async def close(self) -> None:
        await self._resolver.close()


def clean_text(value: object, limit: int = 700) -> str:
    """Turn HTML or irregular whitespace into a bounded plain-text value."""

    if value is None:
        return ""
    soup = BeautifulSoup(str(value), "html.parser")
    text = unescape(soup.get_text(" ", strip=True))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def is_http_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
    )


def canonical_url(url: str) -> str:
    """Remove fragment and common tracking parameters before deduplication."""

    if not is_http_url(url):
        return ""
    parsed = urlsplit(url)
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in TRACKING_QUERY_KEYS
        ],
        doseq=True,
    )
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, query, ""),
    )


def normalized_title(title: str) -> str:
    return re.sub(r"[^\w]+", "", title.casefold(), flags=re.UNICODE)


def _is_public_ip(value: str) -> bool:
    return ipaddress.ip_address(value).is_global


async def validate_source_url(url: str) -> None:
    """Reject local and private targets before a configured URL is fetched."""

    if not is_http_url(url):
        raise ValueError("only public http(s) URLs are allowed")

    hostname = urlsplit(url).hostname
    assert hostname is not None
    hostname = hostname.rstrip(".")
    if hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError("local hosts are not allowed")

    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not _is_public_ip(hostname):
            raise ValueError("private IP addresses are not allowed")
        return

    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(
            hostname,
            None,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError("hostname could not be resolved") from exc

    if not records or any(not _is_public_ip(record[4][0]) for record in records):
        raise ValueError("hostname resolves to a non-public IP address")


async def _read_source_response(
    session: aiohttp.ClientSession,
    source_url: str,
) -> tuple[str, bytes, str]:
    """Fetch manually redirected content so each redirect is safety-checked."""

    current_url = source_url
    for _ in range(MAX_REDIRECTS + 1):
        await validate_source_url(current_url)
        async with session.get(current_url, allow_redirects=False) as response:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("redirect response has no Location header")
                current_url = urljoin(current_url, location)
                continue
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}")
            content_length = response.content_length
            if content_length is not None and content_length > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds the 2 MiB limit")
            body = await _read_limited_body(response.content, MAX_RESPONSE_BYTES, "response")
            return current_url, body, response.headers.get("Content-Type", "")
    raise ValueError("too many redirects")


async def _read_limited_body(content, maximum_bytes: int, label: str) -> bytes:
    """Read through EOF while enforcing a byte limit for streaming responses."""

    body = bytearray()
    while True:
        chunk = await content.read(min(64 * 1024, maximum_bytes + 1 - len(body)))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise ValueError(f"{label} exceeds the {maximum_bytes // (1024 * 1024)} MiB limit")


def _feed_value(entry: object, name: str, default: object = "") -> object:
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _image_url(value: object, source_url: str) -> str:
    if not value:
        return ""
    url = urljoin(source_url, str(value))
    return url if is_http_url(url) else ""


def _srcset_image_url(value: object, source_url: str) -> str:
    """Return the largest usable candidate from an image srcset attribute."""

    candidates = str(value or "").split(",")
    for candidate in reversed(candidates):
        url = _image_url(candidate.strip().split(" ", 1)[0], source_url)
        if url:
            return url
    return ""


def _first_image_url(value: object, source_url: str) -> str:
    soup = BeautifulSoup(str(value or ""), "html.parser")
    for node in soup.find_all(True):
        for attribute in ("srcset", "data-srcset"):
            url = _srcset_image_url(node.get(attribute), source_url)
            if url:
                return url
        for attribute in (
            "src",
            "data-src",
            "data-lazy-src",
            "data-original",
            "data-image",
        ):
            url = _image_url(node.get(attribute), source_url)
            if url:
                return url
    return ""


def _main_content_image_url(soup: BeautifulSoup, source_url: str) -> str:
    """Find a likely article image in main content without accepting page chrome."""

    main = soup.select_one("main")
    if main is None:
        return ""
    excluded = re.compile(
        r"(?:^|[-_\s])(?:ad|ads|advert(?:isement)?|banner|sponsor|promo|nav|menu|header|footer|sidebar)(?:$|[-_\s])",
        re.IGNORECASE,
    )
    for image in main.find_all("img"):
        ancestors = [image, *image.parents]
        if any(
            ancestor.name in {"nav", "aside", "header", "footer"}
            or excluded.search(" ".join(ancestor.get("class", [])) + " " + str(ancestor.get("id", "")))
            for ancestor in ancestors
            if getattr(ancestor, "name", None)
        ):
            continue
        url = _first_image_url(image, source_url)
        if url:
            return url
    return ""


def _article_page_cover_url(body: bytes, source_url: str) -> str:
    """Find a representative image from an article page before using a feed thumbnail."""

    soup = BeautifulSoup(body, "html.parser")
    for attrs in (
        {"property": "og:image"},
        {"property": "og:image:url"},
        {"name": "twitter:image"},
        {"name": "twitter:image:src"},
        {"itemprop": "image"},
    ):
        node = soup.find("meta", attrs=attrs)
        if node:
            url = _image_url(node.get("content"), source_url)
            if url:
                return url

    for selector in (
        "article",
        "main article",
        ".entry-content",
        ".post-entry",
        ".post-content",
        ".article-content",
        ".article-body",
        "figure",
    ):
        node = soup.select_one(selector)
        if node:
            url = _first_image_url(node, source_url)
            if url:
                return url
    return _main_content_image_url(soup, source_url)


def _feed_cover_url(entry: object, source_url: str) -> str:
    """Extract a feed-supplied cover without fetching an article page."""

    for field in ("media_content", "media_thumbnail", "enclosures"):
        value = _feed_value(entry, field, [])
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                url = _image_url(item.get("url") or item.get("href"), source_url)
                if url:
                    return url

    links = _feed_value(entry, "links", [])
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            if str(link.get("type", "")).lower().startswith("image/"):
                url = _image_url(link.get("href"), source_url)
                if url:
                    return url

    for field in ("content", "summary", "description"):
        value = _feed_value(entry, field, "")
        if isinstance(value, list):
            for item in value:
                url = _first_image_url(_feed_value(item, "value", ""), source_url)
                if url:
                    return url
        else:
            url = _first_image_url(value, source_url)
            if url:
                return url
    return ""


def _feed_entries(body: bytes, source_url: str, limit: int) -> tuple[str, list[Article]]:
    feed = feedparser.parse(body)
    entries = list(getattr(feed, "entries", []) or [])
    # RSS 1.0/RDF feeds report versions such as ``rss10``. They are common on
    # Japanese anime sites, so only require a parsed entry list here.
    if not entries:
        return "", []

    feed_info = getattr(feed, "feed", {})
    source_name = clean_text(_feed_value(feed_info, "title"), 100)
    source_name = re.sub(r"^(?:news|feed)\s*[-|]\s*", "", source_name, flags=re.IGNORECASE)
    source_name = re.sub(r"\s*[-|]\s*(?:rss|atom)\s*feed\s*$", "", source_name, flags=re.IGNORECASE)
    source_name = source_name or urlsplit(source_url).hostname or source_url
    articles: list[Article] = []
    for entry in entries[:limit]:
        title = clean_text(_feed_value(entry, "title"), 200)
        link = str(_feed_value(entry, "link", "") or "")
        link = urljoin(source_url, link)
        if not title or not is_http_url(link):
            continue
        summary = _feed_value(entry, "summary", "") or _feed_value(
            entry,
            "description",
            "",
        )
        if not summary:
            content = _feed_value(entry, "content", [])
            if isinstance(content, list) and content:
                summary = _feed_value(content[0], "value", "")
        articles.append(
            Article(
                id=0,
                title=title,
                summary=clean_text(summary),
                url=canonical_url(link),
                source=source_name,
                published_at=clean_text(
                    _feed_value(entry, "published", "")
                    or _feed_value(entry, "updated", ""),
                    80,
                ),
                cover_url=_feed_cover_url(entry, source_url),
            ),
        )
    return source_name, articles


def _source_name(soup: BeautifulSoup, source_url: str) -> str:
    for attrs in (
        {"property": "og:site_name"},
        {"name": "application-name"},
    ):
        node = soup.find("meta", attrs=attrs)
        if node and node.get("content"):
            return clean_text(node["content"], 100)
    title = clean_text(soup.title.string if soup.title else "", 100)
    if title:
        return re.split(r"\s+[|\-]\s+", title, maxsplit=1)[-1]
    return urlsplit(source_url).hostname or source_url


_TITLE_SELECTOR = (
    "h1, h2, h3, h4, [class~='title'], [class~='headline'], [class~='heading'], "
    "[class*='-title'], [class*='_title'], [class*='-headline'], [class*='_headline'], "
    "[class*='-heading'], [class*='_heading'], [class*='headline-text'], "
    "[class*='headline_text'], [class*='title-text'], [class*='title_text'], "
    "[class*='heading-text'], [class*='heading_text']"
)


def _article_from_container(
    container,
    source_url: str,
    source_name: str,
) -> Article | None:
    heading = container.select_one(_TITLE_SELECTOR)
    anchor = None
    if heading is not None:
        # A visual card may wrap its heading in a link rather than put the
        # link inside it, for example ``<a><h2>Headline</h2></a>``.
        anchor = heading.select_one("a[href]") or heading.find_parent("a", href=True)
    if anchor is None:
        return None
    title = clean_text(heading or anchor, 200)
    link = canonical_url(urljoin(source_url, str(anchor.get("href", ""))))
    if not title or not link:
        return None

    summary_node = container.select_one(
        ".snippet .hook, .entry-summary, .entry-excerpt, .excerpt, .summary, .text",
    )
    if summary_node is not None:
        summary = clean_text(summary_node, 700)
    else:
        summary_parts = [clean_text(node, 350) for node in container.find_all("p")[:2]]
        summary = clean_text(" ".join(part for part in summary_parts if part), 700)
    if summary == title:
        summary = ""
    time_node = container.find("time")
    published_at = ""
    if time_node:
        published_at = clean_text(
            time_node.get("datetime") or time_node.get_text(" ", strip=True),
            80,
        )
    else:
        info_node = container.select_one(
            ".information .info, .byline, .date, [class~='date'], [class~='published'], "
            "[class*='-date'], [class*='_date'], [class*='-published'], [class*='_published']",
        )
        published_at = clean_text(info_node, 80) if info_node else ""
    return Article(
        0,
        title,
        summary,
        link,
        source_name,
        published_at,
        _first_image_url(container, source_url),
    )


def _html_entries(body: bytes, source_url: str, limit: int) -> tuple[str, list[Article]]:
    soup = BeautifulSoup(body, "html.parser")
    source_name = _source_name(soup, source_url)
    containers = soup.find_all("article")
    if not containers:
        containers = soup.select(
            "li, [class*='news'], [class*='post'], [class*='entry'], "
            "[class*='article'], [class*='item'], [class*='card']",
        )

    articles: list[Article] = []
    seen_links: set[str] = set()
    for container in containers:
        article = _article_from_container(container, source_url, source_name)
        if article and article.url not in seen_links:
            articles.append(article)
            seen_links.add(article.url)
        if len(articles) >= limit:
            return source_name, articles

    # Some sites have bare headline lists or visual cards instead of article
    # elements. Common class-name fragments cover those structures without
    # binding extraction to a publisher domain.
    for heading in soup.select(_TITLE_SELECTOR):
        anchor = heading.select_one("a[href]") or heading.find_parent("a", href=True)
        if anchor is None:
            continue
        link = canonical_url(urljoin(source_url, str(anchor.get("href", ""))))
        title = clean_text(anchor, 200)
        if not title or not link or link in seen_links:
            continue
        parent = heading.parent
        summary_node = parent.find("p") if parent else None
        articles.append(
            Article(
                0,
                title,
                clean_text(summary_node, 700) if summary_node else "",
                link,
                source_name,
                cover_url=_first_image_url(parent, source_url) if parent else "",
            ),
        )
        seen_links.add(link)
        if len(articles) >= limit:
            break
    return source_name, articles


_TAVILY_MARKDOWN_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^\s)]+)(?:\s+[^)]*)?\)")
_TAVILY_MARKDOWN_BLOCK = re.compile(r"(?m)(?=^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+))")
_TAVILY_GENERIC_LINK_TITLES = {"more", "read more", "details", "link"}


def _tavily_list_entries(raw_content: str, source_url: str, source_name: str, limit: int) -> list[Article]:
    """Extract linked list entries from Tavily's Markdown-like page content."""

    articles: list[Article] = []
    seen_urls: set[str] = set()
    for block in _TAVILY_MARKDOWN_BLOCK.split(raw_content):
        link = _TAVILY_MARKDOWN_LINK.search(block)
        if link is None:
            continue
        title = clean_text(link.group(1), 200)
        url = canonical_url(urljoin(source_url, link.group(2)))
        if not title or not url or url in seen_urls:
            continue
        heading = re.match(r"\s*#{1,6}\s+(.+?)(?:\n|$)", block)
        heading_title = clean_text(
            _TAVILY_MARKDOWN_LINK.sub(r"\1", heading.group(1)) if heading else "",
            200,
        )
        if title.casefold() in _TAVILY_GENERIC_LINK_TITLES and heading_title:
            title = heading_title
        plain_block = _TAVILY_MARKDOWN_LINK.sub(r"\1", block)
        plain_block = re.sub(r"(?m)^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)", "", plain_block)
        summary = clean_text(plain_block, 2000)
        if summary.startswith(title):
            summary = summary[len(title) :].lstrip(" .:-")
        articles.append(
            Article(
                id=0,
                title=title,
                summary=summary[:700] or title,
                url=url,
                source=source_name,
                published_at=(
                    re.search(r"\b20\d{2}[-./]\d{1,2}[-./]\d{1,2}\b", block).group(0)
                    if re.search(r"\b20\d{2}[-./]\d{1,2}[-./]\d{1,2}\b", block)
                    else ""
                ),
            ),
        )
        seen_urls.add(url)
        if len(articles) >= limit:
            break
    return articles


def tavily_extract_entries(content: object, source_url: str, limit: int) -> tuple[str, list[Article]]:
    """Turn one Tavily Extract response into bounded untrusted news candidates."""

    if not isinstance(content, str):
        return "", []
    raw_content = content.strip()[:MAX_TAVILY_EXTRACT_CHARS]
    if raw_content.startswith("URL:"):
        content_marker = re.search(r"(?m)^\s*Content:\s*", raw_content)
        if content_marker is None:
            return "", []
        raw_content = raw_content[content_marker.end() :]
    raw_content = raw_content.strip()
    if not raw_content or raw_content.startswith("Error:"):
        return "", []

    source_name = f"Tavily · {urlsplit(source_url).hostname or source_url}"
    # Tavily may return the original XML for a configured RSS/Atom/RDF URL.
    # Reuse the generic feed parser so those entries are not collapsed into one
    # candidate page merely because their retrieval used Tavily.
    feed_start = re.search(r"<(?:(?:\?xml\b)|(?:rss\b)|(?:feed\b)|(?:rdf:RDF\b))", raw_content, re.IGNORECASE)
    if feed_start:
        feed_name, feed_articles = _feed_entries(
            raw_content[feed_start.start() :].encode("utf-8"),
            source_url,
            limit,
        )
        if feed_articles:
            source_name = f"Tavily · {feed_name}"
            return (
                source_name,
                [
                    Article(
                        id=article.id,
                        title=article.title,
                        summary=article.summary,
                        url=article.url,
                        source=source_name,
                        published_at=article.published_at,
                        cover_url=article.cover_url,
                    )
                    for article in feed_articles
                ],
            )

    list_articles = _tavily_list_entries(raw_content, source_url, source_name, limit)
    if list_articles:
        return source_name, list_articles

    # Tavily returns Markdown-like raw content. Preserve link labels but discard
    # syntax so a page heading can become the candidate title.
    plain_content = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", raw_content)
    headings = re.findall(r"(?m)^\s*#{1,6}\s+(.+?)\s*$", plain_content)
    title = clean_text(headings[0], 200) if headings else ""
    plain_content = re.sub(r"(?m)^\s*#{1,6}\s+", "", plain_content)
    text = clean_text(plain_content, 2000)
    if not title:
        title = clean_text(text.split(". ", 1)[0], 200)
    if not title:
        return source_name, []
    summary = text
    if summary.startswith(title):
        summary = summary[len(title) :].lstrip(" .:-")
    if not summary:
        summary = title
    return (
        source_name,
        [
            Article(
                id=0,
                title=title,
                summary=summary[:700],
                url=canonical_url(source_url),
                source=source_name,
            ),
        ][: max(0, limit)],
    )


async def collect_tavily_extract_sources(
    urls: list[str],
    tool: object,
    tool_context: object,
    max_articles_per_source: int,
) -> list[SourceResult]:
    """Use AstrBot's Tavily Extract tool for one independently configured URL each."""

    limit = max(1, min(max_articles_per_source, 20))

    async def extract_one(url: str) -> SourceResult:
        try:
            await validate_source_url(url)
            result = await tool.call(tool_context, url=url, extract_depth="basic")
            source_name, articles = tavily_extract_entries(result, url, limit)
            if not articles:
                return SourceResult(url, source_name or "Tavily", [], "未从 Tavily 提取结果中识别到资讯内容")
            return SourceResult(url, source_name, articles)
        except Exception as exc:
            return SourceResult(url, "Tavily", [], str(exc) or type(exc).__name__)

    return await asyncio.gather(*(extract_one(url) for url in urls))


def _date_sort_value(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError):
            return 0.0


def deduplicate_articles(
    articles: Iterable[Article],
    max_candidates: int,
) -> list[Article]:
    """Deduplicate and interleave sources so one fast publisher cannot dominate."""

    unique: list[Article] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for article in sorted(articles, key=lambda item: _date_sort_value(item.published_at), reverse=True):
        title_key = normalized_title(article.title)
        if not article.url or not title_key:
            continue
        if article.url in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(article.url)
        seen_titles.add(title_key)
        unique.append(article)
        if len(unique) >= max_candidates:
            break
    by_source: dict[str, list[Article]] = {}
    for item in unique:
        by_source.setdefault(item.source, []).append(item)

    balanced: list[Article] = []
    while len(balanced) < max_candidates:
        added = False
        for source in by_source:
            if not by_source[source]:
                continue
            balanced.append(by_source[source].pop(0))
            added = True
            if len(balanced) >= max_candidates:
                break
        if not added:
            break

    return [
        Article(
            index,
            item.title,
            item.summary,
            item.url,
            item.source,
            item.published_at,
            item.cover_url,
        )
        for index, item in enumerate(balanced, start=1)
    ]


class NewsScraper:
    def __init__(self, timeout_seconds: int, max_articles_per_source: int) -> None:
        self.timeout_seconds = max(1, min(timeout_seconds, 30))
        self.max_articles_per_source = max(1, min(max_articles_per_source, 20))

    async def collect(self, urls: list[str]) -> list[SourceResult]:
        logger.info("ACG 日报：开始抓取 %d 个已配置资讯源。", len(urls))
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, "
            "text/html, application/xhtml+xml;q=0.9, */*;q=0.1",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        connector = aiohttp.TCPConnector(resolver=PublicAddressResolver())
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            connector=connector,
        ) as session:
            results = await asyncio.gather(
                *(self._collect_one(session, url) for url in urls),
                return_exceptions=True,
            )

        normalized: list[SourceResult] = []
        for url, result in zip(urls, results):
            if isinstance(result, Exception):
                normalized.append(SourceResult(url, urlsplit(url).hostname or url, [], str(result)))
            else:
                normalized.append(result)
        for result in normalized:
            if result.error:
                logger.warning(
                    "ACG 日报：资讯源抓取失败（%s）：%s",
                    result.url,
                    result.error,
                )
            else:
                logger.info(
                    "ACG 日报：资讯源抓取成功（%s），获得 %d 条资讯。",
                    result.source_name,
                    len(result.articles),
                )
        return normalized

    async def fetch_cover_images(self, articles: list[Article]) -> dict[int, str]:
        """Fetch selected article pages and download their best safe cover image."""

        if not articles:
            return {}

        logger.info("ACG 日报：开始从 %d 条入选资讯的详情页补全封面。", len(articles))
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8",
        }
        connector = aiohttp.TCPConnector(resolver=PublicAddressResolver())
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            connector=connector,
        ) as session:
            images = await asyncio.gather(
                *(self._fetch_article_cover_image(session, article) for article in articles),
                return_exceptions=True,
            )

        result: dict[int, str] = {}
        detail_page_covers = 0
        fallback_covers = 0
        for article, image in zip(articles, images):
            if isinstance(image, tuple):
                result[article.id] = image[0]
                if image[1]:
                    detail_page_covers += 1
                else:
                    fallback_covers += 1
            elif isinstance(image, Exception):
                logger.info("ACG 日报：未使用资讯封面（%s）：%s", article.source, image)
        logger.info(
            "ACG 日报：封面补全完成，详情页封面 %d 张，列表或订阅源回退封面 %d 张，缺失 %d 张。",
            detail_page_covers,
            fallback_covers,
            len(articles) - len(result),
        )
        return result

    async def _fetch_article_cover_image(
        self,
        session: aiohttp.ClientSession,
        article: Article,
    ) -> tuple[str, bool]:
        cover_urls: list[tuple[str, bool]] = []
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                final_url, body, _content_type = await _read_source_response(session, article.url)
            except Exception as exc:
                last_error = exc
                logger.info(
                    "ACG 日报：详情页抓取失败（%s｜%s，第 %d/2 次）：%s",
                    article.source,
                    article.title,
                    attempt + 1,
                    exc,
                )
                if attempt == 0:
                    await asyncio.sleep(0.3)
                continue

            last_error = None
            detail_cover = _article_page_cover_url(body, final_url)
            if detail_cover:
                cover_urls.append((detail_cover, True))
                logger.info(
                    "ACG 日报：详情页找到封面候选（%s｜%s）。",
                    article.source,
                    article.title,
                )
            else:
                logger.info(
                    "ACG 日报：详情页未找到封面候选，将尝试列表或订阅源封面（%s｜%s）。",
                    article.source,
                    article.title,
                )
            break

        if article.cover_url and article.cover_url not in {url for url, _is_detail in cover_urls}:
            cover_urls.append((article.cover_url, False))
        for cover_url, is_detail_cover in cover_urls:
            for attempt in range(2):
                try:
                    return await self._fetch_cover_image(session, cover_url), is_detail_cover
                except Exception as exc:
                    last_error = exc
                    logger.info(
                        "ACG 日报：封面候选下载失败（%s｜%s，%s，%s，第 %d/2 次）：%s",
                        article.source,
                        article.title,
                        "详情页" if is_detail_cover else "列表或订阅源",
                        cover_url,
                        attempt + 1,
                        exc,
                    )
                    if attempt == 0:
                        await asyncio.sleep(0.3)

        if last_error is not None:
            raise last_error
        raise ValueError("article page and source entry do not provide a usable cover")

    async def _fetch_cover_image(
        self,
        session: aiohttp.ClientSession,
        current_url: str,
    ) -> str:
        for _ in range(MAX_REDIRECTS + 1):
            await validate_source_url(current_url)
            async with session.get(current_url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("cover redirect response has no Location header")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status != 200:
                    raise ValueError(f"cover HTTP {response.status} ({current_url})")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                    length = response.content_length if response.content_length is not None else "unknown"
                    raise ValueError(
                        "cover response has unsupported type "
                        f"{content_type or 'unknown'} ({length} bytes, {current_url})",
                    )
                if response.content_length is not None and response.content_length > MAX_IMAGE_BYTES:
                    raise ValueError(
                        "cover is "
                        f"{response.content_length} bytes and exceeds the 4 MiB limit ({current_url})",
                    )
                body = await _read_limited_body(response.content, MAX_IMAGE_BYTES, "cover")
                encoded = base64.b64encode(body).decode("ascii")
                return f"data:{content_type};base64,{encoded}"
        raise ValueError("too many cover redirects")

    async def _collect_one(
        self,
        session: aiohttp.ClientSession,
        source_url: str,
    ) -> SourceResult:
        source_name = source_url
        error = ""
        for attempt in range(2):
            try:
                final_url, body, content_type = await _read_source_response(session, source_url)
                source_name, articles = _feed_entries(
                    body,
                    final_url,
                    self.max_articles_per_source,
                )
                response_type = content_type.split(";", 1)[0].lower()
                looks_like_html = (
                    response_type in {"text/html", "application/xhtml+xml"}
                    or bool(re.search(br"<html(?:\s|>)", body[:4096], flags=re.IGNORECASE))
                )
                if not articles and looks_like_html:
                    source_name, articles = _html_entries(
                        body,
                        final_url,
                        self.max_articles_per_source,
                    )
                if articles:
                    return SourceResult(source_url, source_name, articles)
                error = f"no articles found (response {response_type or 'unknown'}, {len(body)} bytes)"
            except Exception as exc:
                error = str(exc) or type(exc).__name__

            if attempt == 0:
                logger.info("ACG 日报：资讯源首次未提取到条目，将重试（%s）：%s", source_url, error)
                await asyncio.sleep(0.5)

        return SourceResult(
            source_url,
            source_name or urlsplit(source_url).hostname or source_url,
            [],
            error,
        )
