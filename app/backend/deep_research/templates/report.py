"""
研究報告樣板：單一 HTML 檔，離線可開、列印即成 PDF。

版面取自長篇報導的作法：封面區、摘要卡、關鍵數字帶、編號小節、
側欄註記（callout）、參考來源。主題（色票 + 字體配對）由 `themes.py` 提供。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List

from .common import esc, markdown_to_html
from .themes import Theme

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font-body);background:var(--stage);color:var(--body);
     line-height:1.72;-webkit-font-smoothing:antialiased}
.page{max-width:880px;margin:0 auto;background:var(--bg);min-height:100vh;
      padding:0 0 96px;box-shadow:0 1px 3px rgba(16,24,40,.05),0 20px 70px rgba(16,24,40,.08)}
.masthead{padding:74px 78px 30px;border-bottom:1px solid var(--line);position:relative}
.masthead::before{content:"";position:absolute;left:0;top:0;width:6px;height:100%;
                  background:var(--accent)}
.kicker{display:inline-flex;align-items:center;gap:7px;color:var(--accent);font-weight:700;
        letter-spacing:.14em;text-transform:uppercase;font-size:11.5px}
.kicker.pill{background:var(--accent-soft);border:1px solid var(--accent-line);
             border-radius:999px;padding:5px 12px}
.kicker.underline{padding-bottom:3px;border-bottom:2px solid var(--accent)}
h1{font-family:var(--font-display);font-size:42px;line-height:1.14;letter-spacing:-.028em;
   font-weight:var(--h-weight);color:var(--ink);margin:20px 0 12px;max-width:24ch}
.subtitle{font-size:19px;color:var(--ink2);font-weight:450;line-height:1.55;max-width:56ch}
.meta{margin-top:26px;display:flex;flex-wrap:wrap;gap:8px 30px;font-size:12.5px;
      color:var(--muted)}
.meta b{color:var(--ink2);font-weight:600;letter-spacing:.05em;text-transform:uppercase;
        font-size:11px;display:block;margin-bottom:3px}
.body-wrap{padding:0 78px}
h2{font-family:var(--font-display);font-size:25px;line-height:1.28;letter-spacing:-.016em;
   font-weight:var(--h-weight);color:var(--ink);margin:58px 0 8px;
   display:flex;align-items:baseline;gap:13px}
h2 .sec-no{flex:0 0 auto;font-size:13px;font-weight:800;color:var(--accent);
           letter-spacing:.06em;font-family:var(--font-body);
           border-bottom:2px solid var(--accent-line);padding-bottom:2px}
h3{font-family:var(--font-display);font-size:18px;font-weight:var(--h-weight);
   color:var(--ink2);margin:30px 0 8px}
p{margin:13px 0}
ul,ol{margin:13px 0 13px 24px}
li{margin:7px 0}
a{color:var(--accent);text-decoration:none;border-bottom:1px solid var(--accent-line);
  word-break:break-word}
a:hover{border-bottom-color:var(--accent)}
strong{color:var(--ink);font-weight:700}
code{font-family:var(--font-mono);font-size:.88em;background:var(--bg-soft);
     border:1px solid var(--line);border-radius:4px;padding:1px 6px;color:var(--ink2)}
table{width:100%;border-collapse:collapse;margin:20px 0;font-size:14.5px}
th,td{border-bottom:1px solid var(--line);padding:11px 13px;text-align:left;
      vertical-align:top}
th{background:var(--bg-soft);color:var(--ink2);font-weight:650;
   border-bottom:2px solid var(--line-strong);font-size:13px;letter-spacing:.03em}
tr:last-child td{border-bottom:1px solid var(--line-strong)}

/* 摘要 */
.summary{margin-top:16px;background:var(--bg-soft);border-radius:var(--radius);
         padding:28px 32px;border-left:4px solid var(--accent)}
.summary ol{margin:0 0 0 20px}
.summary li{margin:12px 0;color:var(--ink2);font-size:16.5px;line-height:1.66}
.summary li::marker{color:var(--accent);font-weight:700}

/* 關鍵數字 */
.figures{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:0;
         margin-top:32px;border-top:1px solid var(--line-strong);
         border-bottom:1px solid var(--line-strong)}
.figure{padding:22px 22px 22px 0;border-right:1px solid var(--line)}
.figure:last-child{border-right:0}
.figure .value{font-family:var(--font-display);font-size:31px;font-weight:800;color:var(--ink);
               letter-spacing:-.03em;line-height:1.08}
.figure .value.long{font-size:17px;font-weight:700;letter-spacing:0;line-height:1.45}
.figure .label{margin-top:8px;font-size:12.5px;color:var(--muted);line-height:1.5}

/* 側欄註記 */
.callout{margin:20px 0 0;border-left:3px solid var(--accent);background:var(--accent-soft);
         border-radius:0 var(--radius) var(--radius) 0;padding:15px 20px;font-size:15.5px;
         color:var(--ink2);line-height:1.6}
.callout::before{content:"重點";display:block;font-size:10.5px;letter-spacing:.14em;
                 text-transform:uppercase;color:var(--accent);font-weight:700;
                 margin-bottom:5px}
.open-questions{list-style:none;margin-left:0}
.open-questions li{position:relative;padding-left:28px;margin:13px 0}
.open-questions li::before{content:"?";position:absolute;left:0;top:1px;width:19px;height:19px;
  border-radius:50%;border:1.5px solid var(--accent);color:var(--accent);font-size:11px;
  font-weight:800;display:flex;align-items:center;justify-content:center}

/* 參考來源 */
.refs{margin:14px 0 0;padding:0;list-style:none;counter-reset:ref}
.refs li{counter-increment:ref;position:relative;padding-left:38px;margin:13px 0;
         font-size:14.5px}
.refs li::before{content:counter(ref,decimal-leading-zero);position:absolute;left:0;top:2px;
  color:var(--accent);font-size:12px;font-weight:800;font-family:var(--font-mono)}
.refs .ref-url{display:block;color:var(--muted);font-size:12px;word-break:break-all;
               margin-top:2px}
footer{margin:64px 78px 0;padding-top:22px;border-top:1px solid var(--line);
       font-size:12.5px;color:var(--muted);line-height:1.65}

@media print{
  body{background:#fff}
  .page{box-shadow:none;max-width:none;padding:0}
  .masthead,.body-wrap,footer{padding-left:0;padding-right:0;margin-left:0;margin-right:0}
  h2{break-after:avoid}
  .figure,.summary,.callout,table{break-inside:avoid}
}
@media (max-width:760px){
  .masthead{padding:44px 24px 24px}
  .body-wrap{padding:0 24px}
  footer{margin:44px 24px 0}
  h1{font-size:30px}
  .figure{border-right:0;border-bottom:1px solid var(--line)}
}
"""


def render_report(doc: Any, *, model: str, query: str, theme: Theme) -> str:
    """把 `ReportDoc` 轉成完整 HTML 字串。"""
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    kicker_style = theme.traits.get("kicker", "pill")
    kicker_cls = f"kicker {kicker_style}" if kicker_style != "plain" else "kicker"

    summary_items = "".join(f"<li>{esc(s)}</li>" for s in doc.executive_summary)
    summary_block = (
        f'<div class="summary"><ol>{summary_items}</ol></div>' if summary_items else ""
    )

    # 模型偶爾會把一整組數列塞進 value；長字串改用小一級的字，版面才不會爆
    figures = "".join(
        f'<div class="figure">'
        f'<div class="value{" long" if len(f.value) > 12 else ""}">{esc(f.value)}</div>'
        f'<div class="label">{esc(f.label)}</div></div>'
        for f in doc.key_figures
    )
    figures_block = f'<div class="figures">{figures}</div>' if figures else ""

    sections: List[str] = []
    for n, section in enumerate(doc.sections, start=1):
        callout = (
            f'<div class="callout">{esc(section.callout)}</div>'
            if section.callout.strip()
            else ""
        )
        sections.append(
            f'<h2><span class="sec-no">{n:02d}</span>{esc(section.heading)}</h2>'
            f'<div class="section-body">{markdown_to_html(section.body_markdown)}</div>'
            f"{callout}"
        )
    sections_block = "".join(sections)

    open_items = "".join(f"<li>{esc(q)}</li>" for q in doc.open_questions)
    open_block = (
        f'<h2><span class="sec-no">--</span>尚待釐清</h2>'
        f'<ul class="open-questions">{open_items}</ul>'
        if open_items
        else ""
    )

    refs = "".join(
        f'<li><a href="{esc(r.url)}" target="_blank" rel="noopener noreferrer">'
        f"{esc(r.title)}</a>"
        f'<span class="ref-url">{esc(r.url)}</span></li>'
        for r in doc.references
    )
    refs_block = (
        f'<h2><span class="sec-no">--</span>參考來源</h2><ul class="refs">{refs}</ul>'
        if refs
        else ""
    )

    stage = "#05070a" if theme.is_dark else "#eceef2"
    root = (
        f"{theme.css_vars()}"
        f"--stage:{stage};"
        f"--h-weight:{theme.traits.get('heading_weight', '700')};"
    )

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(doc.title)}</title>
<style>:root{{{root}}}{_CSS}</style>
</head>
<body>
<main class="page">
  <header class="masthead">
    <span class="{kicker_cls}">深度研究報告</span>
    <h1>{esc(doc.title)}</h1>
    <p class="subtitle">{esc(doc.subtitle)}</p>
    <div class="meta">
      <span><b>研究題目</b>{esc(query)}</span>
      <span><b>模型</b>{esc(model)}</span>
      <span><b>產出時間</b>{esc(generated_at)}</span>
    </div>
  </header>

  <div class="body-wrap">
    <h2><span class="sec-no">00</span>摘要</h2>
    {summary_block}
    {figures_block}

    {sections_block}
    {open_block}
    {refs_block}
  </div>

  <footer>
    本報告由 Insight 深度研究以 {esc(model)} 產生，內容可能有誤，
    請於引用前自行查證來源。
  </footer>
</main>
</body>
</html>
"""
