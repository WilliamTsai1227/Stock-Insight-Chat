"""重新產生 app/frontend/icons/ 底下的 PWA 圖示。

深色底 + Lucide trending-up 折線，與站上 sidebar / 登入頁的 logo 一致。
圖示已 commit 進 repo，只有要改配色或造型時才需要重跑。

用法（專案根目錄）：
    pip install pillow
    python deploy/generate_pwa_icons.py

輸出：
    apple-touch-icon.png     180  iOS 主畫面（不透明全出血，iOS 自己套圓角）
    icon-192 / icon-512.png       manifest purpose="any"（圓角＋透明外緣）
    icon-maskable-512.png    512  manifest purpose="maskable"（內容縮在中央安全區）
    favicon-16 / favicon-32.png   瀏覽器分頁
"""
from PIL import Image, ImageDraw
import os

OUT = "app/frontend/icons"
os.makedirs(OUT, exist_ok=True)

SS = 4  # supersample

# Lucide trending-up (24x24 viewBox, stroke-width 2, round cap/join)
POLYS = [
    [(22, 7), (13.5, 15.5), (8.5, 10.5), (2, 17)],
    [(16, 7), (22, 7), (22, 13)],
]
STROKE_VB = 2.0

BG_CENTER = (28, 28, 28)
BG_EDGE = (13, 13, 13)
GLYPH_L = (255, 255, 255)
GLYPH_R = (138, 138, 138)


def radial_bg(size):
    """中心略亮的深色底，呼應 --content-surface-gradient。"""
    g = Image.radial_gradient("L").resize((size, size), Image.LANCZOS)
    a = Image.new("RGB", (size, size), BG_CENTER)
    b = Image.new("RGB", (size, size), BG_EDGE)
    return Image.composite(b, a, g.point(lambda v: 255 if v > 255 else v))


def glyph_gradient(size):
    """白 → 灰的水平漸層，對應 --login-logo-gradient。"""
    row = Image.new("RGB", (size, 1))
    px = row.load()
    for x in range(size):
        t = x / max(size - 1, 1)
        px[x, 0] = tuple(round(GLYPH_L[i] + (GLYPH_R[i] - GLYPH_L[i]) * t) for i in range(3))
    return row.resize((size, size), Image.LANCZOS)


def stroke_mask(size, frac, stroke_vb=STROKE_VB):
    """把 24x24 viewBox 的折線畫成遮罩（圓角端點＋圓角轉折）。"""
    S = size * SS
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    box = S * frac
    off = (S - box) / 2
    scale = box / 24.0
    w = max(int(round(stroke_vb * scale)), 2)
    r = w / 2.0
    for poly in POLYS:
        pts = [(off + x * scale, off + y * scale) for x, y in poly]
        d.line(pts, fill=255, width=w, joint="curve")
        for x, y in pts:  # round caps
            d.ellipse([x - r, y - r, x + r, y + r], fill=255)
    return m.resize((size, size), Image.LANCZOS)


def rounded_mask(size, radius_frac):
    S = size * SS
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, S - 1, S - 1], radius=S * radius_frac, fill=255)
    return m.resize((size, size), Image.LANCZOS)


def build(size, frac, stroke_vb=STROKE_VB, corner=None):
    """corner=None → 全出血方形（iOS / maskable）；否則圓角＋透明外緣。"""
    img = radial_bg(size).convert("RGBA")
    img = Image.composite(glyph_gradient(size).convert("RGBA"), img, stroke_mask(size, frac, stroke_vb))
    if corner is not None:
        img.putalpha(rounded_mask(size, corner))
    return img


jobs = [
    # iOS 主畫面圖示：必須不透明全出血，iOS 自己套圓角遮罩
    ("apple-touch-icon.png", build(180, 0.72)),
    # manifest purpose="any"
    ("icon-192.png", build(192, 0.66, corner=0.22)),
    ("icon-512.png", build(512, 0.66, corner=0.22)),
    # manifest purpose="maskable"：內容需縮在中央 80% 安全區內
    ("icon-maskable-512.png", build(512, 0.52)),
    # 瀏覽器分頁 favicon（小尺寸線條加粗才看得清）
    ("favicon-32.png", build(32, 0.78, stroke_vb=2.6, corner=0.22)),
    ("favicon-16.png", build(16, 0.86, stroke_vb=3.2, corner=0.22)),
]
for name, im in jobs:
    p = os.path.join(OUT, name)
    im.save(p, "PNG", optimize=True)
    print(f"{p}  {im.size[0]}x{im.size[1]}  {os.path.getsize(p)}B")
