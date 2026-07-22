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


_EXCLUDED_IMAGE_REGION = re.compile(
    r"(?:^|[-_\s])(?:ad|ads|advert(?:isement)?|banner|sponsor|promo|nav|menu|header|footer|"
    r"sidebar|related|recommend|ranking|pickup|popular|widget|share|social|sns|comment|"
    r"pager|pagination|breadcrumb|tag-list|more-news|series-list|side-box|sidebox|"
    r"relation|kanren|osusume|ranking-list)(?:$|[-_\s])",
    re.IGNORECASE,
)
_NON_COVER_IMAGE_HINT = re.compile(
    r"(?:logo|icon|favicon|avatar|sprite|emoji|badge|button|pixel|tracking|1x1|"
    r"spacer|blank|transparent|share[-_]?|sns[-_]?|facebook|twitter|line\.me|"
    r"youtube|instagram|tiktok|qr[-_]?code)",
    re.IGNORECASE,
)


def _node_label(node: object) -> str:
    if not getattr(node, "name", None):
        return ""
    classes = " ".join(str(part) for part in node.get("class", []) or [])
    return f"{node.name} {classes} {node.get('id', '')} {node.get('role', '')}".lower()


def _is_excluded_image_region(node: object) -> bool:
    for ancestor in [node, *getattr(node, "parents", [])]:
        if not getattr(ancestor, "name", None):
            continue
        if ancestor.name in {"nav", "aside", "header", "footer"}:
            return True
        if _EXCLUDED_IMAGE_REGION.search(_node_label(ancestor)):
            return True
    return False


def _declared_image_edge(node: object) -> int:
    """Best-effort width/height from attributes, ignoring unit-less zero values."""

    edges: list[int] = []
    for attribute in ("width", "height"):
        raw = str(getattr(node, "get", lambda *_: None)(attribute) or "").strip().lower()
        match = re.match(r"(\d+)", raw)
        if match:
            edges.append(int(match.group(1)))
    style = str(getattr(node, "get", lambda *_: None)("style") or "")
    for key in ("width", "height"):
        match = re.search(rf"{key}\s*:\s*(\d+)", style, flags=re.IGNORECASE)
        if match:
            edges.append(int(match.group(1)))
    return max(edges) if edges else 0


def _is_probable_cover_url(url: str, node: object | None = None) -> bool:
    if not url or _NON_COVER_IMAGE_HINT.search(url):
        return False
    if node is not None:
        if _NON_COVER_IMAGE_HINT.search(_node_label(node)):
            return False
        edge = _declared_image_edge(node)
        if 0 < edge < 80:
            return False
        if _is_excluded_image_region(node):
            return False
    return True


def _image_url_from_node(node: object, source_url: str) -> str:
    if not getattr(node, "get", None):
        return ""
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


def _score_cover_candidate(url: str, node: object | None = None, *, base: int = 0) -> int:
    if not _is_probable_cover_url(url, node):
        return -1
    score = base
    if node is not None:
        edge = _declared_image_edge(node)
        if edge >= 400:
            score += 4
        elif edge >= 200:
            score += 2
        if node.name == "img" and any(parent.name == "figure" for parent in node.parents if getattr(parent, "name", None)):
            score += 2
        alt = clean_text(node.get("alt"), 80) if node.name == "img" else ""
        if alt:
            score += 1
    path = urlsplit(url).path.lower()
    if re.search(r"(?:cover|hero|main|eyecatch|thumbnail|thumb|ogp|opengraph)", path):
        score += 2
    return score


def _iter_image_nodes(value: object) -> list[object]:
    """Walk image-bearing tags while preserving parent context for region filters."""

    if hasattr(value, "find_all"):
        nodes: list[object] = []
        if getattr(value, "name", None):
            nodes.append(value)
        nodes.extend(value.find_all(True))
        return nodes
    return BeautifulSoup(str(value or ""), "html.parser").find_all(True)


def _best_image_url(value: object, source_url: str, *, base: int = 10) -> str:
    """Pick the strongest cover-like image inside a fragment instead of the first tag."""

    best_url = ""
    best_score = -1
    for index, node in enumerate(_iter_image_nodes(value)):
        url = _image_url_from_node(node, source_url)
        if not url:
            continue
        # Prefer earlier content images when scores are otherwise equal.
        score = _score_cover_candidate(url, node, base=base) - min(index, 20) // 10
        if score > best_score:
            best_score = score
            best_url = url
    return best_url if best_score >= 0 else ""


def _first_image_url(value: object, source_url: str) -> str:
    return _best_image_url(value, source_url)


def _main_content_image_url(soup: BeautifulSoup, source_url: str) -> str:
    """Find a likely article image in main content without accepting page chrome."""

    main = soup.select_one("main")
    if main is None:
        return ""
    return _best_image_url(main, source_url, base=6)


def _metadata_cover_url(soup: BeautifulSoup, source_url: str) -> str:
    for attrs in (
        {"property": "og:image"},
        {"property": "og:image:url"},
        {"name": "twitter:image"},
        {"name": "twitter:image:src"},
        {"itemprop": "image"},
    ):
        node = soup.find("meta", attrs=attrs)
        if not node:
            continue
        url = _image_url(node.get("content"), source_url)
        if url and _is_probable_cover_url(url):
            return url
    return ""


def _article_page_cover_url(body: bytes, source_url: str) -> str:
    """Prefer a scored article-body cover, then page metadata, then main content."""

    soup = BeautifulSoup(body, "html.parser")
    best_url = ""
    best_score = -1
    for selector in (
        "article",
        "main article",
        "[itemprop='articleBody']",
        ".entry-content",
        ".post-entry",
        ".post-content",
        ".article-content",
        ".article-body",
        "main figure",
        "article figure",
        "figure",
    ):
        for node in soup.select(selector):
            if _is_excluded_image_region(node):
                continue
            url = _best_image_url(node, source_url, base=12)
            if not url:
                continue
            score = _score_cover_candidate(url, base=12)
            if score > best_score:
                best_score = score
                best_url = url
            if best_score >= 12:
                return best_url

    if best_url:
        return best_url

    metadata = _metadata_cover_url(soup, source_url)
    if metadata:
        return metadata
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


def format_source_diagnostics(
    results: list[SourceResult],
    cover_counts: dict[str, int],
    deduplicated_count: int,
) -> str:
    """Summarize configured source extraction without exposing article links."""

    raw_count = sum(len(result.articles) for result in results)
    usable_count = sum(1 for result in results if result.articles)
    lines = [
        "ACG 日报订阅源诊断",
        f"配置 {len(results)} 个来源；可用 {usable_count} 个；原始资讯 {raw_count} 条；去重候选 {deduplicated_count} 条。",
    ]
    for result in results:
        raw_source_name = str(result.source_name or "")
        if not raw_source_name or raw_source_name == result.url or is_http_url(raw_source_name):
            source_name = urlsplit(result.url).hostname or result.url
        else:
            source_name = clean_text(raw_source_name, 80)
        if result.error:
            lines.append(f"- {source_name}：抓取失败（{clean_text(result.error, 160)}）")
            continue
        article_count = len(result.articles)
        sampled_cover_count = min(1, max(0, cover_counts.get(result.url, 0)))
        cover_status = "可下载" if sampled_cover_count else "不可下载"
        lines.append(f"- {source_name}：资讯 {article_count} 条；封面抽检（1 条）：{cover_status}。")
    return "\n".join(lines)


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

        logger.debug("ACG 日报：开始从 %d 条资讯的详情页补全封面。", len(articles))
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
                logger.debug("ACG 日报：未使用资讯封面（%s）：%s", article.source, image)
        logger.debug(
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
        last_error: Exception | None = None
        entry_cover = article.cover_url if _is_probable_cover_url(article.cover_url) else ""
        if article.cover_url and not entry_cover:
            logger.debug(
                "ACG 日报：跳过不像封面的列表或订阅源图片（%s｜%s，%s）。",
                article.source,
                article.title,
                article.cover_url,
            )
        if entry_cover:
            for attempt in range(2):
                try:
                    return await self._fetch_cover_image(session, entry_cover), False
                except Exception as exc:
                    last_error = exc
                    logger.debug(
                        "ACG 日报：列表或订阅源封面下载失败（%s｜%s，%s，第 %d/2 次）：%s",
                        article.source,
                        article.title,
                        entry_cover,
                        attempt + 1,
                        exc,
                    )
                    if attempt == 0:
                        await asyncio.sleep(0.3)

        detail_cover = ""
        for attempt in range(2):
            try:
                final_url, body, _content_type = await _read_source_response(session, article.url)
            except Exception as exc:
                last_error = exc
                logger.debug(
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
                logger.debug(
                    "ACG 日报：详情页找到封面候选（%s｜%s）。",
                    article.source,
                    article.title,
                )
            else:
                logger.debug(
                    "ACG 日报：详情页未找到封面候选，将结束封面尝试（%s｜%s）。",
                    article.source,
                    article.title,
                )
            break

        if detail_cover:
            for attempt in range(2):
                try:
                    return await self._fetch_cover_image(session, detail_cover), True
                except Exception as exc:
                    last_error = exc
                    logger.debug(
                        "ACG 日报：详情页封面下载失败（%s｜%s，%s，第 %d/2 次）：%s",
                        article.source,
                        article.title,
                        detail_cover,
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
