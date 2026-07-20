"""Generate a browser-previewable ACG daily image report without AstrBot."""

from __future__ import annotations

import base64
import io
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from acg_daily.image_report import build_daily_image_html, normalize_cover_data_uri
from acg_daily.models import Article, DailyEdition, EditedItem


def _sample_cover(color: str, label: str) -> str:
    image = Image.new("RGB", (960, 720), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((42, 42, 918, 678), outline="#17243c", width=18)
    draw.ellipse((150, 120, 810, 780), outline="#ffd84e", width=38)
    draw.text((95, 90), label, fill="#ffffff", stroke_width=2, stroke_fill="#17243c")
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=85)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return normalize_cover_data_uri(f"data:image/jpeg;base64,{encoded}")


def main() -> None:
    articles = [
        Article(
            1,
            "动画新作公开主视觉图与 2027 年播出档期",
            "制作委员会公布首张主视觉图，并确认播出时间与核心制作阵容。",
            "https://example.com/anime",
            "Anime Trending",
        ),
        Article(
            2,
            "知名声优公布重要公开动态",
            "相关公告在社群引发讨论，官方感谢长期支持的听众与粉丝。",
            "https://example.com/seiyuu",
            "Anime Corner",
        ),
        Article(
            3,
            "轻小说改编企划公开制作团队",
            "原作系列将启动新媒体企划，更多详情将在后续活动公开。",
            "https://example.com/novel",
            "LN News",
        ),
        Article(
            4,
            "经典漫画海外版确定发售时间",
            "出版社公布精装合辑的发行安排，收录内容与规格同步公开。",
            "https://example.com/manga",
            "Anime News Network",
        ),
    ]
    edition = DailyEdition(
        "新作播出、声优动态与出版消息集中公开，今天的 ACG 编辑台关注作品进展与产业动向。",
        [
            EditedItem(1, "动画", "动画新作公开主视觉图，确认 2027 年播出", articles[0].summary, "新作档期与核心阵容同步公布"),
            EditedItem(2, "声优", "知名声优公布重要公开动态", articles[1].summary, "ACG 社群高度关注的公开消息"),
            EditedItem(3, "轻小说", "轻小说改编企划公开制作团队", articles[2].summary, "改编项目进入新阶段"),
            EditedItem(4, "漫画", "经典漫画海外版确定发售时间", articles[3].summary, "海外出版与版权发行动态"),
        ],
    )
    covers = {
        1: _sample_cover("#235aa6", "ANIME / 01"),
        2: _sample_cover("#d95b86", "SEIYUU / 02"),
        3: _sample_cover("#22876c", "NOVEL / 03"),
        4: _sample_cover("#8a5ac2", "MANGA / 04"),
    }
    html = build_daily_image_html(
        edition,
        articles,
        covers,
        datetime.now().strftime("%Y 年 %m 月 %d 日"),
        "本地预览样例 / 4 条资讯 / 未调用 AstrBot 文转图",
    )
    output = PROJECT_ROOT / "preview" / "daily-report-preview.html"
    output.parent.mkdir(exist_ok=True)
    output.write_text(html, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
