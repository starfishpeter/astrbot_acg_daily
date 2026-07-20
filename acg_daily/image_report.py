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
    """Build a self-contained HTML digest for AstrBot's HTML-to-image renderer."""

    article_by_id = {article.id: article for article in articles}
    cards: list[str] = []
    for index, item in enumerate(edition.items, start=1):
        article = article_by_id.get(item.article_id)
        if article is None:
            continue
        cover = cover_images.get(article.id)
        if cover:
            media = f'<img src="{escape(cover, quote=True)}" alt="">'
        else:
            media = f'<div class="cover-placeholder">{escape(item.category[:8]) or "ACG"}</div>'
        reason = (
            f'<p class="reason">{escape(item.reason)}</p>'
            if item.reason
            else ""
        )
        cards.append(
            f"""
            <article class="card">
              <div class="cover">{media}</div>
              <div class="card-copy">
                <div class="meta"><span>{index:02d} / {escape(item.category)}</span><span>{escape(article.source)}</span></div>
                <h2>{escape(item.title)}</h2>
                <p class="summary">{escape(item.summary)}</p>
                {reason}
              </div>
            </article>
            """
        )

    if not cards:
        cards.append(
            """
            <article class="empty-card">
              <h2>今天暂未筛选出值得收录的 ACG 动态</h2>
              <p>资讯源仍会在下次命令执行时重新抓取。</p>
            </article>
            """
        )

    intro = escape(edition.intro or "为你整理今日值得关注的二次元动态。")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    color: #1d2431;
    background: #eef2f5;
    font-family: "Microsoft YaHei", "Noto Sans CJK SC", Arial, sans-serif;
  }}
  .sheet {{
    width: 960px;
    padding: 48px;
    background:
      radial-gradient(circle at 94% 0%, rgba(255, 204, 103, .48), transparent 250px),
      linear-gradient(135deg, #f9fbfc 0%, #edf2f5 100%);
  }}
  .masthead {{
    position: relative;
    overflow: hidden;
    padding: 34px 36px 30px;
    border-radius: 20px;
    color: #ffffff;
    background: linear-gradient(120deg, #1a2e47 0%, #274f68 56%, #376c6c 100%);
    box-shadow: 0 16px 30px rgba(29, 50, 71, .18);
  }}
  .masthead::after {{
    position: absolute;
    right: -35px;
    bottom: -65px;
    width: 210px;
    height: 210px;
    border: 22px solid rgba(255, 211, 105, .28);
    border-radius: 50%;
    content: "";
  }}
  .eyebrow {{
    margin: 0 0 8px;
    color: #ffdc85;
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 3px;
  }}
  h1 {{
    margin: 0;
    font-family: Georgia, "Times New Roman", "Microsoft YaHei", serif;
    font-size: 42px;
    letter-spacing: 1px;
  }}
  .date {{
    margin: 10px 0 0;
    color: #c6e6e1;
    font-size: 17px;
    letter-spacing: 1px;
  }}
  .intro {{
    margin: 30px 4px 23px;
    color: #3b4855;
    font-size: 19px;
    font-weight: 600;
    line-height: 1.65;
  }}
  .card {{
    display: flex;
    gap: 22px;
    margin-top: 16px;
    padding: 18px;
    break-inside: avoid;
    border: 1px solid #d8e0e5;
    border-radius: 16px;
    background: rgba(255, 255, 255, .92);
    box-shadow: 0 5px 14px rgba(40, 59, 77, .06);
  }}
  .cover {{
    flex: 0 0 178px;
    width: 178px;
    height: 126px;
    overflow: hidden;
    border-radius: 11px;
    background: #d8e4e6;
  }}
  .cover img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .cover-placeholder {{
    display: flex;
    width: 100%;
    height: 100%;
    align-items: center;
    justify-content: center;
    color: #f7fbfc;
    background:
      linear-gradient(135deg, rgba(255, 216, 119, .2) 25%, transparent 25%) -12px 0 / 24px 24px,
      linear-gradient(135deg, #315d71, #5e8b89);
    font-family: Georgia, "Microsoft YaHei", serif;
    font-size: 25px;
    font-weight: 700;
    letter-spacing: 2px;
  }}
  .card-copy {{ min-width: 0; flex: 1; }}
  .meta {{
    display: flex;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 5px;
    color: #638080;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: .4px;
  }}
  .meta span:last-child {{ color: #7a8690; font-weight: 500; text-align: right; }}
  h2 {{ margin: 0; color: #172b40; font-size: 20px; line-height: 1.4; }}
  .summary {{ margin: 7px 0 0; color: #4a5865; font-size: 15px; line-height: 1.55; }}
  .reason {{
    display: inline-block;
    margin: 8px 0 0;
    padding: 3px 8px;
    border-radius: 5px;
    color: #8a5d1d;
    background: #fff1cf;
    font-size: 12px;
    line-height: 1.35;
  }}
  .empty-card {{
    padding: 32px;
    border: 1px dashed #b9c7cf;
    border-radius: 16px;
    background: rgba(255, 255, 255, .7);
    text-align: center;
  }}
  .empty-card p {{ color: #5c6974; }}
  footer {{
    margin-top: 24px;
    color: #71808c;
    font-size: 13px;
    letter-spacing: .3px;
    text-align: center;
  }}
</style>
</head>
<body>
  <main class="sheet">
    <header class="masthead">
      <p class="eyebrow">ACG DAILY BRIEF</p>
      <h1>二次元日报</h1>
      <p class="date">{escape(date_text)}</p>
    </header>
    <p class="intro">{intro}</p>
    {''.join(cards)}
    <footer>{escape(source_status)} · 内容由已配置资讯源抓取并经 AI 整理</footer>
  </main>
</body>
</html>"""
