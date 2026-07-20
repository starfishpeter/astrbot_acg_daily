"""Render a local HTML preview from the currently configured five news sources."""

from __future__ import annotations

import asyncio
import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from acg_daily.image_report import build_daily_image_html, normalize_cover_data_uri
from acg_daily.models import Article, DailyEdition, EditedItem
import aiohttp

from acg_daily.ranking import RANKING_SOURCES, RANKING_SOURCE_ANIME_PLANET, Ranking, RankingEntry
from acg_daily.scraper import NewsScraper, deduplicate_articles


SOURCE_URLS = [
    "https://myanimelist.net/rss/news.xml",
    "https://ln-news.com/feed",
    "https://animecorner.me/feed/",
    "https://www.animatetimes.com/index.php?p=1",
    "https://www.pashplus.jp/feed/",
]

# This is the local Agent selection for the sources fetched on 2026-07-21.
# URLs retain the original articles and keep the preview grounded in source facts.
SELECTION = {
    "https://www.pashplus.jp/anime/487524/": (
        "动画",
        "《Fate/kaleid liner 魔法少女☆伊莉雅 FINALE》确定2027年电视播出",
        "《Fate/stay night》衍生系列《魔法少女☆伊莉雅》的最终作 FINALE 确定于2027年作为电视动画播出。第二弹预告视觉图、首批主创与主要声优阵容同步公开，前作核心班底将继续参与制作。",
        "系列最终作的播出档期落定",
    ),
    "https://myanimelist.net/news/74495845?_location=rss": (
        "动画",
        "《Yurayura Q》漫画确定改编电视动画",
        "日活宣布天濑木度创作的超自然喜剧漫画《Yurayura Q》将制作电视动画。作品最初以短篇形式发表，2019年开始连载；正篇将在7月27日号完结，2027年还计划推出衍生作品。",
        "漫画完结前公布动画化企划",
    ),
    "https://myanimelist.net/news/74490039?_location=rss": (
        "动画",
        "《午夜心旋律》第二季公开新视觉图与追加声优",
        "电视动画《午夜心旋律》第二季公开第二张预告视觉图，并宣布小清水亚美与本渡枫将加入饰演山吹莉奈、山吹美都。新一季预计2027年播出，制作公司 Gekkou 继续参与。",
        "第二季阵容与档期同步更新",
    ),
    "https://myanimelist.net/news/74471698?_location=rss": (
        "动画",
        "《来自远方》电视动画公开主创阵容与首支预告",
        "冰川京子的少女漫画《来自远方》电视动画公开主声优、制作人员、预告视觉图及首支宣传影片。动画将于2026年10月5日在 Tokyo MX 首播，ABC TV 与 WOWOW 也将播出。",
        "经典少女漫画动画化进入宣传阶段",
    ),
    "https://animecorner.me/wuthering-waves-elysium-animated-series-officially-announced/": (
        "游戏",
        "《鸣潮》Elysium 动画系列正式公布",
        "库洛游戏旗下动画品牌 Kuro Onroad 正式公布《鸣潮》Elysium 动画系列。该项目将把游戏世界延伸至动画内容，后续制作信息与上线安排仍待官方继续公开。",
        "二次元游戏公布独立动画企划",
    ),
    "https://www.pashplus.jp/anime/487518/": (
        "动画",
        "《东京复仇者》三天战争篇确定由 JO1 演唱片头曲",
        "将于2026年10月开播的《东京复仇者》三天战争篇宣布片头曲采用 JO1 的新歌《IGNITE》。成员木全翔也与河野纯喜参与作词，作品还将在9月举行先行上映活动。",
        "新篇章主题曲与线下活动公开",
    ),
    "https://ln-news.com/articles/126140": (
        "轻小说",
        "Earth Star 将于9月创办新轻小说文艺品牌 Earth Star 文库",
        "Earth Star Entertainment 宣布旗下角色文艺品牌 Earth Star 文库将于2026年9月18日创刊。新品牌的首批作品与作者阵容尚未公布，后续将陆续公开详情。",
        "轻小说出版品牌新增动向",
    ),
    "https://myanimelist.net/news/74495692?_location=rss": (
        "漫画",
        "《奇诺之旅》作者时雨泽惠一开始连载原创漫画",
        "《奇诺之旅》作者时雨泽惠一与漫画家朝木隼人合作，在《周刊 CoroCoro Comic》网站开始连载原创漫画《Kemono-tachi no Peregrinatio》。这是时雨泽惠一首次担任原创漫画的原作作者。",
        "知名轻小说作者首次挑战原创漫画",
    ),
}


RANKING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

RANKING_TITLE_TRANSLATIONS = {
    "Mushoku Tensei: Jobless Reincarnation 3rd Season": "无职转生 ～到了异世界就拿出真本事～ 第三季",
    "You and I are Polar Opposites 2nd Season": "正反对的你与我 第二季",
    "One Piece": "航海王",
    "The World's Strongest Rearguard: Labyrinth Country's Novice Seeker": "世界最强后卫～迷宫国的新人探索者～",
    "Though I Am an Inept Villainess: Tale of the Butterfly-Rat Body Swap in the Maiden Court": "虽然是恶役千金，但我会凭借蝶鼠换身传说活下去",
    "Daemons of The Shadow Realm": "黄泉使者",
    "That Time I Got Reincarnated as a Slime Season 4": "关于我转生变成史莱姆这档事 第四季",
    "The 100 Girlfriends Who Really, Really, Really, Really, Really Love You Season 3": "超超超超超喜欢你的100个女朋友 第三季",
    "Sparks of Tomorrow": "明日的火花",
    "Black Torch": "黑色火炬",
}


def _fetch_ranking_with_curl(url: str, proxy: str) -> bytes:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl is None:
        raise RuntimeError("未找到 curl，无法用本地代理抓取 Anime-Planet 排行榜")
    result = subprocess.run(
        [
            curl,
            "--proxy",
            proxy,
            "--connect-timeout",
            "15",
            "--max-time",
            "30",
            "--http1.1",
            "--fail",
            "--silent",
            "--show-error",
            "-A",
            RANKING_HEADERS["User-Agent"],
            "-H",
            f"Accept: {RANKING_HEADERS['Accept']}",
            "-H",
            f"Accept-Language: {RANKING_HEADERS['Accept-Language']}",
            url,
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or "curl 请求失败")
    return result.stdout


async def _fetch_preview_ranking(proxy: str | None) -> Ranking:
    source = RANKING_SOURCES[RANKING_SOURCE_ANIME_PLANET]
    if proxy:
        body = await asyncio.to_thread(_fetch_ranking_with_curl, source.url, proxy)
    else:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout, headers=RANKING_HEADERS) as session:
            async with session.get(source.url) as response:
                response.raise_for_status()
                body = await response.read()
    entries = source.parser(body)
    if not entries:
        raise ValueError("未解析到排行榜条目")
    untranslated = [entry.title for entry in entries[:10] if entry.title not in RANKING_TITLE_TRANSLATIONS]
    if untranslated:
        raise ValueError("本地人工翻译缺少榜单标题：" + "、".join(untranslated))
    return Ranking(
        source.title,
        source.source,
        tuple(
            RankingEntry(entry.rank, RANKING_TITLE_TRANSLATIONS[entry.title], entry.detail)
            for entry in entries[:10]
        ),
    )


async def render_preview(proxy: str | None = None) -> tuple[Path, str]:
    scraper = NewsScraper(timeout_seconds=15, max_articles_per_source=10)
    results = await scraper.collect(SOURCE_URLS)
    articles = deduplicate_articles(
        [article for result in results for article in result.articles],
        max_candidates=40,
    )
    articles_by_url = {article.url: article for article in articles}
    missing_urls = [url for url in SELECTION if url not in articles_by_url]
    if missing_urls:
        raise RuntimeError("selected source articles are no longer present:\n" + "\n".join(missing_urls))

    selected = [articles_by_url[url] for url in SELECTION]
    edition = DailyEdition(
        "动画改编、新作档期与游戏动画企划集中更新，今天有不少值得群友补档关注的消息。",
        [
            EditedItem(article.id, *SELECTION[article.url])
            for article in selected
        ],
    )
    raw_covers = await scraper.fetch_cover_images(selected)
    covers: dict[int, str] = {}
    for article_id, cover in raw_covers.items():
        try:
            covers[article_id] = normalize_cover_data_uri(cover)
        except ValueError:
            pass
    try:
        ranking = await _fetch_preview_ranking(proxy)
        ranking_status = f"已附加 {ranking.source} {len(ranking.entries)} 条榜单"
    except Exception as exc:
        ranking = None
        ranking_status = f"排行榜已跳过：{exc}"

    html = build_daily_image_html(
        edition,
        articles,
        covers,
        datetime.now().astimezone().strftime("%Y 年 %m 月 %d 日"),
        f"本地人工 Agent 预览 / 抓取 {sum(bool(result.articles) for result in results)}/{len(results)} 个来源 / 筛选 {len(selected)} 条资讯",
        ranking=ranking,
    )
    output = PROJECT_ROOT / "preview" / "current-source-report.html"
    output.parent.mkdir(exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return output, ranking_status


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", help="optional HTTP(S) proxy for Anime-Planet ranking requests")
    args = parser.parse_args()
    output, ranking_status = asyncio.run(render_preview(args.proxy))
    print(output)
    print(ranking_status)


if __name__ == "__main__":
    main()
