from __future__ import annotations

import asyncio
import ipaddress
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
MAX_REDIRECTS = 3
USER_AGENT = "AstrBot-ACG-Daily/0.1 (+https://github.com/starfishpeter/astrbot_acg_daily)"
TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref"}


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
    address = ipaddress.ip_address(value)
    return not any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        ),
    )


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
            body = await response.content.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds the 2 MiB limit")
            return current_url, body, response.headers.get("Content-Type", "")
    raise ValueError("too many redirects")


def _feed_value(entry: object, name: str, default: object = "") -> object:
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _feed_entries(body: bytes, source_url: str, limit: int) -> tuple[str, list[Article]]:
    feed = feedparser.parse(body)
    entries = list(getattr(feed, "entries", []) or [])
    version = str(getattr(feed, "version", "") or "")
    if not version or not entries:
        return "", []

    feed_info = getattr(feed, "feed", {})
    source_name = clean_text(_feed_value(feed_info, "title"), 100)
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


def _article_from_container(
    container,
    source_url: str,
    source_name: str,
) -> Article | None:
    heading = container.find(["h1", "h2", "h3", "h4"])
    anchor = heading.find("a", href=True) if heading else None
    if anchor is None:
        anchor = container.find("a", href=True)
    if anchor is None:
        return None
    title = clean_text(heading or anchor, 200)
    link = canonical_url(urljoin(source_url, str(anchor.get("href", ""))))
    if not title or not link:
        return None

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
    return Article(0, title, summary, link, source_name, published_at)


def _html_entries(body: bytes, source_url: str, limit: int) -> tuple[str, list[Article]]:
    soup = BeautifulSoup(body, "html.parser")
    source_name = _source_name(soup, source_url)
    containers = soup.find_all("article")
    if not containers:
        containers = soup.select(
            ".news-item, .post, .post-item, .article-item, .news-unit",
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

    # Some sites have bare headline lists instead of article cards.
    for heading in soup.find_all(["h2", "h3", "h4"]):
        anchor = heading.find("a", href=True)
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
    """Deduplicate URL/title matches and assign stable IDs for the editor."""

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
    return [
        Article(index, item.title, item.summary, item.url, item.source, item.published_at)
        for index, item in enumerate(unique, start=1)
    ]


class NewsScraper:
    def __init__(self, timeout_seconds: int, max_articles_per_source: int) -> None:
        self.timeout_seconds = max(1, min(timeout_seconds, 30))
        self.max_articles_per_source = max(1, min(max_articles_per_source, 20))

    async def collect(self, urls: list[str]) -> list[SourceResult]:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, text/html, "
            "application/xhtml+xml;q=0.9, */*;q=0.1",
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
        return normalized

    async def _collect_one(
        self,
        session: aiohttp.ClientSession,
        source_url: str,
    ) -> SourceResult:
        try:
            final_url, body, _content_type = await _read_source_response(session, source_url)
            source_name, articles = _feed_entries(
                body,
                final_url,
                self.max_articles_per_source,
            )
            if not articles:
                source_name, articles = _html_entries(
                    body,
                    final_url,
                    self.max_articles_per_source,
                )
            if not articles:
                return SourceResult(source_url, source_name or source_url, [], "no articles found")
            return SourceResult(source_url, source_name, articles)
        except Exception as exc:
            return SourceResult(
                source_url,
                urlsplit(source_url).hostname or source_url,
                [],
                str(exc),
            )
