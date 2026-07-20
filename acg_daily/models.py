from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Article:
    """A normalized item extracted from one configured news source."""

    id: int
    title: str
    summary: str
    url: str
    source: str
    published_at: str = ""
    cover_url: str = ""


@dataclass(frozen=True)
class EditedItem:
    """A model-selected article whose source fields remain plugin controlled."""

    article_id: int
    category: str
    title: str
    summary: str
    reason: str


@dataclass(frozen=True)
class DailyEdition:
    """The validated editorial result used to create the image digest."""

    intro: str
    items: list[EditedItem]
