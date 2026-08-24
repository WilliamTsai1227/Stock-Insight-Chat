"""
簡報樣板：1280×720 的自帶樣式 HTML 投影片，離線可放映。

操作：→ / 空白鍵下一頁、← 上一頁、N 切換講稿、F 全螢幕、Home / End 首末頁。

版面策略
────────
每一頁都是「頁首（kicker + 標題 + 導言）」加「內容區」的固定節奏，
內容區**緊接**頁首之後而不是在剩餘空間置中 —— 置中會讓條目少的頁面
在標題下方裂出一大塊空洞（這是舊版最明顯的毛病）。

內容多寡以 `_density()` 換算成 d1/d2/d3 三級，字級與間距整組跟著降。
這是 OpenAI slides skill 那套 render→偵測溢位→修正的廉價替代：
HTML 由瀏覽器排版，我們控制得了樣板，與其事後量測不如事前給預算。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List

from .common import esc
from .themes import Theme

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--stage);color:var(--body);font-family:var(--font-body);
     overflow:hidden;-webkit-font-smoothing:antialiased}
#stage{position:fixed;inset:0;display:flex;align-items:center;justify-content:center}
#deck{position:relative;width:1280px;height:720px;transform-origin:center center;
      background:var(--bg);overflow:hidden;border-radius:var(--radius);
      box-shadow:0 30px 90px rgba(8,12,20,.30)}
.slide{position:absolute;inset:0;padding:60px 74px 76px;display:none;flex-direction:column;
       animation:fade .26s ease}
.slide.active{display:flex}
@keyframes fade{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
.blob{position:absolute;border-radius:50%;filter:blur(70px);opacity:.42;pointer-events:none}

/* ── 頁首 ───────────────────────────────────────────── */
.kicker{display:inline-flex;align-self:flex-start;align-items:center;gap:7px;
        color:var(--accent);font-weight:700;letter-spacing:.14em;text-transform:uppercase;
        font-size:12px;font-family:var(--font-body)}
.kicker.pill{background:var(--accent-soft);border:1px solid var(--accent-line);
             border-radius:999px;padding:5px 13px}
.kicker.underline{padding-bottom:4px;border-bottom:2px solid var(--accent)}
.slide-head{flex:0 0 auto;margin-bottom:26px}
.slide-head h2{font-family:var(--font-display);font-weight:var(--h-weight);
               color:var(--ink);letter-spacing:-.02em;margin-top:13px;
               font-size:36px;line-height:1.2;max-width:26ch}
.d2 .slide-head h2{font-size:32px}
.d3 .slide-head h2{font-size:29px;margin-top:11px}
.lead{font-size:19px;line-height:1.55;color:var(--ink2);font-weight:450;
      margin-top:11px;max-width:60ch}
.d2 .lead{font-size:17.5px;margin-top:9px}
.d3 .lead{font-size:16.5px;margin-top:8px}
.slide-head .rule{margin-top:18px;height:3px;width:56px;background:var(--accent);
                  border-radius:2px}
.rule-hairline .slide-head .rule{height:1px;width:100%;background:var(--line-strong)}
.rule-double .slide-head .rule{height:0;width:100%;border-top:3px double var(--line-strong)}

/* 內容緊接頁首，不吃掉剩餘空間 —— 空洞的根因就在這 */
.slide-body{flex:0 1 auto;min-height:0;overflow:hidden}

/* ── 條列 ───────────────────────────────────────────── */
ul.bullets{list-style:none;display:flex;flex-direction:column;gap:15px}
.d2 ul.bullets{gap:12px}
.d3 ul.bullets{gap:9px}
ul.bullets li{position:relative;padding-left:30px;font-size:20px;line-height:1.5;
              color:var(--ink2)}
.d2 ul.bullets li{font-size:18px;padding-left:27px}
.d3 ul.bullets li{font-size:16.5px;line-height:1.45;padding-left:25px}
ul.bullets li::before{content:"";position:absolute;left:4px;top:11px;width:8px;height:8px;
  border-radius:2px;background:var(--accent)}
.d3 ul.bullets li::before{top:9px;width:7px;height:7px}

/* ── 議程（編號卡）───────────────────────────────────── */
.agenda{display:grid;grid-template-columns:1fr 1fr;gap:13px 22px}
.agenda-item{display:flex;gap:14px;align-items:flex-start;
             border:1px solid var(--line);border-radius:var(--radius);padding:15px 18px;
             background:var(--bg-soft)}
/* 奇數個項目時最後一張跨滿兩欄，避免孤兒卡 */
.agenda-item:last-child:nth-child(odd){grid-column:1 / -1}
.agenda-num{flex:0 0 26px;height:26px;border-radius:calc(var(--radius) - 2px);
            background:var(--accent);color:var(--bg);font-weight:800;font-size:13px;
            display:flex;align-items:center;justify-content:center}
.agenda-text{font-size:17px;color:var(--ink2);line-height:1.45}
.d3 .agenda-text{font-size:15.5px}

/* ── 數字 ───────────────────────────────────────────── */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:18px}
.stat{border-top:3px solid var(--accent);padding:20px 4px 0}
.stat .value{font-family:var(--font-display);font-size:46px;font-weight:800;color:var(--ink);
             letter-spacing:-.035em;line-height:1.02}
.stat .value.long{font-size:26px;letter-spacing:-.01em;line-height:1.25}
.stat .label{margin-top:11px;font-size:14.5px;color:var(--muted);line-height:1.45}

/* ── 引言 ───────────────────────────────────────────── */
.slide.quote-slide{justify-content:center}
.quote-mark{font-family:var(--font-display);font-size:96px;line-height:.55;
            color:var(--accent);opacity:.28;margin-bottom:14px}
.quote{font-family:var(--font-display);max-width:22ch;font-size:44px;line-height:1.24;
       font-weight:var(--h-weight);color:var(--ink);letter-spacing:-.022em}
.quote-attr{margin-top:26px;font-size:15px;color:var(--muted)}

/* ── 左右對照 ───────────────────────────────────────── */
.compare{display:grid;grid-template-columns:1fr 1fr;gap:26px;align-items:start}
.compare-col{border-top:3px solid var(--accent);padding-top:16px}
.compare-col:nth-child(2){border-top-color:var(--line-strong)}
.compare-col h3{font-family:var(--font-display);font-size:20px;font-weight:var(--h-weight);
                color:var(--ink);margin-bottom:13px;letter-spacing:-.01em}
.compare-col ul{list-style:none;display:flex;flex-direction:column;gap:10px}
.compare-col li{font-size:16.5px;line-height:1.5;color:var(--body);padding-left:16px;
                position:relative}
.compare-col li::before{content:"";position:absolute;left:0;top:10px;width:6px;height:1px;
                        background:var(--muted)}
.d3 .compare-col li{font-size:15px}

/* ── 章節分隔 ───────────────────────────────────────── */
.slide.section-slide{justify-content:center}
.section-num{font-family:var(--font-display);font-size:120px;font-weight:800;
             color:var(--accent);opacity:.18;line-height:.85;letter-spacing:-.05em}
.slide.section-slide h2{font-size:52px;max-width:20ch;margin-top:-18px}

/* ── 封面 / 結語 ────────────────────────────────────── */
.slide.cover{justify-content:center;gap:20px}
.cover h1{font-family:var(--font-display);font-size:60px;line-height:1.06;
          letter-spacing:-.03em;font-weight:800;color:var(--ink);max-width:20ch}
.cover .sub{font-size:22px;color:var(--body);max-width:52ch;line-height:1.5}
.cover .stamp{position:absolute;left:74px;bottom:52px;font-size:13.5px;color:var(--muted);
              letter-spacing:.04em}
.cover .edge{position:absolute;left:0;top:0;bottom:0;width:6px;background:var(--accent)}
.slide.closing{justify-content:center;gap:16px}
.closing h2{font-size:46px;max-width:22ch}

/* ── 頁尾 / 講稿 ────────────────────────────────────── */
.slide-foot{position:absolute;left:74px;right:74px;bottom:26px;display:flex;
            justify-content:space-between;align-items:center;font-size:12px;
            color:var(--muted);letter-spacing:.03em}
.slide-foot .deck-name{max-width:68%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.progress{position:absolute;left:0;bottom:0;height:3px;background:var(--accent);opacity:.85}
#notes{position:fixed;left:0;right:0;bottom:0;background:rgba(10,13,18,.95);color:#e8ebf1;
       padding:17px 26px 19px;font-size:15px;line-height:1.6;display:none;z-index:5;
       max-height:34vh;overflow:auto;backdrop-filter:blur(6px)}
#notes.on{display:block}
#notes b{color:#8fb2ff;display:block;margin-bottom:6px;font-size:11.5px;letter-spacing:.11em;
         text-transform:uppercase}
#hint{position:fixed;right:14px;top:12px;font-size:11.5px;color:var(--muted);z-index:5;
      background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:6px 11px;
      opacity:.9}
"""

_JS = """
(function(){
  var slides=[].slice.call(document.querySelectorAll('.slide'));
  var deck=document.getElementById('deck');
  var notes=document.getElementById('notes');
  var i=0, notesOn=false;

  function fit(){
    var pad=notesOn?0.80:0.94;
    var s=Math.min(window.innerWidth/1280, window.innerHeight/720)*pad;
    deck.style.transform='scale('+s+')';
  }
  function show(n){
    i=Math.max(0,Math.min(slides.length-1,n));
    slides.forEach(function(el,k){el.classList.toggle('active',k===i);});
    var note=slides[i].getAttribute('data-note')||'（本頁無講稿）';
    notes.innerHTML='<b>講稿 '+(i+1)+' / '+slides.length+'</b>';
    notes.appendChild(document.createTextNode(note));
    var bar=slides[i].querySelector('.progress');
    if(bar) bar.style.width=((i+1)/slides.length*100)+'%';
    location.hash='p'+(i+1);
  }
  function toggleNotes(){notesOn=!notesOn;notes.classList.toggle('on',notesOn);fit();}

  document.addEventListener('keydown',function(e){
    if(e.key==='ArrowRight'||e.key===' '||e.key==='PageDown'){e.preventDefault();show(i+1);}
    else if(e.key==='ArrowLeft'||e.key==='PageUp'){e.preventDefault();show(i-1);}
    else if(e.key==='Home'){show(0);}
    else if(e.key==='End'){show(slides.length-1);}
    else if(e.key==='n'||e.key==='N'){toggleNotes();}
    else if(e.key==='f'||e.key==='F'){
      if(document.fullscreenElement){document.exitFullscreen();}
      else{document.documentElement.requestFullscreen();}
    }
  });
  document.getElementById('stage').addEventListener('click',function(e){
    show(e.clientX < window.innerWidth/2 ? i-1 : i+1);
  });
  window.addEventListener('resize',fit);

  var m=(location.hash||'').match(/^#p(\\d+)$/);
  fit(); show(m ? parseInt(m[1],10)-1 : 0);
})();
"""


def _density(slide: Any) -> str:
    """
    依這一頁的內容量給版面預算。

    不是精確量測，是保守估計：寧可字小一號，也不要溢出畫布。
    這頂掉了「render 成圖再偵測溢位」那一整套基礎設施。
    """
    chars = len(slide.title) + len(slide.lead)
    items = list(slide.bullets)
    for col in getattr(slide, "columns", None) or []:
        items.extend(col.bullets)
        chars += len(col.heading)
    chars += sum(len(b) for b in items)

    if len(items) >= 6 or chars > 460:
        return "d3"
    if len(items) >= 4 or chars > 280:
        return "d2"
    return "d1"


def _kicker(theme: Theme, text: str) -> str:
    style = theme.traits.get("kicker", "pill")
    cls = f"kicker {style}" if style != "plain" else "kicker"
    return f'<span class="{cls}">{esc(text)}</span>'


def _foot(deck_title: str, index: int, total: int) -> str:
    return (
        f'<div class="slide-foot"><span class="deck-name">{esc(deck_title)}</span>'
        f"<span>{index} / {total}</span></div>"
        f'<div class="progress" style="width:{index / total * 100:.1f}%"></div>'
    )


def _body_html(slide: Any) -> str:
    layout = slide.layout

    if layout == "stats" and slide.stats:
        cards = "".join(
            f'<div class="stat">'
            f'<div class="value{" long" if len(s.value) > 12 else ""}">{esc(s.value)}</div>'
            f'<div class="label">{esc(s.label)}</div></div>'
            for s in slide.stats
        )
        return f'<div class="stats">{cards}</div>'

    if layout == "compare" and getattr(slide, "columns", None):
        cols = "".join(
            f'<div class="compare-col"><h3>{esc(c.heading)}</h3><ul>'
            + "".join(f"<li>{esc(b)}</li>" for b in c.bullets)
            + "</ul></div>"
            for c in slide.columns[:2]
        )
        return f'<div class="compare">{cols}</div>'

    if layout == "agenda" and slide.bullets:
        items = "".join(
            f'<div class="agenda-item"><div class="agenda-num">{n}</div>'
            f'<div class="agenda-text">{esc(b)}</div></div>'
            for n, b in enumerate(slide.bullets, start=1)
        )
        return f'<div class="agenda">{items}</div>'

    if slide.bullets:
        items = "".join(f"<li>{esc(b)}</li>" for b in slide.bullets)
        return f'<ul class="bullets">{items}</ul>'

    return ""


def _render_slide(
    slide: Any, index: int, total: int, deck_title: str, theme: Theme, section_no: int
) -> str:
    note = esc(slide.note)
    layout = slide.layout
    density = _density(slide)

    if layout == "cover":
        return f"""<section class="slide cover {density}" data-note="{note}">
  <div class="edge"></div>
  <div class="blob" style="width:540px;height:540px;background:var(--accent);opacity:.12;right:-170px;top:-190px"></div>
  {_kicker(theme, slide.kicker or '深度研究')}
  <h1>{esc(slide.title)}</h1>
  <p class="sub">{esc(slide.lead)}</p>
  <div class="stamp">{esc(datetime.now().strftime('%Y-%m-%d'))}　·　Insight 深度研究</div>
</section>"""

    if layout == "quote":
        return f"""<section class="slide quote-slide {density}" data-note="{note}">
  <div class="quote-mark">&ldquo;</div>
  <div class="quote">{esc(slide.lead or slide.title)}</div>
  <div class="quote-attr">{esc(slide.kicker or '本次研究的核心結論')}</div>
  {_foot(deck_title, index, total)}
</section>"""

    if layout == "section":
        return f"""<section class="slide section-slide {density}" data-note="{note}">
  <div class="section-num">{section_no:02d}</div>
  <h2 class="slide-head-inline">{esc(slide.title)}</h2>
  <p class="lead">{esc(slide.lead)}</p>
  {_foot(deck_title, index, total)}
</section>"""

    if layout == "closing":
        items = "".join(f"<li>{esc(b)}</li>" for b in slide.bullets)
        bullets = f'<ul class="bullets">{items}</ul>' if items else ""
        return f"""<section class="slide closing {density}" data-note="{note}">
  <div class="blob" style="width:480px;height:480px;background:var(--accent);opacity:.10;left:-170px;bottom:-200px"></div>
  {_kicker(theme, slide.kicker or '結語')}
  <h2>{esc(slide.title)}</h2>
  <p class="lead">{esc(slide.lead)}</p>
  {bullets}
  {_foot(deck_title, index, total)}
</section>"""

    lead = f'<p class="lead">{esc(slide.lead)}</p>' if slide.lead.strip() else ""
    return f"""<section class="slide {density}" data-note="{note}">
  <div class="slide-head">
    {_kicker(theme, slide.kicker or '重點')}
    <h2>{esc(slide.title)}</h2>
    {lead}
    <div class="rule"></div>
  </div>
  <div class="slide-body">{_body_html(slide)}</div>
  {_foot(deck_title, index, total)}
</section>"""


def render_deck(doc: Any, *, model: str, query: str, theme: Theme) -> str:
    """把 `DeckDoc` 轉成完整的 HTML 投影片檔。"""
    total = len(doc.slides)
    stage = "#080a0e" if theme.is_dark else "#e9ebf0"

    slides: List[str] = []
    section_no = 0
    for idx, slide in enumerate(doc.slides, start=1):
        if slide.layout == "section":
            section_no += 1
        slides.append(_render_slide(slide, idx, total, doc.title, theme, section_no))

    root = (
        f"{theme.css_vars()}"
        f"--stage:{stage};"
        f"--h-weight:{theme.traits.get('heading_weight', '700')};"
    )
    rule_cls = f"rule-{theme.traits.get('rule', 'bar')}"

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(doc.title)}</title>
<style>:root{{{root}}}{_CSS}</style>
</head>
<body class="{rule_cls}">
<div id="hint">→ 下一頁　← 上一頁　N 講稿　F 全螢幕</div>
<div id="stage"><div id="deck">{''.join(slides)}</div></div>
<div id="notes"></div>
<script>{_JS}</script>
</body>
</html>
"""
