from __future__ import annotations

from html import escape

from .models import Article, DailyEdition


def build_daily_image_html(
    edition: DailyEdition,
    articles: list[Article],
    cover_images: dict[int, str],
    date_text: str,
    source_status: str,
) -> str:
    """Build an editorial ACG news sheet for AstrBot's HTML-to-image renderer."""

    article_by_id = {article.id: article for article in articles}
    selected = [
        (index, item, article_by_id[item.article_id])
        for index, item in enumerate(edition.items, start=1)
        if item.article_id in article_by_id
    ]

    def media(article: Article, category: str, featured: bool = False) -> str:
        cover = cover_images.get(article.id)
        if cover:
            return f'<img src="{escape(cover, quote=True)}" alt="">'
        size = " cover-placeholder-featured" if featured else ""
        return (
            f'<div class="cover-placeholder{size}">'
            f'<span class="placeholder-kicker">ACG NEWS</span>'
            f'<strong>{escape(category[:8]) or "ACG"}</strong>'
            "<span class=\"placeholder-mark\">01</span>"
            "</div>"
        )

    lead = ""
    cards: list[str] = []
    for index, item, article in selected:
        item_media = media(article, item.category, featured=index == 1)
        reason = (
            f'<p class="reason"><b>编辑注记</b>{escape(item.reason)}</p>'
            if item.reason
            else ""
        )
        metadata = (
            f'<div class="story-meta"><span class="story-index">{index:02d}</span>'
            f'<span class="category">{escape(item.category)}</span>'
            f'<span class="source">{escape(article.source)}</span></div>'
        )
        if index == 1:
            lead = f"""
            <article class="lead-story">
              <div class="lead-media">{item_media}</div>
              <div class="lead-copy">
                <div class="lead-flag"><span>TOP STORY</span><span>{escape(article.source)}</span></div>
                <div class="lead-category">{escape(item.category)}</div>
                <h2>{escape(item.title)}</h2>
                <p class="lead-summary">{escape(item.summary)}</p>
                {reason}
              </div>
            </article>
            """
            continue
        cards.append(
            f"""
            <article class="story-card">
              <div class="card-media">{item_media}</div>
              <div class="card-copy">
                {metadata}
                <h3>{escape(item.title)}</h3>
                <p class="summary">{escape(item.summary)}</p>
                {reason}
              </div>
            </article>
            """
        )

    if not lead:
        lead = """
        <article class="empty-state">
          <p class="empty-eyebrow">NO FEATURED STORY</p>
          <h2>今天暂未筛选出值得收录的 ACG 动态</h2>
          <p>资讯源会在下次命令执行时重新抓取。</p>
        </article>
        """

    intro = escape(edition.intro or "为你整理今日值得关注的二次元动态。")
    count = len(selected)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  :root {{
    --ink: #1c2733;
    --muted: #667684;
    --paper: #f8f7f2;
    --line: #d8ddd9;
    --blue: #1d5267;
    --blue-deep: #12394d;
    --coral: #e87557;
    --gold: #edc86a;
    --mint: #b9d9d1;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    color: var(--ink);
    background: #e9edef;
    font-family: "Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC", Arial, sans-serif;
  }}
  .sheet {{
    width: 1080px;
    min-height: 100%;
    padding: 34px;
    background:
      radial-gradient(circle at 100% 0%, rgba(237, 200, 106, .38), transparent 300px),
      linear-gradient(135deg, #f9faf7 0%, #edf1f0 100%);
  }}
  .masthead {{
    position: relative;
    overflow: hidden;
    min-height: 230px;
    padding: 29px 34px 32px;
    color: #fff;
    background:
      linear-gradient(118deg, rgba(16, 53, 72, .98), rgba(30, 86, 102, .92)),
      repeating-linear-gradient(135deg, transparent 0 18px, rgba(255,255,255,.04) 18px 19px);
    border-radius: 22px;
    box-shadow: 0 16px 32px rgba(24, 58, 76, .18);
  }}
  .masthead::before {{
    position: absolute;
    right: -82px;
    bottom: -106px;
    width: 310px;
    height: 310px;
    border: 34px solid rgba(237, 200, 106, .32);
    border-radius: 50%;
    content: "";
  }}
  .masthead::after {{
    position: absolute;
    right: 106px;
    bottom: -75px;
    width: 154px;
    height: 154px;
    border: 1px solid rgba(255,255,255,.25);
    border-radius: 50%;
    content: "";
  }}
  .folio {{
    display: flex;
    justify-content: space-between;
    gap: 18px;
    color: #f1d98f;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 2.8px;
  }}
  h1 {{
    position: relative;
    z-index: 1;
    margin: 23px 0 6px;
    font-family: Georgia, "Songti SC", "Microsoft YaHei", serif;
    font-size: 53px;
    letter-spacing: 2px;
    line-height: 1.05;
  }}
  .subhead {{
    position: relative;
    z-index: 1;
    max-width: 610px;
    margin: 12px 0 0;
    color: #d3e5e4;
    font-size: 16px;
    line-height: 1.6;
  }}
  .edition {{
    position: absolute;
    right: 35px;
    bottom: 25px;
    z-index: 1;
    padding: 8px 12px;
    border: 1px solid rgba(255,255,255,.35);
    color: #fff;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 1.2px;
  }}
  .intro {{
    margin: 25px 8px 22px;
    padding: 0 0 0 16px;
    border-left: 5px solid var(--coral);
    color: #354551;
    font-family: Georgia, "Songti SC", "Microsoft YaHei", serif;
    font-size: 21px;
    font-weight: 700;
    line-height: 1.58;
  }}
  .lead-story {{
    display: grid;
    grid-template-columns: 1.06fr .94fr;
    overflow: hidden;
    min-height: 346px;
    border: 1px solid var(--line);
    border-radius: 18px;
    background: #fff;
    box-shadow: 0 10px 22px rgba(30, 51, 63, .08);
    break-inside: avoid;
  }}
  .lead-media {{
    min-width: 0;
    min-height: 346px;
    overflow: hidden;
    background: #c8d9d9;
  }}
  .lead-media img, .card-media img {{
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }}
  .lead-copy {{
    display: flex;
    flex-direction: column;
    padding: 27px 29px 24px;
    background:
      linear-gradient(135deg, rgba(237, 200, 106, .16), transparent 42%),
      #fff;
  }}
  .lead-flag, .story-meta {{
    display: flex;
    align-items: center;
    gap: 9px;
    min-width: 0;
    color: var(--muted);
    font-size: 12px;
  }}
  .lead-flag {{
    justify-content: space-between;
    color: var(--blue);
    font-weight: 800;
    letter-spacing: 1.2px;
  }}
  .lead-flag span:last-child {{
    max-width: 195px;
    overflow: hidden;
    color: var(--muted);
    font-weight: 500;
    letter-spacing: 0;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .lead-category {{
    width: fit-content;
    margin-top: 29px;
    padding: 4px 9px;
    color: #8a542a;
    background: #fff0d3;
    font-size: 12px;
    font-weight: 700;
  }}
  .lead-copy h2 {{
    margin: 12px 0 0;
    color: #1d2d39;
    font-size: 27px;
    line-height: 1.32;
  }}
  .lead-summary {{
    margin: 12px 0 0;
    color: #4c5b65;
    font-size: 15px;
    line-height: 1.68;
  }}
  .story-grid {{
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 18px;
    margin-top: 18px;
  }}
  .story-card {{
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 16px;
    background: rgba(255,255,255,.94);
    box-shadow: 0 6px 16px rgba(30, 51, 63, .06);
    break-inside: avoid;
  }}
  .card-media {{
    width: 100%;
    height: 204px;
    overflow: hidden;
    background: #c8d9d9;
  }}
  .card-copy {{ padding: 17px 18px 19px; }}
  .story-meta {{ margin-bottom: 9px; }}
  .story-index {{
    color: var(--blue);
    font-weight: 800;
    letter-spacing: .6px;
  }}
  .category {{
    padding-left: 9px;
    border-left: 1px solid #b7c3c6;
    color: #587d79;
    font-weight: 700;
  }}
  .source {{
    min-width: 0;
    margin-left: auto;
    overflow: hidden;
    color: #818d95;
    text-align: right;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  h3 {{
    margin: 0;
    color: #1c2c38;
    font-size: 19px;
    line-height: 1.42;
  }}
  .summary {{
    margin: 9px 0 0;
    color: #53616b;
    font-size: 14px;
    line-height: 1.65;
  }}
  .reason {{
    margin: 12px 0 0;
    padding: 7px 9px;
    border-left: 3px solid var(--gold);
    color: #79623a;
    background: #fff8e8;
    font-size: 12px;
    line-height: 1.48;
  }}
  .reason b {{ margin-right: 7px; color: #a47b2c; font-weight: 800; }}
  .cover-placeholder {{
    position: relative;
    display: flex;
    width: 100%;
    height: 100%;
    min-height: 204px;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    color: #f4fbf9;
    background:
      linear-gradient(135deg, rgba(255,255,255,.12) 25%, transparent 25%) -15px 0 / 30px 30px,
      linear-gradient(135deg, #194b63, #43817a 62%, #8da89b);
  }}
  .cover-placeholder::after {{
    position: absolute;
    inset: 12px;
    border: 1px solid rgba(255,255,255,.3);
    content: "";
  }}
  .cover-placeholder-featured {{ min-height: 346px; font-size: 39px; }}
  .cover-placeholder strong {{
    position: relative;
    z-index: 1;
    padding: 0 18px;
    font-family: Georgia, "Microsoft YaHei", serif;
    font-size: 28px;
    letter-spacing: 3px;
    text-align: center;
  }}
  .cover-placeholder-featured strong {{ font-size: 43px; }}
  .placeholder-kicker {{
    position: absolute;
    top: 23px;
    left: 24px;
    color: #f0ce77;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 2px;
  }}
  .placeholder-mark {{
    position: absolute;
    right: 21px;
    bottom: 7px;
    color: rgba(255,255,255,.17);
    font-family: Georgia, serif;
    font-size: 68px;
    font-weight: 700;
  }}
  .empty-state {{
    padding: 55px 30px;
    border: 1px dashed #b7c3c6;
    border-radius: 18px;
    background: rgba(255,255,255,.68);
    text-align: center;
  }}
  .empty-eyebrow {{ color: #61817b; font-size: 12px; font-weight: 800; letter-spacing: 2px; }}
  .empty-state h2 {{ margin: 10px 0; color: var(--ink); font-size: 25px; }}
  .empty-state p:last-child {{ color: var(--muted); }}
  footer {{
    display: flex;
    justify-content: space-between;
    gap: 20px;
    margin: 25px 7px 2px;
    padding-top: 16px;
    border-top: 1px solid #ccd4d4;
    color: #74818a;
    font-size: 12px;
    line-height: 1.5;
  }}
  .footer-brand {{ color: var(--blue); font-weight: 800; letter-spacing: .8px; }}
</style>
</head>
<body>
  <main class="sheet">
    <header class="masthead">
      <div class="folio"><span>ACG VISUAL DESK</span><span>{escape(date_text)}</span></div>
      <h1>二次元情报局</h1>
      <p class="subhead">{escape(source_status)}</p>
      <div class="edition">DAILY EDITION / {count:02d}</div>
    </header>
    <p class="intro">{intro}</p>
    {lead}
    <section class="story-grid">{''.join(cards)}</section>
    <footer><span class="footer-brand">ACG DAILY BRIEF</span><span>内容由已配置资讯源抓取并经 AI 整理，仅展示来源</span></footer>
  </main>
</body>
</html>"""
