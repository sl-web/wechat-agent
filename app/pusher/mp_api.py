"""公众号后台**只读**接口客户端(复用已有登录会话的 cookie)。

为什么不用 Playwright 抓这里的页面:
实测这些数据本来就是 **XHR 返回的 JSON**(`appmsgpublish` 直接给 `publish_page` 结构),
而会话 cookie 就存在 `data/storage_state.json` 里 —— 直接用 HTTP 调即可,
比开浏览器快两个数量级,也**不必**套 `pusher/runner.py` 那套"Playwright 必须独立进程"的机制。

⚠️ 本模块**只读**:只做 GET,不发表、不群发、不删除。任何写操作都不在这里。
⚠️ 会话会过期(和推送共用同一份 storage_state),失效时要能给出可执行的提示。
"""
from __future__ import annotations

import html as _html
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.core.logger import get_logger
from app.pusher.service import state_path

logger = get_logger("mp_api")

# 指标字段映射(2026-09-11 实测:用后台 DOM 的 tooltip 文案与接口数值**逐行对照**得出)。
#
#   DOM class        后台 tooltip    接口字段
#   appmsg-view      阅读人数    ←   read_num
#   appmsg-like      点赞人数    ←   old_like_num   ⚠️ **不是** like_num
#   appmsg-share     分享人数    ←   share_num
#   appmsg-haokan    推荐人数    ←   like_num       ⚠️ 原「在看」现已改名「推荐」
#   appmsg-comment   留言条数    ←   comment_num
#   appmsg-underline 划线人数    ←   本接口**没有**,需另抓 mpunderline
#
# ⚠️ 两个 `like` 字段的反直觉映射是**数据定下来的**:拿「点赞人数=3 / 推荐人数=0」的那篇
# (学会舍得)比对,接口里 old_like_num=3、like_num=0 —— 所以 like↔old_like_num。
# 单看字段名会写反,别"顺手改回去"。
STAT_MAP = {
    "read_count": "read_num",
    "like_count": "old_like_num",
    "share_count": "share_num",
    "recommend_count": "like_num",
    "comment_count": "comment_num",
}

# 后台 HTML 页里内联的变量名
_PAGE_KEY = "publish_page = "

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HOME = "https://mp.weixin.qq.com/"
BASE = "https://mp.weixin.qq.com/cgi-bin/"

# token 变化不频繁(同一次登录会话内稳定),缓存一会儿避免每次抓取都多一次请求
_TOKEN_TTL = 600
_token_cache: dict = {"value": "", "at": 0.0}


class MpSessionError(RuntimeError):
    """会话失效或未登录 —— 调用方应提示用户重新扫码。"""


def _cookie_header() -> str:
    p = Path(state_path())
    if not p.exists():
        raise MpSessionError("本地没有登录会话,请先在「文章处理窗口 → 扫码登录」")
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise MpSessionError(f"会话文件损坏:{e}") from e
    pairs = [f"{c['name']}={c['value']}" for c in st.get("cookies", [])
             if "mp.weixin.qq.com" in (c.get("domain") or "")]
    if not pairs:
        raise MpSessionError("会话里没有 mp.weixin.qq.com 的 cookie,请重新扫码登录")
    return "; ".join(pairs)


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Cookie": _cookie_header(),
        "Referer": "https://mp.weixin.qq.com/",
        "Accept": "application/json, text/plain, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raise MpSessionError(f"请求失败 HTTP {e.code},会话可能已过期") from e
    except Exception as e:  # noqa: BLE001
        raise MpSessionError(f"请求失败:{e}") from e


def token(force: bool = False) -> str:
    """从后台首页解析 token(随会话变化)。"""
    now = time.time()
    if not force and _token_cache["value"] and now - _token_cache["at"] < _TOKEN_TTL:
        return _token_cache["value"]
    html = _get(HOME)
    m = re.search(r"token=(\d+)", html) or re.search(r'"token":"(\d+)"', html)
    if not m:
        raise MpSessionError("没取到 token(多半是会话已过期),请重新扫码登录")
    _token_cache.update(value=m.group(1), at=now)
    return m.group(1)


def _json(path: str, params: str, timeout: int = 20) -> dict:
    url = f"{BASE}{path}?{params}&token={token()}&lang=zh_CN&f=json&ajax=1"
    txt = _get(url, timeout=timeout)
    if not txt.lstrip().startswith("{"):
        raise MpSessionError("接口返回的不是 JSON(通常是会话过期被重定向到登录页)")
    try:
        data = json.loads(txt)
    except ValueError as e:
        raise MpSessionError(f"接口返回无法解析:{e}") from e
    ret = (data.get("base_resp") or {}).get("ret")
    if ret not in (0, None):
        raise MpSessionError(f"接口报错 ret={ret}: {(data.get('base_resp') or {}).get('err_msg')}")
    return data


def _extract_publish_page(doc: str) -> dict:
    """从后台 HTML 里把内联的 `publish_page = {...}` 抠出来。

    用 `raw_decode` 而不是正则 —— 里面的 JSON 有嵌套对象和字符串里的大括号,
    正则的非贪婪匹配会在第一个 `};` 处提前截断。
    """
    i = doc.find(_PAGE_KEY)
    if i < 0:
        raise MpSessionError("页面里没有 publish_page(可能后台改版,或会话已过期)")
    try:
        obj, _ = json.JSONDecoder().raw_decode(doc[i + len(_PAGE_KEY):])
    except ValueError as e:
        raise MpSessionError(f"publish_page 解析失败:{e}") from e
    return obj


def published_page(begin: int = 0, count: int = 20, timeout: int = 25) -> dict:
    """抓「发表记录」的一页(**带指标**)。

    ⚠️ 同一个 `appmsgpublish` 接口有**两个变体**,别搞混:
      · `sub_action=list_ex&f=json` → JSON,**只有标题/链接,没有指标**;
      · 不带 `f=json`(普通页面请求) → HTML,内联 `publish_page` 变量,
        **带全部指标**(read_num / like_num / share_num / old_like_num / comment_num)。
    我们要指标,所以走后者 —— 依然是**只读 GET**。
    """
    url = (f"{BASE}appmsgpublish?sub=list&begin={begin}&count={count}"
           f"&token={token()}&lang=zh_CN")
    return _extract_publish_page(_get(url, timeout=timeout))


def list_published(count: int = 20, max_pages: int = 20) -> dict:
    """抓「发表记录」全量(自动翻页,**含每篇指标**)。

    返回 {items: [...], total_count, publish_count(未通知), masssend_count(已通知), pages}

    每项包含:
      draft_msgid  起草时的 appmsgid —— **与我们 articles.draft_url 里的 appmsgid 同源,可精确匹配**
      msgid/link/title/digest  发表后的信息
      create_time  发表时间(秒级时间戳)
      publish_type 1 = 只发表(未通知) / 101 = 群发过(已通知)  ← 见 verify_type_mapping()
      is_deleted
      stats        反馈指标 {read_count, like_count, share_count, recommend_count, comment_count}
                   (字段含义见 STAT_MAP;取不到时为空 dict,调用方应**跳过而不是写 0**)
      articles     一条发表记录可能含多篇(合集群发),这里是逐篇明细
    """
    items: list[dict] = []
    meta = {"total_count": 0, "publish_count": 0, "masssend_count": 0}
    begin, pages = 0, 0
    while pages < max_pages:
        page = published_page(begin, count)
        if pages == 0:
            meta = {k: page.get(k, 0) for k in meta}
        batch = page.get("publish_list") or []
        for raw in batch:
            parsed = _parse_item(raw)
            if parsed:
                items.append(parsed)
        pages += 1
        begin += count
        if len(batch) < count or begin >= int(page.get("total_count") or 0):
            break
        time.sleep(0.4)          # 轻微间隔,别把后台当压测目标

    logger.info("发表记录:抓到 %d 条(后台口径 全部 %s / 未通知 %s / 已通知 %s)",
                len(items), meta["total_count"], meta["publish_count"], meta["masssend_count"])
    return {**meta, "items": items, "pages": pages}


def _stats_of(art: dict) -> dict:
    """按 STAT_MAP 取指标。**取不到就不放这个键**(调用方据此判断"没有数据"而非"数据为 0")。"""
    out = {}
    for col, key in STAT_MAP.items():
        v = art.get(key)
        if isinstance(v, (int, float)):
            out[col] = int(v)
    return out


def _parse_item(raw: dict) -> dict | None:
    """把一坨嵌套 JSON 拍平成我们要的字段。

    兼容两种形状:
      · HTML 变体 → `appmsg_info`(含指标,链接字段叫 `content_url`)
      · JSON 变体 → `appmsgex`(无指标,链接字段叫 `link`)
    """
    try:
        info = raw.get("publish_info")
        if isinstance(info, str):
            info = json.loads(_html.unescape(info))
        info = info or {}
        arts = info.get("appmsg_info") or info.get("appmsgex") or []
        if not arts:
            return None
        first = arts[0]
        pub = info.get("publish_info") or {}
        return {
            "draft_msgid": str(pub.get("draft_msgid") or raw.get("draft_msgid") or ""),
            "msgid": str(pub.get("msgid") or first.get("appmsgid") or ""),
            "title": str(first.get("title") or "").strip(),
            "link": str(first.get("content_url") or first.get("link") or "").strip(),
            "digest": str(first.get("digest") or "").strip(),
            "create_time": (pub.get("create_time") or first.get("create_time")
                            or pub.get("update_time")),
            "publish_type": raw.get("publish_type"),
            "is_deleted": bool(first.get("is_deleted")),
            "stats": _stats_of(first),
            "articles": [{
                "appmsgid": str(a.get("appmsgid") or ""),
                "itemidx": a.get("itemidx"),
                "title": str(a.get("title") or "").strip(),
                "link": str(a.get("content_url") or a.get("link") or "").strip(),
                "is_deleted": bool(a.get("is_deleted")),
                "stats": _stats_of(a),
            } for a in arts],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("解析发表记录条目失败(跳过): %s", e)
        return None


def verify_type_mapping(result: dict) -> dict:
    """核对 `publish_type` 与后台口径(未通知/已通知)是否吻合。

    ⚠️ 这一步是**自查**:`publish_type` 的含义没有官方文档,是从计数倒推出来的。
    不吻合就说明推错了,调用方应把"是否群发"标记为未知,而不是写错数据。
    """
    by_type: dict = {}
    for it in result.get("items") or []:
        by_type[str(it.get("publish_type"))] = by_type.get(str(it.get("publish_type")), 0) + 1
    unmass = by_type.get("1", 0)          # 推测:只发表(未通知)
    massed = by_type.get("101", 0)        # 推测:群发过(已通知)
    ok = (unmass == int(result.get("publish_count") or -1)
          and massed == int(result.get("masssend_count") or -1))
    return {"ok": ok, "by_type": by_type,
            "expect": {"publish_count": result.get("publish_count"),
                       "masssend_count": result.get("masssend_count")}}
