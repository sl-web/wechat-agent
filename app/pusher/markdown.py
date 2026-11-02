"""Markdown → 公众号富文本 HTML 转换(PRD 5.4 正文格式)。

关键约束:**公众号编辑器会剥离 class/id,只保留内联 style**,
所以所有排版都必须写成 `style="..."` 内联属性,不能靠 CSS 类。

块级:ATX 标题 / **隐式小标题** / 容器块(`:::`) / 引用块 / 列表 / 代码块 / 段落。
行内:加粗 / 斜体 / 行内代码 / 链接。

### 四个必须守住的坑(都真实踩过)

1. **段落绝不能合并**。LLM 输出正文时习惯用**单换行**分段,而标准 Markdown 把
   单换行当"段内软换行"。若照标准合并,整篇正文会挤进一个 `<p>` ——
   实测把 1100 字变成一坨,手机上看就是一大块灰。
   现在按「行尾是否有句末标点」切段,两种写法都能正确分段。

2. **不能只依赖模型输出 `##`**。模型经常用 `①②③`、`一、`、`1.` 这类
   **纯文本序号**分段,视觉上等于没有层级。`_implicit_heading()` 把这类
   编号提升为真正的小标题 —— 这是"排版不出彩"的兜底防线。

3. **段落收集不能把 `*` 当块起始**。否则 `**重点句。**` 这种整行会被**静默丢弃**。

4. **短行不一定是小标题**。收尾句、软换行留下的半句话都很像标题。
   判据是「其后继正文够不够长」+「是否结句」,见 `_is_short_heading` / `_bold_heading`。

### 结构块语法
`article_to_markdown()` 用 `:::lead` / `:::thesis` / `:::quote` / `:::cta` 包裹
非正文段落,`markdown_to_html()` 把它们渲染成视觉上明显不同的卡片。

### 排版主题(C 批)
`LAYOUT_THEMES` 把整套视觉参数(主题色 / 字号 / 行距 / 段距 / 圆角 / 标题样式)
收在一个字典里,换主题即换风格。配置项:
- `mp_layout_theme`:预设名 `tech` / `minimal` / `vivid`
- `mp_heading_style`:小节标题样式 `bar`(左色块)/ `badge`(编号徽章)/ `center`(居中带线),
  留空则跟随主题预设
- `mp_accent_color`:只覆盖主题色,留空则跟随主题预设
**所有浅色底都由主题色按比例混白推导**(`_tint`),所以改一个色值整套配色一起变。
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

# 项目根目录(用于把相对图片路径解析成绝对路径)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ---- 基础色板 ----
DEFAULT_ACCENT = "#185fa5"    # 默认主题色(主题预设或 mp_accent_color 可覆盖)
LINK_COLOR = "#576b95"        # 公众号正文链接色
TEXT_COLOR = "#3f3f3f"        # 正文
MUTED_COLOR = "#5b6b7c"       # 次要文字(导语/引用)
TITLE_COLOR = "#1a1a1a"
SOURCE_COLOR = "#a0aab5"      # 信息来源

# ---- 排版主题预设 ----
# 一套主题 = 全部视觉参数。新增主题只要往这里加一项,并在 PanelView 暴露即可。
LAYOUT_THEMES: dict[str, dict] = {
    "tech": {
        "name": "科技蓝",
        "accent": "#185fa5",
        "p_size": 16, "line_height": 1.85, "letter_spacing": 1,
        "para_gap": 22, "radius": 8, "heading_size": 18, "heading_style": "bar",
    },
    "minimal": {
        "name": "简约浅色",
        "accent": "#4a5568",
        "p_size": 16, "line_height": 1.9, "letter_spacing": 1.5,
        "para_gap": 24, "radius": 6, "heading_size": 17, "heading_style": "center",
    },
    "vivid": {
        "name": "高对比",
        "accent": "#8a3a1f",
        "p_size": 16, "line_height": 1.8, "letter_spacing": 1,
        "para_gap": 20, "radius": 12, "heading_size": 19, "heading_style": "badge",
    },
}
DEFAULT_THEME = "tech"
HEADING_STYLES = ("bar", "badge", "center")

# 单张内嵌图片的体积上限(转 base64 后正文会膨胀约 1/3,太大不适合粘进编辑器)
_IMG_MAX_BYTES = 2 * 1024 * 1024
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# 字体栈:公众号正文默认走系统字体,显式声明可让中英文与数字在不同机型上更一致
FONT_STACK = (
    "-apple-system,BlinkMacSystemFont,'Helvetica Neue','PingFang SC',"
    "'Hiragino Sans GB','Microsoft YaHei',sans-serif"
)

# ---- 行内样式(与主题色无关,固定)----
STRONG_STYLE = f"font-weight:bold;color:{TITLE_COLOR};"
EM_STYLE = "font-style:italic;"
A_STYLE = f"color:{LINK_COLOR};text-decoration:none;"
CODE_STYLE = (
    "background:#f2f2f2;border-radius:3px;padding:2px 5px;color:#c7254e;"
    "font-family:Consolas,Monaco,monospace;font-size:14px;"
)

# ---- 分句与分段的判据 ----
# 句末标点:出现在**行尾**即视为一个段落结束
_SENT_END = "。!?!?.．…"
# 行尾常见的收尾符号(引号/括号),判断句末前先剥掉
_TRAIL_TAIL = "\"'’”),)]】」』*_ \t"
# 圈号序号 ①~⑳
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

# 隐式小标题:①标题。正文… / 一、标题。正文… / 1.标题。正文…
_IMPLICIT_RE = re.compile(
    rf"^\s*({f'[{_CIRCLED}]'}|第[一二三四五六七八九十]+[、,，.．]|[一二三四五六七八九十]+、)"
    r"\s*(.{2,26}?)\s*[。.，,:：;；]\s*(.*)$"
)
# 独占一行的 **小节名**
_BOLD_ONLY_RE = re.compile(r"^\*\*(.+?)\*\*$")
# 容器块标记 :::lead / :::thesis / :::quote / :::cta / :::note
_CONTAINER_START_RE = re.compile(r"^:::\s*([a-z]+)\s*$")
_CONTAINER_END_RE = re.compile(r"^:::\s*$")
_CONTAINER_MAX = 12           # 容器内最多收集多少行,防格式错乱时吞掉全文
# 信息来源行(自动降级成小字灰字)
_SOURCE_RE = re.compile(r"^\s*(信息来源|资料来源|来源|参考来源)\s*[:：]")
# 分隔符:独占一行的 --- / *** / ___
_HR_RE = re.compile(r"^([-*_])\1{2,}$")
# 图片:![说明](地址) —— 地址可以是 http(s) 直链,也可以是本地文件路径
_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")

# 短行视为小标题的字数上限
_SHORT_HEADING_MAX = 20
# 拆出标题后,剩余正文至少这么长才算"标题 + 正文"(否则不拆,避免误伤)
_MIN_REMAINDER = 30
# 短行要被当成小标题,其后继正文至少要有这么长(防收尾句/半句话误判)
_MIN_NEXT_LEN = 24


# ---------------- 颜色工具 ----------------

def _tint(hex_color: str, ratio: float = 0.92) -> str:
    """把主题色按比例混入白色,得到极浅的底色(ratio 越大越浅)。"""
    h = (hex_color or DEFAULT_ACCENT).lstrip("#")
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        r, g, b = (24, 95, 165)
    mix = lambda c: round(c + (255 - c) * ratio)  # noqa: E731
    return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"


def current_theme() -> dict:
    """当前生效的排版主题(主题预设 + 配置覆盖)。

    覆盖优先级:`mp_accent_color` / `mp_heading_style` 显式配了就覆盖预设,留空则跟随。
    配置不可用时用默认预设,排版不该因为读不到配置就失败。
    """
    theme = dict(LAYOUT_THEMES[DEFAULT_THEME])
    cfg: dict = {}
    try:
        from app.core.config import get_all_config

        cfg = get_all_config() or {}
    except Exception:  # noqa: BLE001
        return theme

    key = str(cfg.get("mp_layout_theme") or DEFAULT_THEME).strip()
    theme = dict(LAYOUT_THEMES.get(key) or LAYOUT_THEMES[DEFAULT_THEME])

    a = str(cfg.get("mp_accent_color") or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", a):
        theme["accent"] = a
    hs = str(cfg.get("mp_heading_style") or "").strip()
    if hs in HEADING_STYLES:
        theme["heading_style"] = hs
    theme["key"] = key if key in LAYOUT_THEMES else DEFAULT_THEME
    return theme


def accent_color() -> str:
    """当前主题色(保留此函数名:调用方只需一个色值时的便捷入口)。"""
    return current_theme()["accent"]


def _head_style(t: dict, size: int, mt: int, mb: int) -> str:
    """小节标题样式 —— 三种风格视觉差别明显,由主题或配置决定。"""
    a = t["accent"]
    r = t["radius"]
    base = (f"font-size:{size}px;font-weight:bold;line-height:1.6;"
            f"margin:{mt}px 0 {mb}px;letter-spacing:1px;")
    style = t["heading_style"]
    if style == "center":          # 居中 + 细下划线(文字仍用主题色,否则标题上看不出配色)
        return (base + f"text-align:center;color:{a};padding:0 0 12px;"
                       f"border-bottom:1px solid {_tint(a, 0.55)};")
    if style == "badge":           # 编号徽章 + 深色标题(徽章由 span 提供)
        return base + f"color:{TITLE_COLOR};padding:0;"
    # bar:左色块 + 浅底 + 圆角(默认)
    return (base + f"color:{a};padding:8px 12px;letter-spacing:1px;"
                   f"border-left:5px solid {a};border-radius:0 {r}px {r}px 0;"
                   f"background:{_tint(a, 0.92)};")


def _styles(t: dict) -> dict[str, str]:
    """按主题生成整套内联样式。浅色底一律由主题色混白推导,保证换色后整体协调。"""
    a = t["accent"]
    r = t["radius"]
    ps, lh, ls, gap = t["p_size"], t["line_height"], t["letter_spacing"], t["para_gap"]
    hb = t["heading_size"] + 8
    return {
        # 正文
        "p": (f"font-size:{ps}px;line-height:{lh};letter-spacing:{ls}px;color:{TEXT_COLOR};"
              f"margin:0 0 {gap}px;"),
        # 小节标题(## / ### 视觉统一,只差字号)
        "h1": _head_style(t, t["heading_size"], 36, 20),
        "h2": _head_style(t, t["heading_size"], 36, 20),
        "h3": _head_style(t, max(15, t["heading_size"] - 1), 30, 16),
        "h4": _head_style(t, max(15, t["heading_size"] - 2), 26, 14),
        "heading_style": t["heading_style"],
        # 编号徽章(badge 样式用)
        "badge": (f"display:inline-block;min-width:{hb}px;height:{hb}px;line-height:{hb}px;"
                  f"background:{a};color:#ffffff;border-radius:{r}px;text-align:center;"
                  f"font-size:{max(13, t['heading_size'] - 4)}px;margin-right:10px;"
                  "letter-spacing:0;"),
        # 引用块
        "quote": (f"background:#f7f8fa;border-left:4px solid {_tint(a, 0.45)};"
                  f"border-radius:0 {r}px {r}px 0;padding:14px 16px;margin:0 0 {gap}px;"
                  f"color:{MUTED_COLOR};font-size:{ps - 1}px;line-height:1.8;letter-spacing:0.5px;"),
        "quote_p": "margin:0 0 8px;",
        # 列表:用主题色符号 + 悬挂缩进,比默认圆点更贴合公众号观感
        "ul": f"list-style:none;padding-left:0;margin:0 0 {gap}px;",
        "ol": f"list-style:none;padding-left:0;margin:0 0 {gap}px;",
        "li": (f"font-size:{ps}px;line-height:{lh};letter-spacing:{ls}px;color:{TEXT_COLOR};"
               "margin:0 0 10px;padding-left:1.3em;text-indent:-1.3em;"),
        "bullet": f"color:{a};font-weight:bold;",
        # 代码块
        "pre": (f"background:#f7f8fa;border-radius:{r}px;padding:14px 16px;margin:0 0 {gap}px;"
                "font-family:Consolas,Monaco,monospace;font-size:14px;line-height:1.7;"
                "white-space:pre-wrap;word-break:break-all;"),
        # 导语卡
        "lead": (f"background:{_tint(a, 0.95)};border-radius:{r + 2}px;padding:16px 18px;"
                 "margin:0 0 26px;"),
        "lead_p": (f"margin:0;font-size:{ps - 1}px;line-height:1.85;letter-spacing:0.5px;"
                   f"color:{MUTED_COLOR};"),
        # 核心论点卡:全文最该被看到的一句
        "thesis": (f"background:{_tint(a, 0.88)};border-left:5px solid {a};"
                   f"border-radius:0 {r + 2}px {r + 2}px 0;padding:16px 18px;margin:0 0 28px;"),
        "thesis_label": (f"margin:0 0 8px;font-size:12px;font-weight:bold;color:{a};"
                         "letter-spacing:2px;"),
        "thesis_p": (f"margin:0;font-size:{ps + 1}px;font-weight:bold;line-height:1.8;"
                     f"letter-spacing:1px;color:{a};"),
        # 金句:上下细线 + 居中大字
        "kicker": "margin:34px 0;text-align:center;",
        "kicker_line": f"height:1px;background:{_tint(a, 0.55)};margin:0 auto;width:100%;",
        "kicker_p": (f"margin:18px 0;font-size:{ps + 1}px;font-weight:bold;line-height:1.85;"
                     f"letter-spacing:2px;color:{a};"),
        # 结尾引导
        "cta": (f"background:#f7f8fa;border-radius:{r + 2}px;padding:16px 18px;"
                "margin:30px 0 0;text-align:center;"),
        "cta_p": f"margin:0;font-size:{ps - 1}px;line-height:1.8;color:{MUTED_COLOR};letter-spacing:0.5px;",
        # 信息来源
        "source": (f"font-size:13px;line-height:1.75;color:{SOURCE_COLOR};"
                   "margin:28px 0 0;padding-top:14px;border-top:1px solid #eeeeee;"
                   "letter-spacing:0.5px;word-break:break-all;"),
        # 分隔符:三个小圆点(比一条直线更有装饰感)
        "hr": "margin:32px 0;text-align:center;",
        "hr_dot": (f"display:inline-block;width:6px;height:6px;border-radius:50%;"
                   f"background:{_tint(a, 0.55)};margin:0 6px;"),
        # 配图
        "img_wrap": "margin:0 0 24px;text-align:center;",
        "img": f"max-width:100%;border-radius:{r}px;display:block;margin:0 auto;",
        "img_cap": ("margin:10px 0 0;font-size:13px;line-height:1.6;"
                    f"color:{SOURCE_COLOR};text-align:center;"),
    }


# ---------------- 通用工具 ----------------

def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    """行内:图片 / 链接 / 加粗 / 斜体 / 行内代码(均带内联样式)。

    图片必须排在链接**之前** —— 否则 `![alt](src)` 的 `[alt](src)` 部分会先被链接规则吃掉。
    """
    t = _escape(text)
    t = _IMG_RE.sub(lambda m: _img_tag(m.group(1), m.group(2), inline=True), t)
    t = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: f'<a href="{m.group(2)}" style="{A_STYLE}">{m.group(1)}</a>',
        t,
    )
    t = re.sub(r"\*\*([^*]+)\*\*", lambda m: f'<strong style="{STRONG_STYLE}">{m.group(1)}</strong>', t)
    t = re.sub(r"\*([^*]+)\*", lambda m: f'<em style="{EM_STYLE}">{m.group(1)}</em>', t)
    t = re.sub(r"`([^`]+)`", lambda m: f'<code style="{CODE_STYLE}">{m.group(1)}</code>', t)
    return t


def _img_src(src: str) -> str:
    """解析图片地址:http(s) 直链原样返回;本地文件读出来转 base64。

    公众号编辑器在粘贴时会**自行把内嵌图片上传到微信图床**,所以粘完就是永久链接。
    只接受常见图片扩展名并限制体积(base64 会让正文膨胀约 1/3,太大不适合粘)。
    """
    if re.match(r"^https?://", src):
        return src
    try:
        p = Path(src)
        if not p.is_absolute():
            p = PROJECT_ROOT / src
        if p.suffix.lower() not in _IMG_EXTS or not p.is_file():
            return ""
        if p.stat().st_size > _IMG_MAX_BYTES:
            return ""
        ext = p.suffix.lower().lstrip(".")
        mime = "jpeg" if ext in ("jpg", "jpeg") else ext
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        return f"data:image/{mime};base64,{data}"
    except Exception:  # noqa: BLE001  读图失败不该让整篇排版失败
        return ""


def _img_tag(alt: str, src: str, inline: bool = False) -> str:
    """图片标签。解析不出来时退化成 `[图: alt]` 文本,让人知道这里本该有图。"""
    url = _img_src(src)
    if not url:
        return f"[图:{_escape(alt) or _escape(src)}]"
    style = "max-width:100%;border-radius:6px;vertical-align:middle;" if inline else IMG_STYLE
    return f'<img src="{url}" alt="{_escape(alt)}" style="{style}" />'


# 独立成段的图片用这份样式(在 _styles 里也会覆盖,这里给行内兜底)
IMG_STYLE = "max-width:100%;border-radius:6px;display:block;margin:0 auto;"


def _render_image(alt: str, src: str, S: dict[str, str]) -> str:
    url = _img_src(src)
    if not url:
        return f'<p style="{S["p"]}">[图:{_escape(alt) or _escape(src)}]</p>'
    cap = (f'<p style="{S["img_cap"]}">{_inline(alt)}</p>' if alt.strip() else "")
    return (f'<section style="{S["img_wrap"]}">'
            f'<img src="{url}" alt="{_escape(alt)}" style="{S["img"]}" />{cap}</section>')


def _render_hr(S: dict[str, str]) -> str:
    dot = f'<span style="{S["hr_dot"]}"></span>'
    return f'<section style="{S["hr"]}">{dot}{dot}{dot}</section>'


def _is_cjk(ch: str) -> bool:
    return bool(ch) and "\u2e80" <= ch <= "\u9fff"


def _join(lines: list[str]) -> str:
    """合并同一段落内的多行。中文之间不加空格,英文之间加一个空格。"""
    if len(lines) == 1:
        return lines[0]
    out = lines[0]
    for nxt in lines[1:]:
        if _is_cjk(out[-1:]) or _is_cjk(nxt[:1]):
            out += nxt
        else:
            out += " " + nxt
    return out


def _ends_sentence(s: str) -> bool:
    """行尾是否为句末(剥掉引号/星号等收尾符号后判断)。"""
    t = s.rstrip(_TRAIL_TAIL)
    return bool(t) and t[-1] in _SENT_END


def _split_paragraphs(lines: list[str]) -> list[str]:
    """把「连续非空行」切成真正的段落。

    **这是修复"整篇正文挤进一个 <p>"的关键**:LLM 用单换行分段时,
    标准 Markdown 的合并行为会把 1000+ 字并成一段。
    规则:行尾出现句末标点即为段落边界。
    """
    out: list[str] = []
    buf: list[str] = []
    for ln in lines:
        buf.append(ln)
        if _ends_sentence(ln):
            out.append(_join(buf))
            buf = []
    if buf:                      # 结尾没有句末标点的残段
        out.append(_join(buf))
    return [p for p in out if p.strip()]


def _is_block_start(line: str) -> bool:
    """该行是否开启一个新的块级元素(标题/引用/列表/容器/分隔符/图片)。"""
    s = line.strip()
    if not s:
        return True
    if s.startswith(("#", ">", "```")):
        return True
    if _CONTAINER_START_RE.match(s) or _HR_RE.match(s):
        return True
    if _IMG_RE.fullmatch(s):                 # 独占一行的图片
        return True
    if re.match(r"^[-*+]\s+", s):            # 无序列表(要求空格,避免误伤 **加粗**)
        return True
    if re.match(r"^\d+[.)]\s+", s):          # 有序列表
        return True
    return False


def _implicit_heading(line: str) -> tuple[str, str] | None:
    """把 `①标题。正文…` 拆成 (标题, 剩余正文);不适用时返回 None。

    模型经常用纯文本序号分段 —— 那样在手机上没有任何视觉层级,
    读者看到的还是一整块灰。提升为真正的小标题是排版兜底的第一道防线。
    """
    m = _IMPLICIT_RE.match(line)
    if not m:
        return None
    head = m.group(2).strip()
    rest = m.group(3).strip()
    if not head:
        return None
    if rest and len(rest) < _MIN_REMAINDER:
        return None
    return head, rest


def _next_nonempty_len(lines: list[str], i: int) -> int:
    """紧随其后的第一个非空行的长度(用于判断"短行是标题还是半句话")。"""
    for j in range(i + 1, len(lines)):
        if lines[j].strip():
            return len(lines[j].strip())
    return 0


def _is_short_heading(line: str, next_len: int = 0) -> str | None:
    """独立的短行(无句末标点)视为小标题,返回标题文本;否则 None。

    `next_len` 是紧随其后那段正文的长度,**必须够长**才算小标题 ——
    这是防误判的关键判据:
      - 文章末尾的短行通常是收尾句("这就是代价"),不是标题;
      - 软换行留下的半句话("这一句没写完")同样不该变成标题。
    两种情况的后继正文都很短或没有,因此用"后继正文长度"一道卡住。
    """
    s = line.strip()
    if not s or len(s) > _SHORT_HEADING_MAX or _is_block_start(s):
        return None
    if next_len < _MIN_NEXT_LEN:
        return None
    if _ends_sentence(s) or s[-1] in "，,、；;：:":
        return None
    # 含行内标记的是正文(加粗/链接/代码),不是标题
    if any(ch in s for ch in ("*", "[", "`", "|", "@")):
        return None
    if s.startswith("http"):
        return None
    # 纯数字/纯符号的碎片不是标题
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", s):
        return None
    return s


def _bold_heading(line: str) -> str | None:
    """独占一行的 **短句加粗** ⇒ 小标题;否则 None。

    只有**短、且不结句**的加粗行才是标题:
      - `**真正的变量在哪**`            ⇒ 标题(短、无句末标点)
      - `**按条计价,本质是装阀门。**`  ⇒ 重点句(带句号),属于正文
      - `**40~80 字的核心论点……**`    ⇒ thesis,属于正文
    """
    m = _BOLD_ONLY_RE.match(line.strip())
    if not m:
        return None
    inner = m.group(1).strip()
    if not inner or len(inner) > _SHORT_HEADING_MAX or _ends_sentence(inner):
        return None
    return inner


# 标题开头的序号:① / 一、 / 第 3 节 / 1. —— badge 样式把它拆成徽章
_HEAD_NUM_RE = re.compile(
    rf"^\s*([{_CIRCLED}]|第[一二三四五六七八九十]+[条节部分、]|[一二三四五六七八九十]+[、.]|\d{{1,2}}[.、])\s*"
)


def _split_heading_number(text: str) -> tuple[str, str]:
    """拆出标题开头的序号:(徽章文字, 去掉序号的标题)。

    徽章要塞进一个小方块里,所以**只接受 1~2 个字符**的序号:
    `①` / `一、` / `1.` 可用;`第一节` 这种太长 —— 此时返回空徽章,
    badge 样式会改用自动递增的节号,同时把冗余的「第一节」从标题里去掉。
    """
    t = text.strip()
    m = _HEAD_NUM_RE.match(t)
    if not m:
        return "", t
    badge = m.group(1).rstrip("、.,.．")
    rest = t[m.end():].strip()
    if not rest:                       # 标题里只有序号,保留原文交给自动编号
        return "", t
    if not badge or len(badge) > 2:
        return "", rest
    return badge, rest


def _heading_html(text: str, level: int, S: dict[str, str],
                  counter: list[int] | None = None) -> str:
    """渲染小节标题。三种风格共用 `<h2>`,只有内联样式与徽章不同。"""
    body = text
    if level <= 2 and S.get("heading_style") == "badge":
        badge, plain = _split_heading_number(text)
        if not badge and counter is not None:
            counter[0] += 1
            badge = str(counter[0])
        body = (f'<span style="{S["badge"]}">{_escape(badge)}</span>{_inline(plain)}'
                if badge else _inline(plain))
    else:
        body = _inline(text)
    return f'<h{level} style="{S[f"h{level}"]}">{body}</h{level}>'


# ---------------- 块渲染 ----------------

def _render_ul(lines: list[str], S: dict[str, str]) -> str:
    items = ""
    for l in lines:
        txt = l.strip()[2:].strip()
        if txt:
            items += (f'<li style="{S["li"]}">'
                      f'<span style="{S["bullet"]}">▪ </span>{_inline(txt)}</li>')
    return f'<ul style="{S["ul"]}">{items}</ul>'


def _render_ol(lines: list[str], S: dict[str, str]) -> str:
    items = ""
    for l in lines:
        m = re.match(r"^(\d+)\.\s+(.*)$", l.strip())
        if m:
            num, txt = m.group(1), m.group(2)
            items += (f'<li style="{S["li"]}">'
                      f'<span style="{S["bullet"]}">{num}. </span>{_inline(txt)}</li>')
    return f'<ol style="{S["ol"]}">{items}</ol>'


def _render_paragraphs(text_lines: list[str], style: str) -> str:
    return "".join(
        f'<p style="{style}">{_inline(chunk)}</p>'
        for chunk in _split_paragraphs(text_lines)
    )


def _render_container(kind: str, lines: list[str], S: dict[str, str]) -> str:
    """把 `:::kind … :::` 渲染成视觉卡片(导语 / 论点 / 金句 / 结尾)。"""
    body = [l for l in lines if l.strip()]
    if not body:
        return ""
    if kind == "thesis":
        return (f'<section style="{S["thesis"]}">'
                f'<p style="{S["thesis_label"]}">核心论点</p>'
                f'{_render_paragraphs(body, S["thesis_p"])}'
                f'</section>')
    if kind == "lead":
        return (f'<section style="{S["lead"]}">'
                f'{_render_paragraphs(body, S["lead_p"])}'
                f'</section>')
    if kind == "quote":
        return (f'<section style="{S["kicker"]}">'
                f'<section style="{S["kicker_line"]}"></section>'
                f'{_render_paragraphs(body, S["kicker_p"])}'
                f'<section style="{S["kicker_line"]}"></section>'
                f'</section>')
    if kind == "cta":
        return (f'<section style="{S["cta"]}">'
                f'{_render_paragraphs(body, S["cta_p"])}'
                f'</section>')
    return _render_paragraphs(body, S["p"])


# ---------------- 主入口 ----------------

def markdown_to_html(md: str, accent: str | None = None,
                     theme: dict | None = None) -> str:
    """把 Markdown 正文转成适合公众号粘贴的 HTML 片段(全部内联样式)。

    `accent` / `theme` 可显式覆盖配置(预览与测试用),不传则读当前配置。
    输出外面再套一层 `<section>`,统一声明字体栈与基础字号 ——
    公众号编辑器保留内联 style,这样整篇排版不会因机型差异而跑掉。
    """
    t = dict(theme or current_theme())
    if accent:
        t["accent"] = accent
    S = _styles(t)
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    counter = [0]           # badge 样式的自动节号

    while i < len(lines):
        line = lines[i].rstrip()
        s = line.strip()

        if not s:                                  # 空行
            i += 1
            continue

        # ---- 容器块 :::kind ... ::: ----
        cm = _CONTAINER_START_RE.match(s)
        if cm and cm.group(1) in ("lead", "thesis", "quote", "cta"):
            kind = cm.group(1)
            i += 1
            buf: list[str] = []
            while i < len(lines) and not _CONTAINER_END_RE.match(lines[i].strip()):
                if _CONTAINER_START_RE.match(lines[i].strip()):
                    break                          # 缺少收尾标记,遇到下一个容器就停
                buf.append(lines[i].rstrip())
                i += 1
                if len(buf) > _CONTAINER_MAX:
                    break
            if i < len(lines) and _CONTAINER_END_RE.match(lines[i].strip()):
                i += 1
            out.append(_render_container(kind, buf, S))
            continue

        # ---- 代码块 ----
        if s.startswith("```"):
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            out.append(f'<pre style="{S["pre"]}">{_escape(chr(10).join(code))}</pre>')
            i += 1
            continue

        # ---- 分隔符(独占一行的 --- / *** / ___)----
        if _HR_RE.match(s):
            out.append(_render_hr(S))
            i += 1
            continue

        # ---- 独占一行的图片 ![说明](地址) ----
        im = _IMG_RE.fullmatch(s)
        if im:
            out.append(_render_image(im.group(1), im.group(2), S))
            i += 1
            continue

        # ---- ATX 标题(# ~ ####)----
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            level = min(len(m.group(1)), 4)
            out.append(_heading_html(m.group(2), level, S, counter))
            i += 1
            continue

        # ---- 引用块(可能多行)----
        if s.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(lines[i].lstrip()[1:].strip())
                i += 1
            inner = "".join(
                f'<p style="{S["quote_p"]}">{_inline(q)}</p>' for q in quote if q
            )
            out.append(f'<blockquote style="{S["quote"]}">{inner}</blockquote>')
            continue

        # ---- 隐式小标题(①标题。正文…)。放在列表判断之前 ----
        ih = _implicit_heading(s)
        if ih:
            head, rest = ih
            out.append(_heading_html(head, 2, S, counter))
            if rest:
                lines[i] = rest          # 剩余正文回填,原地继续处理
                continue
            i += 1
            continue

        # ---- 独占一行的 **短句**(结尾不带句末标点)才算小标题 ----
        bh = _bold_heading(s)
        if bh:
            out.append(_heading_html(bh, 2, S, counter))
            i += 1
            continue

        # ---- 无序列表(连续)----
        if re.match(r"^[-*+]\s+", s):
            block = []
            while i < len(lines) and re.match(r"^[-*+]\s+", lines[i].strip()):
                block.append(lines[i])
                i += 1
            out.append(_render_ul(block, S))
            continue

        # ---- 有序列表(连续)----
        if re.match(r"^\d+\.\s+", s):
            block = []
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i].strip()):
                block.append(lines[i])
                i += 1
            out.append(_render_ol(block, S))
            continue

        # ---- 独立短行 ⇒ 小标题(末行/后继过短除外,那更可能是收尾句)----
        sh = _is_short_heading(s, next_len=_next_nonempty_len(lines, i))
        if sh:
            out.append(_heading_html(sh, 2, S, counter))
            i += 1
            continue

        # ---- 普通段落:收集连续非空行,再按句末标点切成多段 ----
        para: list[str] = []
        while i < len(lines):
            cur = lines[i].strip()
            if not cur or _is_block_start(cur):
                break
            # 这一行其实是下一个块的开头(隐式编号小标题 / 独占一行的短加粗标题)。
            # 必须交回外层处理 —— 否则 ②③ 这类小节会被吞进本段落,视觉层级全丢。
            if _implicit_heading(cur) or _bold_heading(cur):
                break
            # 上一行已收句、本行又是独立短行 ⇒ 它是小标题,不是上一段的续行
            if para and _ends_sentence(para[-1]) and _is_short_heading(
                cur, next_len=_next_nonempty_len(lines, i)
            ):
                break
            para.append(cur)
            i += 1
        if not para:                   # 兜底:不该发生,但要保证不丢内容
            i += 1
            continue

        # 信息来源整段降级成小字灰字(URL 直接摆在正文里很碍眼)
        if _SOURCE_RE.match(para[0]):
            out.append(f'<p style="{S["source"]}">{_inline(_join(para))}</p>')
            continue
        for chunk in _split_paragraphs(para):
            out.append(f'<p style="{S["p"]}">{_inline(chunk)}</p>')

    body = "\n".join(x for x in out if x)
    wrap = (f"font-family:{FONT_STACK};font-size:16px;line-height:1.85;"
            f"letter-spacing:1px;color:{TEXT_COLOR};")
    return f'<section style="{wrap}">{body}</section>'


def article_to_markdown(article: dict) -> str:
    """把稿件字段组装成 Markdown(导语 → 论点 → 正文 → 金句 → 结尾 → 来源)。

    非正文段落用 `:::kind` 容器包裹,让它们在公众号里呈现为**明显不同的卡片** ——
    否则导语、论点、金句、结尾会和正文糊成一片,读者分不出重点。
    文案为空时自动跳过,不留空块。

    模型习惯把「信息来源」写在正文末尾,那样它会夹在金句和结尾之间;
    这里把它摘出来挪到**全文最后**,符合阅读顺序。
    """
    blocks: list[str] = []

    def add(kind: str, text: str) -> None:
        t = (text or "").strip()
        if t:
            blocks.append(f":::{kind}\n{t}\n:::")

    # 先摘出正文末尾的「信息来源」,稍后放到最后
    body = (article.get("body") or "").strip()
    source = ""
    if body:
        lines = body.split("\n")
        for idx in range(len(lines) - 1, -1, -1):
            if lines[idx].strip():
                if _SOURCE_RE.match(lines[idx].strip()):
                    source = lines[idx].strip()
                    lines[idx] = ""
                break
        body = "\n".join(lines).strip()

    add("lead", article.get("lead") or "")
    add("thesis", article.get("thesis") or "")
    if body:
        blocks.append(body)
    add("quote", article.get("quote") or "")
    add("cta", article.get("cta") or "")
    if source:
        blocks.append(source)
    return "\n\n".join(blocks)


def strip_containers(md: str) -> str:
    """去掉 `:::kind` 标记,供「方案 B」导出纯 Markdown 文本使用。

    论点保留加粗(它是全文最该被看到的一句),其余容器退化成普通段落。
    """
    out: list[str] = []
    kind: str | None = None
    for line in md.split("\n"):
        s = line.strip()
        cm = _CONTAINER_START_RE.match(s)
        if cm:
            kind = cm.group(1)
            continue
        if _CONTAINER_END_RE.match(s):
            kind = None
            continue
        if kind == "thesis" and s:
            out.append(f"**{s}**")
        else:
            out.append(line)
    return "\n".join(out)
