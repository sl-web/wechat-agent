"""公众号封面图生成:HTML 模板 + Playwright 截图。

### 为什么不用 AI 文生图
- **中文几乎必错**:AI 生图写中文标题错字率很高,封面是最显眼的位置;
- 风格每次不同,同一账号的封面不统一;
- 有版权与肖像权风险(真人形象、知名 IP)。

模板化合成恰好相反:中文字体完美、风格稳定、可复现、可回滚、零版权风险。
需要变化时,可以把 AI 生成的**无文字抽象底图**当背景,标题永远由模板渲染。

### 尺寸(公众号规范)
- **首图 2.35:1 → 900×383**:列表页与分享卡片用的主图
- **次图 1:1 → 200×200**:多图文场景下的小图

### 落盘位置
`data/covers/{article_id}_{style}.png`(首图)与 `..._sq.png`(次图)。
生成后由 `app/pusher/client.py` 的上传链路当作普通本地图片使用 —— 上传逻辑零改动。
"""
from __future__ import annotations

from pathlib import Path

from app.core.settings import settings

# 首图 / 次图尺寸
WIDE = (900, 383)
SQUARE = (200, 200)

# 字体栈:无头浏览器没有默认中文字体时会渲染成方框,必须显式声明
FONT = "'Microsoft YaHei','PingFang SC','Hiragino Sans GB','Source Han Sans SC',sans-serif"

COVER_STYLES = {
    "dark": "科技深色",
    "light": "简约浅色",
    "blocks": "色块对比",
}
DEFAULT_STYLE = "dark"


def _title_size(text: str, base: int) -> int:
    """按标题长度自适应字号,避免长标题溢出安全区。"""
    n = len(text or "")
    if n <= 14:
        return base
    if n <= 20:
        return int(base * 0.88)
    if n <= 26:
        return int(base * 0.77)
    return int(base * 0.66)


def _page(inner: str, width: int, height: int) -> str:
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ width:{width}px; height:{height}px; overflow:hidden; }}
  body {{ font-family:{FONT}; }}
</style></head><body>{inner}</body></html>"""


def build_wide_html(title: str, brand: str, tag: str, style: str) -> str:
    """首图(900×383)HTML。"""
    w, h = WIDE
    t = (title or "").strip() or "未命名文章"
    brand = (brand or "").strip() or "公众号"
    tag = (tag or "").strip()

    if style == "light":
        inner = f"""<div style="width:{w}px;height:{h}px;background:#f7f9fb;position:relative;">
  <div style="position:absolute;left:0;top:0;width:14px;height:100%;background:#4a5568;"></div>
  <div style="padding:52px 64px 52px 78px;height:100%;display:flex;flex-direction:column;justify-content:center;">
    <div style="font-size:18px;letter-spacing:5px;color:#8a97a6;margin-bottom:20px;">{brand}</div>
    <div style="font-size:{_title_size(t, 48)}px;line-height:1.34;font-weight:bold;color:#1f2733;">{t}</div>
    {f'<div style="margin-top:22px;font-size:19px;color:#8a97a6;letter-spacing:2px;">{tag}</div>' if tag else ''}
  </div>
</div>"""

    elif style == "blocks":
        # 色块占 30% 并收窄内边距:保证 12~14 字的常见标题能一行放下,不会断在难看的字上
        inner = f"""<div style="width:{w}px;height:{h}px;display:flex;">
  <div style="width:30%;background:#8a3a1f;display:flex;align-items:center;justify-content:center;">
    <div style="font-size:25px;letter-spacing:8px;color:#ffffff;font-weight:bold;">{brand}</div>
  </div>
  <div style="flex:1;background:#ffffff;display:flex;flex-direction:column;justify-content:center;padding:0 44px;">
    <div style="font-size:{_title_size(t, 44)}px;line-height:1.32;font-weight:bold;color:#1f2733;">{t}</div>
    {f'<div style="margin-top:16px;font-size:18px;color:#8a97a6;letter-spacing:2px;">{tag}</div>' if tag else ''}
  </div>
</div>"""

    else:  # dark(默认)
        inner = f"""<div style="width:{w}px;height:{h}px;background:#0f1a2b;position:relative;overflow:hidden;">
  <div style="position:absolute;left:0;top:0;right:0;bottom:0;opacity:.14;
    background-image:linear-gradient(#4a86c8 1px,transparent 1px),linear-gradient(90deg,#4a86c8 1px,transparent 1px);
    background-size:36px 36px;"></div>
  <div style="position:absolute;right:-70px;top:-90px;width:300px;height:300px;border-radius:50%;
    background:#185fa5;opacity:.42;"></div>
  <div style="position:relative;height:100%;padding:46px 56px;display:flex;flex-direction:column;justify-content:space-between;">
    <div style="font-size:19px;letter-spacing:6px;color:#7fb2e8;font-weight:bold;">{brand}</div>
    <div style="font-size:{_title_size(t, 52)}px;line-height:1.3;font-weight:bold;color:#ffffff;">{t}</div>
    {f'<div style="font-size:18px;color:#8a9cb3;letter-spacing:3px;">{tag}</div>' if tag else '<div></div>'}
  </div>
</div>"""
    return _page(inner, w, h)


def build_square_html(title: str, brand: str, tag: str, style: str) -> str:
    """次图(200×200)HTML —— 尺寸小,只放品牌与一个短标签。"""
    w, h = SQUARE
    brand = (brand or "").strip() or "公众号"
    short = (tag or "").strip() or (title or "").strip()
    short = short[:6]
    bg, fg, sub = {
        "light": ("#f7f9fb", "#1f2733", "#8a97a6"),
        "blocks": ("#8a3a1f", "#ffffff", "#f3d9cf"),
    }.get(style, ("#0f1a2b", "#ffffff", "#7fb2e8"))
    inner = f"""<div style="width:{w}px;height:{h}px;background:{bg};display:flex;
  flex-direction:column;align-items:center;justify-content:center;gap:10px;">
  <div style="font-size:22px;font-weight:bold;color:{fg};letter-spacing:2px;text-align:center;
    padding:0 12px;line-height:1.3;">{brand}</div>
  <div style="width:34px;height:2px;background:{sub};"></div>
  <div style="font-size:13px;color:{sub};letter-spacing:1px;text-align:center;padding:0 10px;">{short}</div>
</div>"""
    return _page(inner, w, h)


def build_html(title: str, brand: str, tag: str, style: str, size: str = "wide") -> str:
    """按尺寸构建封面 HTML。size: wide(首图) / square(次图)。"""
    if size == "square":
        return build_square_html(title, brand, tag, style)
    return build_wide_html(title, brand, tag, style)


def render_png(html: str, out_path: Path, size: str = "wide") -> Path:
    """用 Playwright 把 HTML 渲染成 PNG。**必须在本模块所在的独立进程里调用。**"""
    from playwright.sync_api import sync_playwright

    w, h = SQUARE if size == "square" else WIDE
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": w, "height": h},
                                    device_scale_factor=2)   # 2x 更清晰
            page.set_content(html, wait_until="load")
            page.wait_for_timeout(180)                       # 等字体
            page.screenshot(path=str(out_path), clip={"x": 0, "y": 0, "width": w, "height": h})
        finally:
            browser.close()
    finally:
        pw.stop()
    return out_path


def generate(article: dict, style: str = DEFAULT_STYLE, brand: str = "",
             tag: str = "", out_dir: Path | None = None) -> dict:
    """为文章生成首图 + 次图,返回 `{wide, square}` 路径(失败为 None)。

    单张失败不影响另一张 —— 封面是"尽力而为",不该因此阻断推送。
    """
    style = style if style in COVER_STYLES else DEFAULT_STYLE
    aid = article.get("id") or "tmp"
    title = article.get("title") or ""
    out = Path(out_dir) if out_dir else (settings.data_dir / "covers")
    res: dict[str, str | None] = {"wide": None, "square": None, "style": style}
    for size, suffix in (("wide", ""), ("square", "_sq")):
        try:
            html = build_html(title, brand, tag, style, size)
            p = out / f"{aid}_{style}{suffix}.png"
            render_png(html, p, size)
            res[size] = str(p)
        except Exception as e:  # noqa: BLE001  封面失败不阻断推送
            res[f"{size}_error"] = f"{type(e).__name__}: {e}"
    return res
