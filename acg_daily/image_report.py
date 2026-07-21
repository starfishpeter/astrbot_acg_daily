from __future__ import annotations

import base64
import io
import re
import threading
import warnings
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import escape
from pathlib import Path

from PIL import Image, ImageFile, ImageOps

from .models import Article, DailyEdition, EditedItem
from .ranking import Ranking


TEMPLATE_DIR = Path(__file__).with_name("templates") / "visual_desk"
MAX_COVER_SIZE = (960, 720)
MAX_COVER_PIXELS = 24_000_000
_TRUNCATED_IMAGE_LOCK = threading.Lock()


def _template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


def _render(template: str, **values: str) -> str:
    for name, value in values.items():
        template = template.replace(f"{{{{{name}}}}}", value)
    return template


def _truncate(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else f"{value[:limit].rstrip()}..."


def _publication_date(value: str) -> str:
    """Display a source timestamp as a compact server-local calendar date."""

    if not value:
        return "日期未知"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            match = re.search(r"\b20\d{2}\s*(?:[-./年])\s*\d{1,2}\s*(?:[-./月])\s*\d{1,2}", value)
            return match.group(0) if match else "日期未知"
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d")


def normalize_cover_data_uri(cover: str) -> str:
    """Convert a downloaded cover into a bounded JPEG data URI for Chromium."""

    if not cover.startswith("data:") or "," not in cover:
        raise ValueError("cover is not a data URI")
    header, payload = cover.split(",", 1)
    if ";base64" not in header.lower():
        raise ValueError("cover data URI is not base64 encoded")

    try:
        image_bytes = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise ValueError("cover data URI has invalid base64") from exc

    with _TRUNCATED_IMAGE_LOCK:
        previous_truncated_images = ImageFile.LOAD_TRUNCATED_IMAGES
        try:
            ImageFile.LOAD_TRUNCATED_IMAGES = True
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(image_bytes)) as source:
                    if source.width * source.height > MAX_COVER_PIXELS:
                        raise ValueError("cover dimensions exceed the 24 megapixel limit")
                    image = ImageOps.exif_transpose(source).convert("RGBA")
                    if image.getchannel("A").getextrema()[0] < 255:
                        background = Image.new("RGB", image.size, "#f3f7fb")
                        background.paste(image, mask=image.getchannel("A"))
                        image = background
                    else:
                        image = image.convert("RGB")
                    image.thumbnail(MAX_COVER_SIZE, Image.Resampling.LANCZOS)
                    output = io.BytesIO()
                    image.save(output, format="JPEG", quality=82, optimize=True)
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous_truncated_images

    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _placeholder(category: str, featured: bool) -> str:
    size = " cover-placeholder-featured" if featured else ""
    return (
        f'<div class="cover-placeholder{size}">'
        '<span class="cover-orbit orbit-one"></span>'
        '<span class="cover-orbit orbit-two"></span>'
        '<span class="cover-kicker">NO IMAGE / ACG DESK</span>'
        f"<strong>{escape(_truncate(category, 10) or 'ACG')}</strong>"
        '<span class="cover-code">VISUAL SIGNAL LOST</span>'
        "</div>"
    )


def _story_card(
    template: str,
    index: int,
    item: EditedItem,
    article: Article,
    cover_images: dict[int, str],
    featured: bool,
) -> str:
    cover = cover_images.get(article.id)
    media = (
        f'<img class="cover-image" src="{escape(cover, quote=True)}" alt="">'
        if cover
        else _placeholder(item.category, featured)
    )
    source = _truncate(article.source, 28)
    reason = (
        '<div class="story-note"><span>编辑关注</span>'
        f"{escape(_truncate(item.reason, 42))}</div>"
        if item.reason
        else ""
    )
    return _render(
        template,
        CARD_KIND=("feature" if featured else "standard") + (" with-cover" if cover else " without-cover"),
        ACCENT=str((index - 1) % 4 + 1),
        INDEX=f"{index:02d}",
        CATEGORY=escape(_truncate(item.category, 12)),
        SOURCE=escape(source),
        PUBLISHED=escape(_publication_date(article.published_at)),
        TITLE=escape(_truncate(item.title, 58 if featured else 52)),
        SUMMARY=escape(_truncate(item.summary, 112 if featured else 82)),
        MEDIA=media,
        REASON=reason,
    )


def _ranking_block(ranking: Ranking | None) -> str:
    if ranking is None:
        return ""
    rows = "".join(
        "<li>"
        f"<b>{entry.rank:02d}</b>"
        f"<strong>{escape(_truncate(entry.title, 42))}</strong>"
        f"<span>{escape(_truncate(entry.detail, 22))}</span>"
        "</li>"
        for entry in ranking.entries[:10]
    )
    return (
        '<section class="ranking-block">'
        '<div class="ranking-header"><span>RANKING SIGNAL</span>'
        f"<h2>{escape(ranking.title)}</h2><p>{escape(ranking.source)}</p></div>"
        f"<ol>{rows}</ol></section>"
    )


def build_daily_image_html(
    edition: DailyEdition,
    articles: list[Article],
    cover_images: dict[int, str],
    date_text: str,
    source_status: str,
    ranking: Ranking | None = None,
) -> str:
    """Render one readable ACG news page from a theme and story-card template.

    Keeping the page shell and news-card markup in separate files follows the
    themed-template structure used by the reference daily-analysis plugin.
    """

    article_by_id = {article.id: article for article in articles}
    story_card_template = _template("story_card.html")
    selected = [
        (offset + 1, item, article_by_id[item.article_id])
        for offset, item in enumerate(edition.items)
        if item.article_id in article_by_id
    ]

    if selected:
        first_index, first_item, first_article = selected[0]
        feature = _story_card(
            story_card_template,
            first_index,
            first_item,
            first_article,
            cover_images,
            featured=True,
        )
        stories = "".join(
            _story_card(story_card_template, index, item, article, cover_images, featured=False)
            for index, item, article in selected[1:]
        )
    else:
        feature = """
        <section class="empty-state">
          <span>NO STORY SELECTED</span>
          <h2>今天暂未筛选出值得收录的 ACG 动态</h2>
          <p>资讯源会在下次命令执行时重新抓取。</p>
        </section>
        """
        stories = ""

    intro = edition.intro or "把值得聊的动画、漫画、轻小说和二次元动态放进今天的编辑台。"
    return _render(
        _template("page.html"),
        DATE=escape(date_text),
        SOURCE_STATUS=escape(source_status),
        INTRO=escape(_truncate(intro, 78)),
        FEATURE=feature,
        STORIES=stories,
        RANKING=_ranking_block(ranking),
    )
