"""公众号草稿推送客户端(PRD 5.4)。

Playwright 浏览器自动化:登录(session 复用/扫码)→ 新建图文 → 填内容 → 上传封面 → 保存草稿。

设计要点:
- `storage_state.json` 复用登录会话,过期则提示重新扫码
- 正文用**粘贴事件**注入(直接写 innerHTML 编辑器不认,ProseMirror 只响应 paste/beforeinput)
- 封面尽力上传,**失败不阻断**(记录 warning,草稿仍保存,人工在后台补)
- 失败自动截图留证(logs/screenshots/)
- 选择器集中管理(selectors.yaml),页面改版/风控失败 → 抛 PushError,由上层降级方案 B
"""
from __future__ import annotations

import time
from pathlib import Path

from app.core.logger import get_logger
from app.core.settings import settings
from app.pusher.markdown import article_to_markdown, markdown_to_html, strip_containers
from app.pusher.selectors import SelectorManager

logger = get_logger("pusher")

MP_URL = "https://mp.weixin.qq.com"

# 正文注入:构造 ClipboardEvent(paste) 交给编辑器,ProseMirror 才会
# 把它当作真实粘贴处理并更新内部文档模型。直接改 innerHTML 会导致
# 「界面看着有内容、保存后却是空的」。
_INJECT_JS = """
({ html, text, nth }) => {
  const els = document.querySelectorAll('.ProseMirror');
  const el = els[nth < 0 ? els.length + nth : nth] || els[0];
  if (!el) return 'no-editor';
  el.scrollIntoView({ block: 'center' });
  el.focus();
  const dt = new DataTransfer();
  dt.setData('text/html', html);
  dt.setData('text/plain', text);
  // 返回 false 属正常:ProseMirror 会 preventDefault 并自行处理
  el.dispatchEvent(new ClipboardEvent('paste', {
    clipboardData: dt, bubbles: true, cancelable: true,
  }));
  return 'ok';
}
"""

_EDITOR_TEXT_JS = """
({ nth }) => {
  const els = document.querySelectorAll('.ProseMirror');
  const el = els[nth < 0 ? els.length + nth : nth] || els[0];
  return el ? (el.innerText || '').trim().length : -1;
}
"""


class PushError(RuntimeError):
    """推送失败(含风控/页面改版判定)。"""


def state_path() -> Path:
    return settings.data_dir / "storage_state.json"


def session_exists() -> bool:
    return state_path().exists()


class WechatPusher:
    def __init__(self, headless: bool = False, selector_path: str | None = None):
        self.headless = headless
        self.sel = SelectorManager(selector_path) if selector_path else SelectorManager()
        self._page = None

    # ---------- 浏览器 ----------
    def _browser(self, use_session: bool = True):
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=self.headless)
        state = state_path()
        # 强制重新登录时**不加载**旧会话:否则页面带着 cookie 自动登录,
        # 二维码根本不会出现 —— 用户点了「重新扫码」却只看到窗口一闪而过。
        storage = str(state) if (use_session and state.exists()) else None
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            storage_state=storage,
            locale="zh-CN",
        )
        page = context.new_page()
        return pw, browser, context, page

    # 登录成功后的页面特征(任一命中即可)。多候选是为了扛页面改版:
    # 原来只认一个选择器,一旦改版就会「明明登录成功却一直等到超时」。
    # 注意:只放登录页**不会出现**的特征。像 `.weui-desktop-layout__side` 这种
    # 通用布局类在登录页也存在,放进来会导致「没扫码也判定登录成功」。
    LOGGED_IN_SELECTORS = (
        ".weui-desktop__sidebar",
        ".weui-desktop-account__nickname",
        "text=新的创作",
        "text=图文消息",
    )
    # 这些一旦出现,说明还停在登录/扫码页 —— 优先级高于上面的命中
    QRCODE_SELECTORS = (".login__type__container__scan", "text=扫码登录", ".login_frame")

    def _url_logged_in(self, page) -> str:
        """URL 判据(最可靠):登录成功后跳到 /cgi-bin/ 且带 token。

        公众号后台是 SPA,登录后 DOM 里可能**仍保留隐藏的登录容器**,
        所以纯靠选择器会误判;URL 不会骗人。
        """
        try:
            url = page.url or ""
        except Exception:  # noqa: BLE001
            return ""
        # 登录成功后 URL 形如 https://mp.weixin.qq.com/cgi-bin/home?t=...&token=1234567890
        if "mp.weixin.qq.com" in url and "token=" in url:
            return "url:token"
        if "mp.weixin.qq.com/cgi-bin" in url:
            return "url:cgi-bin"
        return ""

    def _logged_in(self, page) -> str:
        """返回命中的判据(空串表示未登录)。"""
        hit = self._url_logged_in(page)
        if hit:
            return hit
        # 二维码**可见** ⇒ 还没登录。注意用 is_visible 而非 count:
        # count() 会把隐藏元素也算进来,SPA 登录后残留的登录容器会让判定永远失败。
        for sel in self.QRCODE_SELECTORS:
            try:
                if page.locator(sel).first.is_visible():
                    return ""
            except Exception:  # noqa: BLE001
                pass
        for sel in self.LOGGED_IN_SELECTORS:
            try:
                if page.locator(sel).first.is_visible():
                    return sel
            except Exception:  # noqa: BLE001
                continue
        return ""

    def _pick_account(self, page) -> bool:
        """微信账号管理多个公众号时会出现「选择帐号」页,自动选第一个。"""
        try:
            for marker in ("text=选择帐号", "text=选择公众号"):
                if page.locator(marker).count() == 0:
                    continue
                item = page.locator(
                    ".weui-desktop-account__item, .login__type__container__account__item"
                ).first
                if item.count() > 0:
                    item.click(timeout=5000)
                    logger.info("检测到多公众号,已自动选择第一个")
                    page.wait_for_timeout(2000)
                    return True
        except Exception as e:  # noqa: BLE001
            logger.warning("选择帐号步骤异常(继续等待): %s", e)
        return False

    def open(self, scan_wait_seconds: int = 600, force: bool = False,
             on_qrcode=None) -> None:
        """打开后台并确保处于登录态。

        force=False:已有有效会话时免扫码直接进入(推送时的常规路径);
        force=True :**忽略**已有会话,一定走扫码流程 —— 用于「重新扫码登录」。
                     不这么做的话,浏览器会带着旧 cookie 自动登录,
                     用户点了重新扫码却完全看不到二维码。

        on_qrcode:二维码确认出现后的回调(供上层上报进度)。
        """
        pw, browser, context, page = self._browser(use_session=not force)
        self._pw, self._browser_inst, self._context, self._page = pw, browser, context, page
        page.goto(MP_URL, wait_until="domcontentloaded")

        if not force and self._logged_in(page):
            logger.info("会话有效,免登录进入")
            return
        if force:
            logger.info("强制重新登录:已忽略本地会话,等待二维码")

        # 轮询前先确认二维码**真的出现了**(用户点重新扫码就是为了看到它)。
        # 逐个探测而非拼成一个选择器串 —— 不同引擎(text=/css)混排容易被解析器误读。
        qr_shown = ""
        for sel in self.QRCODE_SELECTORS:
            try:
                page.wait_for_selector(sel, timeout=7000, state="visible")
                qr_shown = sel
                break
            except Exception:  # noqa: BLE001
                continue
        if qr_shown:
            logger.info("登录二维码已显示(命中 %s),等待人工扫码", qr_shown)
            if on_qrcode:
                try:
                    on_qrcode()
                except Exception:  # noqa: BLE001
                    pass
        else:
            logger.warning("未检测到二维码元素(可能页面已改版):当前 URL=%s", page.url)

        logger.warning("需要扫码登录,请在手机微信扫码(最多等 %s 秒)", scan_wait_seconds)
        deadline = time.time() + scan_wait_seconds
        tick = 0
        while time.time() < deadline:
            hit = self._logged_in(page)
            if hit:
                context.storage_state(path=str(state_path()))
                logger.info("扫码成功,会话已保存(命中特征: %s)", hit)
                return
            self._pick_account(page)
            tick += 1
            # 每约 6 秒留一次现场:URL 进日志 + 定名截图,便于事后定位改版/风控
            if tick % 4 == 0:
                try:
                    logger.info("等待扫码中… 当前 URL=%s", page.url)
                except Exception:  # noqa: BLE001
                    pass
                self._screenshot("live_login", fixed=True)
            page.wait_for_timeout(1500)

        self._screenshot("login_timeout")
        try:
            logger.error("扫码超时,最后停在: %s", page.url)
        except Exception:  # noqa: BLE001
            pass
        raise PushError(
            f"扫码超时({scan_wait_seconds} 秒内未完成)。"
            "请重新点击「扫码登录」,并在浏览器窗口出现后用手机微信扫码。"
        )

    # 兼容旧调用名
    login = open

    # ---------- 工具 ----------
    def _sel(self, group: str, name: str) -> dict:
        s = self.sel.get(group, name)
        if not s:
            raise PushError(f"选择器缺失: {group}.{name},可能页面改版 —— 请更新 selectors.yaml")
        return s

    def _try(self, group: str, name: str, timeout_ms: int = 6000) -> bool:
        """尝试点击某元素,失败返回 False(用于可跳过的步骤)。"""
        s = self.sel.get(group, name)
        if not s:
            return False
        try:
            self._page.click(s["value"], timeout=timeout_ms)
            return True
        except Exception:
            return False

    # ---------- 建稿 ----------
    def push_draft(
        self,
        title: str,
        author: str,
        markdown_body: str,
        cover_image: str | None = None,
    ) -> dict:
        """创建草稿。

        返回 {draft_url, cover_uploaded, body_chars, warnings}
        """
        if self._page is None:
            raise PushError("未登录,请先调用 open()")
        warnings: list[str] = []
        try:
            self._enter_editor(warnings)
            page = self._page
            n_ed = self._prosemirrors().count()

            # ① 标题(新版编辑器标题槽也是 ProseMirror,取第 1 个)
            if n_ed >= 2:
                self._click_editor(self._prosemirrors().nth(0), "标题框")
                page.keyboard.press("Control+a")
                page.keyboard.press("Delete")
                page.keyboard.insert_text(title)
                page.wait_for_timeout(400)
            else:
                warnings.append("未找到标题输入槽(编辑器可能改版),标题需手动补")

            # ② 作者(可选)
            if author:
                a_sel = self.sel.get("editor", "author_input")
                if a_sel and page.locator(a_sel["value"]).count() > 0:
                    try:
                        page.locator(a_sel["value"]).first.fill(author, timeout=6000)
                    except Exception:  # noqa: BLE001
                        warnings.append("作者栏填写失败,需手动补")
                else:
                    warnings.append("未找到「作者」输入框,作者名未填")

            # ③ 正文(粘贴富文本;最后一个 ProseMirror)
            if n_ed == 0:
                raise PushError("未找到正文编辑器(.ProseMirror),页面可能已改版")
            body_nth = 1 if n_ed >= 2 else 0
            b_ed = self._prosemirrors().nth(body_nth)
            self._click_editor(b_ed, "正文编辑器")
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")
            page.wait_for_timeout(300)
            html = markdown_to_html(markdown_body)
            page.evaluate(_INJECT_JS, {"html": html, "text": markdown_body, "nth": body_nth})
            page.wait_for_timeout(1200)

            chars = page.evaluate(_EDITOR_TEXT_JS, {"nth": body_nth})
            if not chars or chars < 20:
                warnings.append("正文注入后内容异常偏少,请保存后到后台核对")
            self._screenshot("draft_filled")

            # ④ 封面(尽力,失败不阻断)
            cover_uploaded = False
            if cover_image:
                cover_uploaded = self._set_cover(cover_image, warnings)

            # ⑤ 保存(保存前再清一次可能的弹窗)
            self._dismiss_modals()
            save_sel = self._sel("editor", "save_btn")
            page.click(save_sel["value"], timeout=25000)
            page.wait_for_timeout(3000)
            self._screenshot("draft_saved")

            return {
                "draft_url": self._grab_draft_url(),
                "cover_uploaded": cover_uploaded,
                "body_chars": chars if chars and chars > 0 else len(markdown_body),
                "warnings": warnings,
            }
        except PushError:
            self._screenshot("push_failed")
            raise
        except Exception as e:
            self._screenshot("push_error")
            raise PushError(f"推送草稿失败: {type(e).__name__}: {e}") from e

    def _prosemirrors(self):
        """新版编辑器的富文本槽(标题 + 正文都是 ProseMirror)。"""
        return self._page.locator(self._sel("editor", "prosemirror")["value"])

    # 编辑器里的引导/公告弹窗(首次打开常见),不关掉会挡住整个编辑区,
    # 表现为「.ProseMirror 点不到、等待 20 秒超时」。
    _DISMISS_TEXTS = ("我知道了", "知道了", "开始使用", "知道了 ", "确定", "关闭")

    def _dismiss_modals(self) -> int:
        """关掉阻塞式引导弹窗,返回关掉的个数。

        用 force=True:弹窗常带半透明遮罩,普通 click 的可点击性检查会失败。
        """
        closed = 0
        for t in self._DISMISS_TEXTS:
            try:
                btn = self._page.locator(f"text={t}").first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=2500, force=True)
                    self._page.wait_for_timeout(700)
                    closed += 1
            except Exception:  # noqa: BLE001
                continue
        # 兜底:点弹窗右上角关闭
        try:
            x = self._page.locator(".weui-desktop-dialog__close-btn").first
            if x.count() > 0 and x.is_visible():
                x.click(timeout=2000, force=True)
                self._page.wait_for_timeout(500)
                closed += 1
        except Exception:  # noqa: BLE001
            pass
        if closed:
            logger.info("已尝试关闭 %d 个编辑区弹窗", closed)
        return closed

    def _wait_editor_clickable(self, timeout: float = 45.0) -> bool:
        """等编辑器真正可交互(要求**连续两轮**都可点,避开延迟弹出的弹窗)。

        引导弹窗常在编辑器加载后 2~5 秒才出现,单次探测容易误判。
        """
        deadline = time.time() + timeout
        rounds = 0
        streak = 0
        while time.time() < deadline:
            rounds += 1
            self._dismiss_modals()
            try:
                self._prosemirrors().first.click(timeout=1500, trial=True)
                streak += 1
                if streak >= 2:
                    logger.info("编辑器可交互(第 %d 轮,连续 %d 次通过)", rounds, streak)
                    return True
            except Exception:  # noqa: BLE001
                streak = 0
            try:
                self._page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            self._page.wait_for_timeout(1500)
        self._screenshot("editor_not_clickable")
        return False

    def _click_editor(self, locator, what: str, timeout: float = 25.0) -> None:
        """编辑器内点击。弹窗随时可能弹出遮挡,失败就清一次再试。"""
        deadline = time.time() + timeout
        last_err: Exception | None = None
        while time.time() < deadline:
            self._dismiss_modals()
            try:
                locator.click(timeout=3000)
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                self._page.wait_for_timeout(900)
        raise PushError(f"{what} 无法点击(可能被弹窗遮挡):{last_err}")

    def _enter_editor(self, warnings: list[str]) -> None:
        """从首页进入图文编辑器(已在编辑器内则跳过)。

        ️实测:点击「文章」会**打开新标签页**。若不切页,后续会一直停在首页
        等 `#title`,表现为 20 秒超时——这是最初推送失败的直接原因。
        """
        if "appmsg_edit" in (self._page.url or ""):
            return  # 已在编辑器
        try:
            self._page.wait_for_selector(
                self._sel("editor", "prosemirror")["value"], timeout=2500
            )
            return  # 当前页就是编辑器
        except Exception:
            pass

        item_sel = self.sel.get("editor", "article_item")
        if not item_sel:
            raise PushError("选择器缺失: editor.article_item,请更新 selectors.yaml")
        try:
            item = self._page.locator(item_sel["value"]).filter(has_text="文章").first
            with self._context.expect_page(timeout=25000) as info:
                item.click(timeout=15000)
            new_page = info.value
            new_page.wait_for_load_state("domcontentloaded")
            self._page = new_page  # 关键:切到新标签页
            logger.info("已在新标签页打开图文编辑器")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"未检测到新标签页({type(e).__name__}),尝试在当前页继续")

        # 等编辑器富文本槽出现,并确认**真的能点**(弹窗遮罩会让它不可点)
        self._page.wait_for_selector(self._sel("editor", "prosemirror")["value"], timeout=30000)
        if not self._wait_editor_clickable(45):
            warnings.append("编辑器被弹窗遮挡,自动清理失败:可能有新的引导弹窗")

    def _js_click_visible(self, selector: str) -> bool:
        """点击**可见的**第一个匹配元素,成功返回 True。

        为什么不用 Playwright 的 click():
        1. 公众号后台存在多个**隐藏的重复浮层**(首图/次图各一份),`locator(sel).first`
           会命中隐藏实例,然后一直等到超时 —— 现象是"点了没反应";
        2. 这些弹层按钮常被透明遮罩盖住,Playwright 的可点性检查过不去(即使元素可见)。
        用 JS 直接 click() 对这类第三方页面最稳。
        """
        js = """
        (sel) => {
          const els = [...document.querySelectorAll(sel)];
          const vis = els.filter((e) => e.offsetParent !== null
            && e.getBoundingClientRect().width > 0);
          if (!vis.length) return 'none';
          vis[0].click();
          return 'ok';
        }
        """
        try:
            return self._page.evaluate(js, selector) == "ok"
        except Exception:  # noqa: BLE001
            return False

    def _cover_applied(self) -> bool:
        """封面是否**真的**设置上了:以"封面预览是否可见"为准。

        设置前 `.js_cover_preview_new` 就在 DOM 里,但不可见 —— 所以必须查可见性,
        不能只看元素存在。
        """
        js = """
        () => {
          const el = document.querySelector(
            '.js_cover_preview_new, .js_cover_preview_square');
          if (!el) return false;
          const r = el.getBoundingClientRect();
          return r.width > 0 && r.height > 0 && el.offsetParent !== null;
        }
        """
        try:
            return bool(self._page.evaluate(js))
        except Exception:  # noqa: BLE001
            return False

    def _set_cover(self, cover_image: str, warnings: list[str]) -> bool:
        """把本地图片设为草稿封面(实测流程,2026-09-10 旧版编辑器 appmsg_edit_v2)。

        点封面区 → 「从图片库选择」→ 素材库弹窗内上传 → 下一步 → 「编辑封面」确认 → 预览出现。

        ⚠️ 返回 True **仅当封面预览真的出现**。曾经的写法是"没抛异常就算成功",
        结果把图传进了**正文工具栏**的 file input(裸 `input[type=file]` 的第一个匹配),
        于是出现"提示上传成功、草稿却没有封面"的假成功。
        """
        p = Path(cover_image)
        if not p.exists():
            warnings.append(f"封面图不存在,已跳过:{cover_image}")
            return False
        page = self._page
        try:
            if not self._js_click_visible(self._sel("cover", "cover_area")["value"]):
                warnings.append("未找到封面设置入口,已跳过封面(请在后台手动添加)")
                return False
            page.wait_for_timeout(1200)

            if not self._js_click_visible(self._sel("cover", "from_gallery")["value"]):
                warnings.append("封面浮层未出现或不含「从图片库选择」,封面未设置")
                return False

            page.wait_for_selector(self._sel("cover", "picker_dialog")["value"], timeout=15000)
            # 必须用弹窗内那个 input:否则会传进正文图片通道
            page.set_input_files(
                self._sel("cover", "file_input")["value"], str(p.resolve()), timeout=20000)
            page.wait_for_timeout(5000)      # 等上传完成并自动选中该图

            if not self._js_click_visible(self._sel("cover", "next_btn")["value"]):
                warnings.append("素材库「下一步」不可点(上传可能还没完成),封面未设置")
                return False
            page.wait_for_timeout(3000)

            # 「编辑封面」裁剪页:直接用默认裁剪,点确认
            if not self._js_click_visible(self._sel("cover", "confirm_btn")["value"]):
                warnings.append("封面裁剪页「确认」未点到,请在后台核对封面")
            page.wait_for_timeout(2500)

            if self._cover_applied():
                self._screenshot("cover_set")
                return True
            warnings.append("封面疑似未生效(未见封面预览),请在后台手动确认")
            self._screenshot("cover_unset")
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("封面设置失败(不阻断): %s", e)
            warnings.append(f"封面自动设置失败({type(e).__name__}),请在后台手动添加")
            try:
                page.keyboard.press("Escape")  # 关掉可能还开着的面板
            except Exception:  # noqa: BLE001
                pass
            return False

    def _grab_draft_url(self) -> str:
        """尽力抓取草稿链接;抓不到返回空串(不阻断)。"""
        try:
            self._page.wait_for_timeout(1500)
            url = self._page.url
            return url if "mp.weixin.qq.com" in url else ""
        except Exception:
            return ""

    def check_page_ready(self) -> list[str]:
        """页面改版自检:返回缺失的关键选择器。"""
        return self.sel.check_required([
            ("login", "after_login"),
            ("editor", "article_item"),
            ("editor", "prosemirror"),
            ("editor", "author_input"),
            ("editor", "save_btn"),
        ])

    def _screenshot(self, name: str, fixed: bool = False) -> None:
        """截图留证。fixed=True 时用固定文件名(如 live_login.png),便于随时查看现场。"""
        try:
            shot_dir = settings.data_dir / "logs" / "screenshots"
            shot_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{name}.png" if fixed else f"{name}_{int(time.time())}.png"
            path = shot_dir / filename
            self._page.screenshot(path=str(path))
            if not fixed:
                logger.info("截图留证: %s", path)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self._context.close()
            self._browser_inst.close()
            self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


def plan_b_export(article: dict) -> str:
    """半自动方案 B:把文章排版成可人工粘贴的 Markdown 文本。

    必须去掉 `:::kind` 容器标记 —— 人工粘贴时看到 `:::thesis` 只会让人困惑。
    论点保留加粗(它是全文最该被看到的一句)。
    """
    md = strip_containers(article_to_markdown(article))
    return f"# {article.get('title', '')}\n\n{md}"


def plan_b_html(article: dict) -> str:
    """方案 B 富文本版:返回带内联样式的 HTML,可在浏览器里全选复制后粘进公众号。"""
    return markdown_to_html(article_to_markdown(article))
