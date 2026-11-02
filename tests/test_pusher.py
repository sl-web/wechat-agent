"""推送模块测试:选择器配置 + Markdown 转换 + 方案 B + 推送编排(均不依赖浏览器)。"""
from __future__ import annotations

import re
import threading

from app.core.config import DEFAULTS
from app.core.db import connect, init_db
from app.pusher import service as ps
from app.pusher.client import plan_b_export
from app.pusher.markdown import markdown_to_html
from app.pusher.selectors import SelectorManager


def test_selectors_load():
    sm = SelectorManager()
    # 新版编辑器:标题/正文都是 ProseMirror,入口是 .new-creation__menu-item
    assert sm.get("editor", "prosemirror") is not None
    assert sm.get("editor", "article_item") is not None
    assert sm.get("editor", "save_btn") is not None
    missing = sm.check_required([("login", "after_login"), ("editor", "prosemirror")])
    assert missing == []


def test_check_page_ready_covers_editor_essentials():
    """改版自检必须覆盖编辑器关键元素,否则改版后会以"超时"形式暴露。"""
    from app.pusher.client import WechatPusher

    assert WechatPusher(headless=True).check_page_ready() == []


def test_selectors_missing_reported():
    sm = SelectorManager()
    missing = sm.check_required([("editor", "no_such_btn")])
    assert missing == ["editor.no_such_btn"]


# ---------------- 封面设置(2026-09-10 按真机结构重写) ----------------

def test_cover_selectors_are_scoped_to_picker_dialog():
    """封面用的 file input 必须**限定在素材库弹窗内**。

    这是真实故障的回归守卫:页面里第一个 `input[type=file]` 是**正文工具栏**的图片输入框,
    用裸选择器会把封面图传进正文图片通道 —— 上传"成功"、草稿封面却没变。
    """
    sm = SelectorManager()
    fi = sm.get("cover", "file_input")
    assert fi is not None
    value = fi["value"]
    assert "input[type=file]" in value
    assert "weui-desktop-dialog_img-picker" in value, "封面 file input 必须限定在素材库弹窗内"

    # 其余关键步骤的选择器都要在
    for name in ("cover_area", "from_gallery", "picker_dialog", "next_btn",
                 "confirm_btn", "preview"):
        assert sm.get("cover", name) is not None, f"缺少封面选择器: cover.{name}"


def test_cover_from_gallery_uses_class_not_text():
    """「从图片库选择」必须用 class 选择器。

    页面上有多个隐藏的重复浮层(首图/次图各一份),用 `text=从图片库选择` 会命中
    隐藏实例并一直等到超时 —— 用户看到的是"点了没反应"。
    """
    sm = SelectorManager()
    value = sm.get("cover", "from_gallery")["value"]
    assert "js_imagedialog" in value


def test_set_cover_returns_false_for_missing_file():
    """封面文件不存在时:返回 False 并给出可读原因,而不是抛异常。"""
    from app.pusher.client import WechatPusher

    p = WechatPusher(headless=True)
    warnings: list[str] = []
    assert p._set_cover("/no/such/cover.png", warnings) is False
    assert warnings and "不存在" in warnings[0]


def test_cover_applied_requires_visible_preview():
    """"成功"的判据是**封面预览可见**,不是"没抛异常"。

    设置前 `.js_cover_preview_new` 就在 DOM 里但不可见 —— 只看元素存在会误判成功。
    """
    import inspect

    from app.pusher.client import WechatPusher

    src = inspect.getsource(WechatPusher._cover_applied)
    assert "offsetParent" in src and "getBoundingClientRect" in src, "必须查可见性"
    # _set_cover 必须真的用它来判定返回值
    set_src = inspect.getsource(WechatPusher._set_cover)
    assert "_cover_applied()" in set_src


def test_js_click_visible_helper_exists():
    """点击可见元素的辅助必须存在(被 _set_cover 依赖)。"""
    import inspect

    from app.pusher.client import WechatPusher

    p = WechatPusher(headless=True)
    assert hasattr(p, "_js_click_visible")
    src = inspect.getsource(WechatPusher._set_cover)
    assert "_js_click_visible" in src


# ---- Markdown → 公众号富文本 ----
# 公众号编辑器会剥离 class,因此输出必须带**内联 style**;以下断言同时锁定
# 「内容正确」与「样式内联」两件事,防止有人改成 class 导致排版在公众号里丢失。

def test_markdown_headings_have_inline_style():
    html = markdown_to_html("# 标题一\n\n正文段落")
    assert "<h1" in html and "标题一" in html
    assert "style=" in html
    assert "<p style=" in html and "正文段落" in html


def test_markdown_bold_and_link_have_inline_style():
    html = markdown_to_html("这是**加粗**和[链接](https://example.com)")
    assert "<strong style=" in html and "加粗" in html
    assert '<a href="https://example.com" style=' in html and "链接" in html


def test_markdown_lists_styled():
    html = markdown_to_html("- 第一项\n- 第二项\n\n1. 有序一\n2. 有序二")
    assert "<ul style=" in html and "第一项" in html
    assert "<ol style=" in html and "有序一" in html
    assert "<li style=" in html


def test_markdown_quote_is_block_with_background():
    html = markdown_to_html("> 引用一句")
    assert "<blockquote style=" in html
    assert "background:" in html  # 引用块灰底
    assert "引用一句" in html


def test_markdown_code_block_styled():
    html = markdown_to_html("```python\nprint(1)\n```")
    assert "<pre style=" in html
    assert "print(1)" in html


def test_markdown_escapes_html():
    """正文里的尖括号必须转义,避免注入破坏编辑器 DOM。"""
    html = markdown_to_html("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_article_to_markdown_assembles_fields():
    """非正文段落要包成 ::: 容器 —— 否则导语/论点/金句/结尾会和正文糊成一片。"""
    from app.pusher.markdown import article_to_markdown

    md = article_to_markdown({
        "lead": "导语内容", "body": "正文段落", "quote": "金句", "cta": "结尾引导",
    })
    assert ":::lead" in md and "导语内容" in md
    assert ":::quote" in md and "金句" in md
    assert ":::cta" in md and "结尾引导" in md
    assert "正文段落" in md and ":::p" not in md      # 正文保持裸 Markdown
    assert md.index("导语内容") < md.index("正文段落") < md.index("金句") < md.index("结尾引导")


def test_article_to_markdown_wraps_thesis():
    from app.pusher.markdown import article_to_markdown

    md = article_to_markdown({"thesis": "这是一个明确的判断。", "body": "正文。"})
    assert ":::thesis" in md and "这是一个明确的判断。" in md


def test_strip_containers_for_plan_b():
    """方案 B 导出的是纯 Markdown,不能带 ::: 标记,论点保留加粗。"""
    from app.pusher.markdown import article_to_markdown, strip_containers

    md = article_to_markdown({"lead": "导语。", "thesis": "核心判断。", "body": "正文。"})
    plain = strip_containers(md)
    assert ":::" not in plain
    assert "导语。" in plain and "正文。" in plain
    assert "**核心判断。**" in plain


# ---- B 批:视觉排版 ----
# 公众号编辑器保留内联 style,所以"好不好看"完全取决于这些内联属性。
# 改动样式常量时,这些断言能立刻告诉你有没有把某处改回朴素样式。

def test_accent_color_drives_whole_palette():
    """主题色一处配置,应贯穿小节标题 / 论点卡 / 金句 / 列表符号,否则视觉不统一。"""
    from app.pusher.markdown import article_to_markdown

    art = {"thesis": "论点。", "body": "## 小节\n正文。\n\n- 条目一", "quote": "金句。"}
    html = markdown_to_html(article_to_markdown(art), accent="#993556")
    assert html.count("#993556") >= 5, "主题色出现在的地方太少,排版会显得零散"
    assert "#185fa5" not in html, "显式指定主题色后不应残留默认色"


def test_default_accent_used_when_not_specified():
    """不传 accent 时用主题自带的 accent;默认主题的 accent 就是 DEFAULT_ACCENT。

    ⚠️ **必须显式传 `theme=`**:`markdown_to_html` 不传时走 `current_theme()`,
    读的是**用户当前配置**(面板里能改 `mp_layout_theme`)—— 依赖它会让测试随用户配置漂移。
    真实踩过:主题改成「简约浅色」后,这条与下面两条一起变红,而代码其实没问题。
    """
    from app.pusher.markdown import DEFAULT_ACCENT, DEFAULT_THEME, LAYOUT_THEMES

    assert LAYOUT_THEMES[DEFAULT_THEME]["accent"] == DEFAULT_ACCENT
    assert DEFAULT_ACCENT in markdown_to_html("## 小节", theme=LAYOUT_THEMES[DEFAULT_THEME])


def test_body_font_size_and_line_height():
    """正文 15px → 16px、行距 1.75 → 1.85(手机上的可读性)。"""
    html = markdown_to_html("这是一段正文。")
    assert "font-size:16px" in html
    assert "line-height:1.85" in html


def test_heading_has_bar_and_tinted_background():
    """小节标题必须明显区别于正文:色块 + 浅底 + 圆角。

    用显式主题(bar 样式),不依赖用户当前配置 —— 理由见上一条测试的说明。
    """
    from app.pusher.markdown import LAYOUT_THEMES

    html = markdown_to_html("## 小节标题",
                            theme={**LAYOUT_THEMES["tech"], "heading_style": "bar"})
    assert "border-left:5px solid" in html
    assert "border-radius" in html
    assert "background:#" in html


def test_list_bullets_use_accent_symbol():
    html = markdown_to_html("- 条目一\n- 条目二")
    assert "list-style:none" in html
    assert "▪" in html


def test_thesis_card_has_label_and_tint():
    from app.pusher.markdown import article_to_markdown

    html = markdown_to_html(article_to_markdown({"thesis": "一个可被反驳的判断。"}))
    assert "核心论点" in html
    assert "border-left:5px solid" in html
    assert "一个可被反驳的判断。" in html


def test_source_line_is_small_and_grey():
    """信息来源里的 URL 直接摆在正文里很碍眼,要降级成小字灰字 + 上分隔线。"""
    html = markdown_to_html("信息来源: IT之家《某文》 https://example.com/a.htm")
    assert "font-size:13px" in html
    assert "border-top" in html
    assert "https://example.com/a.htm" in html


def test_output_wrapped_in_font_section():
    """整体套一层 section 声明字体栈,避免机型差异导致排版跑掉。"""
    html = markdown_to_html("正文。")
    assert html.startswith("<section style=")
    assert "font-family:" in html


def test_source_line_moved_to_the_end():
    """模型把「信息来源」写在正文末尾,要挪到全文最后,不能夹在金句前面。"""
    from app.pusher.markdown import article_to_markdown

    md = article_to_markdown({
        "body": "正文内容。\n\n信息来源: 某媒体 https://example.com/a.htm",
        "quote": "金句一句。",
        "cta": "欢迎反论。",
    })
    assert md.index("正文内容。") < md.index("金句一句。") < md.index("欢迎反论。")
    assert md.index("信息来源") > md.index("欢迎反论。"), "信息来源应在最末"
    assert "https://example.com/a.htm" in md


# ---- C 批:排版主题 / 标题样式 / 分隔符 / 配图 ----

def test_theme_presets_all_render():
    """三套预设都要能出完整 HTML(不能有哪套因为参数缺失而崩)。"""
    from app.pusher.markdown import LAYOUT_THEMES

    for key in LAYOUT_THEMES:
        html = markdown_to_html("## 小节标题\n正文内容。", theme=LAYOUT_THEMES[key])
        assert "<h2" in html and "font-family:" in html
        assert LAYOUT_THEMES[key]["accent"] in html


def test_theme_presets_have_distinct_accent():
    from app.pusher.markdown import LAYOUT_THEMES

    accents = {t["accent"] for t in LAYOUT_THEMES.values()}
    assert len(accents) == len(LAYOUT_THEMES), "各预设主题色应互不相同"


def test_heading_style_bar():
    from app.pusher.markdown import LAYOUT_THEMES

    html = markdown_to_html("## 小节", theme={**LAYOUT_THEMES["tech"], "heading_style": "bar"})
    assert "border-left:5px solid" in html and "background:#" in html


def test_heading_style_badge_uses_existing_number():
    """① 这类序号要被拆出来做成徽章,而不是留在标题文字里。"""
    from app.pusher.markdown import LAYOUT_THEMES

    theme = {**LAYOUT_THEMES["tech"], "heading_style": "badge"}
    html = markdown_to_html("## ①计费规则暴露成本焦虑", theme=theme)
    assert ">①</span>" in html
    assert "计费规则暴露成本焦虑" in html


def test_heading_style_badge_auto_numbers():
    """标题没有序号时,徽章用自动递增的节号。"""
    from app.pusher.markdown import LAYOUT_THEMES

    theme = {**LAYOUT_THEMES["tech"], "heading_style": "badge"}
    html = markdown_to_html("## 第一节\n正文。\n\n## 第二节\n正文。", theme=theme)
    assert ">1</span>" in html and ">2</span>" in html


def test_heading_style_center():
    from app.pusher.markdown import LAYOUT_THEMES

    html = markdown_to_html("## 小节", theme={**LAYOUT_THEMES["tech"], "heading_style": "center"})
    assert "text-align:center" in html and "border-bottom" in html


def test_divider_renders_decoration():
    """独占一行的 --- 要变成居中小装饰,而不是三个连字符留在正文里。"""
    html = markdown_to_html("正文一段。\n\n---\n\n正文二段。")
    assert "---" not in html
    assert "border-radius:50%" in html


def test_image_block_with_alt_becomes_caption():
    html = markdown_to_html("![配图说明](https://example.com/a.png)")
    assert 'src="https://example.com/a.png"' in html
    assert "配图说明" in html
    assert "max-width:100%" in html


def test_image_missing_local_file_degrades_to_text():
    """图片路径不存在时不能崩,要退化成可见的占位文字。"""
    html = markdown_to_html("![说明](no/such/file.png)")
    assert "<img" not in html
    assert "图:" in html


def test_local_image_inlined_as_base64(tmp_path):
    """本地图片要内嵌成 base64 —— 公众号粘贴时会自行上传到微信图床。"""
    import base64

    png = tmp_path / "x.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    ))
    html = markdown_to_html(f"![小图]({png})")
    assert "data:image/png;base64," in html


def test_image_line_not_treated_as_inline_link():
    """图片必须排在链接规则之前,否则 ![alt](src) 会被链接规则吃掉。"""
    html = markdown_to_html("![说明](https://example.com/a.png)")
    assert "<a href=" not in html


# ---- C 批:封面图(模板渲染,不需要浏览器)----

def test_cover_all_styles_build_wide():
    from app.pusher import cover as cv

    for style in cv.COVER_STYLES:
        html = cv.build_html("语音分层实为算力账单切割", "光盛之路", "AI · 产业观察", style)
        assert "900px" in html and "383px" in html, f"{style} 尺寸不对"
        assert "光盛之路" in html and "AI · 产业观察" in html
        assert "语音分层实为算力账单切割" in html


def test_cover_square_is_200():
    from app.pusher import cover as cv

    html = cv.build_html("标题", "光盛之路", "AI", "dark", "square")
    assert "200px" in html


def test_cover_long_title_shrinks_font():
    """长标题必须自动缩字号,否则会溢出安全区。"""
    from app.pusher import cover as cv

    assert cv._title_size("短标题", 52) == 52
    assert cv._title_size("这是一个相当长的文章标题" * 2, 52) < 52


def test_cover_defaults_when_fields_empty():
    from app.pusher import cover as cv

    html = cv.build_html("", "", "", "dark")
    assert "未命名文章" in html and "公众号" in html


def test_cover_declares_chinese_font():
    """无头浏览器没有默认中文字体会渲染成方框 —— 字体栈必须显式声明。"""
    from app.pusher import cover as cv

    assert "Microsoft YaHei" in cv.FONT
    assert "Microsoft YaHei" in cv.build_html("标题", "品牌", "", "light")


def test_cover_path_none_when_missing():
    from app.pusher import service as ps

    assert ps.cover_path(999999) is None


def test_generate_cover_does_not_deadlock(monkeypatch):
    """`_lock` 是普通 Lock(不可重入),而 `_begin`/`_mark`/`_step` 内部会再次获取它。

    如果实现里在 `with _lock:` 内部调用这些函数,就会把自己锁死 ——
    对外表现为**请求永远挂住**(连访问日志都不会出现),极难排查。
    这条用例用超时把这种写法钉死。
    """
    from app.pusher import service as ps

    monkeypatch.setattr(ps, "_spawn_runner",
                        lambda *a, **k: {"ok": True, "result": {"wide": "x.png"}})
    box: dict = {}

    def run():
        box["r"] = ps.generate_cover(424242)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "generate_cover 死锁:检查是否在持有 _lock 时调用了 _begin/_mark/_step"
    assert box["r"]["ok"] is True


def test_article_to_markdown_skips_empty_fields():
    from app.pusher.markdown import article_to_markdown

    md = article_to_markdown({"title": "只有标题", "body": "   ", "quote": "", "cta": ""})
    assert md == ""


# ---- 正文分段与隐式小标题 ----
# 真实踩过的坑:LLM 用**单换行**分段,而标准 Markdown 把单换行当"段内软换行"。
# 照标准合并 ⇒ 1100 字正文挤进一个 <p>,手机上看就是一坨灰,
# 用户反馈"段落处理太死板"。下面几条锁住这个行为。
# 另一个坑:模型经常用 ①②③ 纯文本序号分段,视觉上没有层级 —— 必须提升为小标题。

def test_single_newline_splits_paragraphs():
    html = markdown_to_html("第一段结束。\n第二段结束。\n第三段结束。")
    assert html.count("<p ") == 3


def test_long_body_not_collapsed_into_one_paragraph():
    body = "\n".join(f"这是第 {i} 个段落的结语。" for i in range(1, 9))
    html = markdown_to_html(body)
    assert html.count("<p ") == 8, "长正文被合并成一个段落 —— 排版会变成一坨"


def test_soft_wrapped_lines_stay_one_paragraph():
    """没有句末标点的续行应与上一行合并,不能拆成两段。"""
    html = markdown_to_html("这一句没写完\n接着写下去。")
    assert html.count("<p ") == 1


def test_implicit_numbered_headings_promoted():
    """①②③ 不能只是正文字符,必须变成真正的小标题。"""
    md = (
        "①计费规则暴露成本焦虑。" + "正文内容" * 12 + "\n"
        "②套餐分层对应算力配给。" + "更多内容" * 12
    )
    html = markdown_to_html(md)
    heads = re.findall(r"<h2[^>]*>(.*?)</h2>", html)
    assert "计费规则暴露成本焦虑" in heads
    assert "套餐分层对应算力配给" in heads


def test_short_standalone_line_becomes_heading():
    body = "真正的变量在哪\n" + "它藏在留存率里,而不是补贴力度。" * 3
    html = markdown_to_html(body)
    assert "<h2" in html and "真正的变量在哪" in html


def test_trailing_short_line_is_not_heading():
    """末尾短行更像收尾句,不能当小标题。"""
    html = markdown_to_html("这是正文内容。\n这就是代价")
    assert "<h2" not in html
    assert "这就是代价" in html


def test_short_line_without_following_body_is_not_heading():
    """软换行留下的半句话不应变成标题。"""
    html = markdown_to_html("这一句没写完\n接着写。")
    assert "<h2" not in html


def test_long_bold_line_is_not_heading():
    """长加粗句是重点句(thesis),不是小标题。"""
    md = "**" + "这是一个很长的核心论点" * 3 + "**"
    html = markdown_to_html(md)
    assert "<h2" not in html
    assert "<strong" in html


def test_bold_sentence_is_not_heading():
    """带句末标点的加粗句是重点句,不是小标题。"""
    html = markdown_to_html("**按条计价,本质是给算力装一道阀门。**")
    assert "<h2" not in html
    assert "<strong" in html


def test_headings_carry_inline_style():
    """标题必须带**内联**样式(公众号编辑器会剥离 class,只留内联 style)。

    同样显式传主题:不传就读用户当前配置,测试会随面板设置漂移。
    """
    from app.pusher.markdown import LAYOUT_THEMES

    html = markdown_to_html("## 小节标题",
                            theme={**LAYOUT_THEMES["tech"], "heading_style": "bar"})
    assert "<h2" in html and "border-left" in html


def test_markdown_body_line_never_dropped():
    """以 * 开头的普通行曾经被静默丢弃(段落收集把 `*` 当块起始)。"""
    html = markdown_to_html("**重点句。**")
    assert "重点句" in html


def test_realistic_article_structure():
    """端到端骨架:导语卡 / 论点卡 / 小节标题 / 重点句 / 金句块 各就各位。"""
    from app.pusher.markdown import article_to_markdown

    art = {
        "lead": "导语一句。",
        "thesis": "**核心论点是明确的判断,而且可以被反驳。**",
        "body": "## 第一节\n正文内容一句。\n\n**这是重点句。**\n\n## 第二节\n又一段内容。",
        "quote": "金句一句。",
        "cta": "欢迎提出反论。",
    }
    html = markdown_to_html(article_to_markdown(art))
    assert html.count("<h2") == 2, "两个 ## 小节应各生成一个 h2"
    assert html.count("<strong") == 2, "论点与重点句应加粗,而不是变成标题"
    assert "核心论点" in html, "论点应有独立卡片与标签"
    assert "金句一句。" in html


def test_plan_b_export():
    art = {"title": "测试标题", "lead": "导语内容", "body": "正文段落内容"}
    text = plan_b_export(art)
    assert text.startswith("# 测试标题")
    assert "正文段落内容" in text


# ---- 登录态判定 ----
# 真实踩过的坑:把通用布局类 `.weui-desktop-layout__side` 当作登录成功特征,
# 结果**没扫码也判定为登录成功**,存下一个无效会话,后续推送全部莫名其妙失败。

class _FakeLocator:
    """可见性语义:0=不存在,1=存在且可见,2=存在但隐藏。"""

    def __init__(self, n: int):
        self._n = n

    def count(self) -> int:
        return 1 if self._n else 0

    @property
    def first(self):
        return self

    def is_visible(self) -> bool:
        return self._n == 1


class _FakePage:
    def __init__(self, mapping: dict, url: str = "https://mp.weixin.qq.com/"):
        self.mapping = mapping
        self.url = url

    def locator(self, sel: str) -> _FakeLocator:
        return _FakeLocator(self.mapping.get(sel, 0))


_LOGIN_URL = "https://mp.weixin.qq.com/"
_HOME_URL = "https://mp.weixin.qq.com/cgi-bin/home?t=home/index&lang=zh_CN&token=1234567890"


def test_logged_in_by_url_after_scan():
    """最可靠判据:登录后 URL 跳到 /cgi-bin/ 且带 token。"""
    from app.pusher.client import WechatPusher

    page = _FakePage({".login__type__container__scan": 2}, url=_HOME_URL)  # 登录容器残留但隐藏
    assert WechatPusher()._logged_in(page) == "url:token"


def test_not_logged_in_while_qrcode_visible():
    """二维码可见 ⇒ 一定没登录,即使别的特征命中。"""
    from app.pusher.client import WechatPusher

    page = _FakePage({".login__type__container__scan": 1, ".weui-desktop__sidebar": 1},
                     url=_LOGIN_URL)
    assert WechatPusher()._logged_in(page) == ""


def test_not_logged_in_on_plain_login_page():
    from app.pusher.client import WechatPusher

    page = _FakePage({".login__type__container__scan": 1}, url=_LOGIN_URL)
    assert WechatPusher()._logged_in(page) == ""


def test_hidden_login_container_does_not_block():
    """SPA 登录后常残留**隐藏**的登录容器,不能因此判定未登录(真实踩过的坑)。"""
    from app.pusher.client import WechatPusher

    page = _FakePage(
        {".login__type__container__scan": 2, ".weui-desktop__sidebar": 1},
        url="https://mp.weixin.qq.com/",  # URL 还没跳转,只能靠选择器
    )
    assert WechatPusher()._logged_in(page) == ".weui-desktop__sidebar"


def test_logged_in_via_fallback_selector():
    """主选择器失效时,兜底候选要能顶上(抗页面改版)。"""
    from app.pusher.client import WechatPusher

    page = _FakePage({"text=新的创作": 1}, url=_LOGIN_URL)
    assert WechatPusher()._logged_in(page) == "text=新的创作"


def test_login_selectors_exclude_generic_layout_classes():
    """登录成功特征里不允许出现通用布局类(会在登录页误命中)。"""
    from app.pusher.client import WechatPusher

    for sel in WechatPusher.LOGGED_IN_SELECTORS:
        assert "layout__side" not in sel
        assert "layout__main" not in sel


def test_plan_b_export_includes_cta_and_quote():
    text = plan_b_export({
        "title": "T", "lead": "L", "body": "B", "quote": "Q", "cta": "C",
    })
    assert "Q" in text and "C" in text


def test_plan_b_html_is_styled():
    """方案 B 的富文本版必须带内联样式,否则粘进公众号会丢排版。"""
    from app.pusher.client import plan_b_html

    html = plan_b_html({"title": "T", "body": "第一段**加粗**"})
    assert "<p style=" in html
    assert "<strong style=" in html


# ---------------- 推送编排 ----------------

def _push_db(tmp_path):
    db = tmp_path / "push.db"
    init_db(db)
    conn = connect(db)
    tid = conn.execute(
        """INSERT INTO hot_topics(source, fetch_mode, title, url, fetch_date)
           VALUES('rss','rss','某热点','http://t/1','2026-09-10')"""
    ).lastrowid
    aid = conn.execute(
        """INSERT INTO articles(topic_id, template_id, structure_id, title, lead, body, status)
           VALUES(?,?,?,?,?,?,?)""",
        (tid, "tpl_ai_news_v1", "s1", "待推送文章", "导语内容",
         "正文内容" * 60, "approved"),
    ).lastrowid
    conn.commit()
    conn.close()
    return db, aid


def test_session_status_reports_limits(tmp_path):
    """额度取自传入的库(空库 ⇒ 回落到默认值),不读线上配置。"""
    db, _ = _push_db(tmp_path)
    st = ps.session_status(db_path=str(db))
    default_limit = DEFAULTS["mp_daily_draft_limit"]
    assert st["daily_limit"] == default_limit
    assert st["pushed_today"] == 0
    assert st["remaining_today"] == default_limit
    assert st["session_exists"] in (True, False)  # 本机可能已扫码


def test_push_blocked_when_daily_limit_reached(tmp_path, monkeypatch):
    """风控保护:当日额度用尽时同步拒绝,不启动浏览器。

    额度从当前配置读取而不是写死 5 —— 用户会在控制面板调这个值,
    写死会让测试因为"线上配置变了"而假失败(真实踩过)。
    """
    db, aid = _push_db(tmp_path)
    limit = ps.session_status()["daily_limit"]
    monkeypatch.setattr(ps, "_pushed_today", lambda db_path=None: limit)

    r = ps.start_push(aid)
    assert r["ok"] is False
    assert "上限" in r["error"]
    assert "明天" in r["hint"]


def test_start_push_rejected_while_login_running(tmp_path, monkeypatch):
    """浏览器不能并发:登录进行中时拒绝推送。"""
    db, aid = _push_db(tmp_path)
    monkeypatch.setattr(ps, "_pushed_today", lambda db_path=None: 0)
    with ps._lock:
        ps._state["login"]["running"] = True
    try:
        r = ps.start_push(aid)
        assert r["ok"] is False
        assert "登录" in r["error"]
    finally:
        with ps._lock:
            ps._state["login"]["running"] = False


def test_start_login_rejected_while_pushing():
    with ps._lock:
        ps._state["login"]["running"] = False
        ps._state["push"]["running"] = True
    try:
        r = ps.start_login()
        assert r["ok"] is False
        assert "推送" in r["error"]
    finally:
        with ps._lock:
            ps._state["push"]["running"] = False


def test_plan_b_text_from_db(tmp_path):
    db, aid = _push_db(tmp_path)
    r = ps.plan_b_text(aid, db_path=str(db))
    assert r["ok"] is True
    assert r["markdown"].startswith("# 待推送文章")
    assert "导语内容" in r["markdown"]
    # HTML 版同样要能拿到,供「复制富文本」使用
    assert "<p style=" in r["html"]


def test_plan_b_text_missing_article(tmp_path):
    db, _ = _push_db(tmp_path)
    r = ps.plan_b_text(99999, db_path=str(db))
    assert r["ok"] is False
    assert "不存在" in r["error"]
