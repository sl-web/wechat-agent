"""效果数据回流服务(PRD 5.9 + V3.0):发布后抓取文章数据写 article_stats。

真实数据源:公众号后台「发表记录」(`app.pusher.mp_api`,**只读**)。
该页的 HTML 变体内联了每篇的反馈指标 —— 见 `mp_api.STAT_MAP`。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Protocol

from app.core.db import connect, execute
from app.pusher.mp_api import MpSessionError

# article_stats 里"指标列"的**唯一清单** —— 读写都从它派生,避免两套口径。
STAT_COLUMNS = ("read_count", "like_count", "share_count",
                "recommend_count", "comment_count", "underline_count")


class StatsFetcher(Protocol):
    """回流数据抓取器接口。"""

    def fetch(self, article: dict, stat_date: str) -> dict:
        """返回指标 dict(键见 STAT_COLUMNS)。

        ⚠️ **拿不到数据时返回空 dict**,不要填 0 —— 0 是"确实没人读",
        空是"我们不知道",两者写进库里就再也分不开了。
        """
        ...


class StubStatsFetcher:
    """占位实现:无后台会话时用于测试/演示。返回全 0(视为"有数据,但都是 0")。"""

    def fetch(self, article: dict, stat_date: str) -> dict:
        return {c: 0 for c in STAT_COLUMNS}


class MpStatsFetcher:
    """从公众号「发表记录」抓真实反馈数据(**只读**)。

    整页只抓一次并在实例内缓存 —— 一次回流要处理多篇,不能每篇都去请求后台
    (那是把人家的后台当压测目标,也慢)。

    匹配规则与反查一致:`draft_msgid` 精确优先,标题**唯一**时兜底。
    匹配不到 → 返回空 dict,调用方跳过。
    """

    def __init__(self, loader=None):
        self._loader = loader or _load_published
        self._by_draft: dict | None = None
        self._by_title: dict = {}
        self._title_count: dict = {}

    def _ensure(self) -> None:
        if self._by_draft is not None:
            return
        items = (self._loader() or {}).get("items") or []
        by_draft: dict = {}
        by_title: dict = {}
        title_count: dict = {}
        for it in items:
            dm = str(it.get("draft_msgid") or "")
            if dm:
                by_draft.setdefault(dm, it)
            t = (it.get("title") or "").strip()
            if t:
                title_count[t] = title_count.get(t, 0) + 1
                by_title.setdefault(t, it)
        self._by_draft, self._by_title, self._title_count = by_draft, by_title, title_count

    def fetch(self, article: dict, stat_date: str) -> dict:
        self._ensure()
        msgid = _appmsgid_of(article.get("draft_url") or "")
        it = self._by_draft.get(msgid) if msgid else None
        if not it:
            t = (article.get("title") or "").strip()
            if t and self._title_count.get(t, 0) == 1:
                it = self._by_title.get(t)
        if not it:
            return {}
        return dict(it.get("stats") or {})


def _load_published() -> dict:
    from app.pusher.mp_api import list_published

    return list_published()


def _default_fetcher() -> StatsFetcher:
    """默认用**真实**抓取器。

    ⚠️ 没登录会话时它会抛 `MpSessionError`(而不是静默写 0) ——
    这是刻意的:回流看板要展示的是真实反馈,凭空写 0 等于伪造数据。
    """
    return MpStatsFetcher()


def _write_stats(conn: sqlite3.Connection, article_id: int, stats: dict, stat_date: str) -> None:
    """写一行指标。**同一天重复回流要覆盖**(UPSERT),否则"手动刷新"会失效。"""
    cols = ", ".join(STAT_COLUMNS)
    marks = ", ".join("?" for _ in STAT_COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in STAT_COLUMNS)
    execute(
        conn,
        f"""INSERT INTO article_stats
              (article_id, {cols}, stat_date, fetched_at)
            VALUES(?, {marks}, ?, datetime('now','localtime'))
            ON CONFLICT(article_id, stat_date) DO UPDATE SET
              {updates}, fetched_at=excluded.fetched_at""",
        (article_id, *(stats.get(c) for c in STAT_COLUMNS), stat_date),
    )


def _due_articles(today: str, conn: sqlite3.Connection) -> list[dict]:
    """找出"已发布且今天应回流"的文章:
    发布后在 backfill_days 配置(默认 1/3/7)天各有一次回流,同日已回流则跳过。
    """
    cfg_days = [1, 3, 7]
    try:
        from app.core.config import get_config

        cfg_days = get_config("backfill_days_after_publish", conn) or cfg_days
    except Exception:
        pass

    rows = _published_articles(conn)

    due = []
    for r in rows:
        pub = datetime.fromisoformat(r["published_at"].replace("Z", "+00:00")).replace(tzinfo=None)
        for day in cfg_days:
            target = (pub + timedelta(days=day)).strftime("%Y-%m-%d")
            if target != today:
                continue
            done = conn.execute(
                "SELECT 1 FROM article_stats WHERE article_id=? AND stat_date=?",
                (r["id"], target),
            ).fetchone()
            if not done:
                due.append(dict(r))
                break
    return due


def _published_articles(conn: sqlite3.Connection) -> list[dict]:
    """**已确认发布**的文章 —— 写入(排期回流)、反查、看板**共用同一个口径**。

    判据只有 **状态 + 发布时间**,⚠️ **不看 `draft_url`**。

    为什么特意强调:`draft_url` 只是与后台**精确匹配**用的手段(draft_msgid 的来源),
    不是"这是不是我们的文章"的身份判据 —— `articles` 表里本来就只有本系统产出的稿子。
    曾经把它当过滤条件,结果三处口径打架:
      · 人工「确认已发布」会把草稿链接清成 NULL(已修);
      · 链接一丢,反查与看板就都看不见这篇了;
      · 而 T+1/3/7 排期回流**不看** draft_url,数据照样写了进来。
    表现就是"数据回流列表和实际发布对不上":库里有 7 行数据,看板只列出 5 篇。
    """
    return [dict(r) for r in conn.execute(
        """SELECT id, title, status, published_at, draft_url, published_url,
                  mp_msgid, mp_mass_sent
           FROM articles
           WHERE status IN ('published','backfilled') AND published_at IS NOT NULL
           ORDER BY id""").fetchall()]


def _tracked_articles(conn: sqlite3.Connection) -> list[dict]:
    """全部已确认发布的文章(不看排期)。与 `_due_articles` 同源,避免两套口径。"""
    return _published_articles(conn)


def _backfill_rows(rows: list[dict], fetcher: StatsFetcher,
                   conn: sqlite3.Connection, today: str) -> dict:
    """对给定文章跑一次抓取+落库。backfill_once / backfill_all 共用,避免两套口径。"""
    result: dict = {"checked": len(rows), "backfilled": 0, "skipped": [], "failed": []}
    for art in rows:
        try:
            stats = fetcher.fetch(art, today)
        except MpSessionError as e:
            # 会话问题是"整体性"的,再试别的文章也是白试 —— 直接中止并给可执行提示。
            result["error"] = f"公众号会话不可用,回流已中止:{e}"
            result["hint"] = "到「文章处理窗口」重新扫码登录后重试"
            break
        except Exception as e:  # noqa: BLE001
            result["failed"].append({"article_id": art["id"], "error": str(e)})
            continue
        if not stats:
            result["skipped"].append({
                "article_id": art["id"], "title": (art.get("title") or "")[:40],
                "reason": "后台发表记录里没找到这篇(可能未发表、已删除或标题被改)",
            })
            continue
        _write_stats(conn, art["id"], stats, today)
        conn.execute(
            "UPDATE articles SET status='backfilled' WHERE id=? AND status='published'",
            (art["id"],),
        )
        conn.commit()
        result["backfilled"] += 1
    return result


def backfill_once(fetcher: StatsFetcher | None = None, db_path: str | None = None) -> dict:
    """按排期回流:**今天到期的**文章(T+1/3/7)→ 抓取 → 写 article_stats。

    返回 {checked, backfilled, skipped, failed, error?, hint?}
    - skipped:后台发表记录里没找到这篇(可能未发表/已删除/改了标题)—— **不写 0 冒充数据**
    - error  :会话失效等致命问题,已中止本轮
    """
    fetcher = fetcher or _default_fetcher()
    own = db_path is not None
    conn = connect(db_path) if db_path else connect()
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        return _backfill_rows(_due_articles(today, conn), fetcher, conn, today)
    finally:
        if own:
            conn.close()


def backfill_all(fetcher: StatsFetcher | None = None, db_path: str | None = None) -> dict:
    """不看排期,**强制回流全部已发布文章** —— 供看板的「立即刷新」。"""
    fetcher = fetcher or _default_fetcher()
    own = db_path is not None
    conn = connect(db_path) if db_path else connect()
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        return _backfill_rows(_tracked_articles(conn), fetcher, conn, today)
    finally:
        if own:
            conn.close()


def backfill_article(article_id: int, fetcher: StatsFetcher | None = None,
                     db_path: str | None = None) -> dict:
    """单篇文章立即回流(文章级手动触发):抓取一次当前数据写 article_stats。

    同一天重复调用会**覆盖**当天的数据(而不是被 INSERT OR IGNORE 忽略) ——
    否则界面上点"刷新"看不到任何变化。
    """
    fetcher = fetcher or _default_fetcher()
    own = db_path is not None
    conn = connect(db_path) if db_path else connect()
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        art = conn.execute(
            "SELECT id, title, published_at, draft_url FROM articles WHERE id=?", (article_id,)
        ).fetchone()
        if not art:
            raise ValueError(f"文章不存在: {article_id}")
        stats = fetcher.fetch(dict(art), today)
        if not stats:
            return {"ok": False, "article_id": article_id, "stat_date": today, "stats": {},
                    "error": "后台发表记录里没找到这篇(可能未发表、已删除或标题被改)",
                    "hint": "先点「同步发布状态」确认这篇已发表"}
        _write_stats(conn, article_id, stats, today)
        conn.execute(
            "UPDATE articles SET status='backfilled' WHERE id=? AND status IN ('published','backfilled')",
            (article_id,),
        )
        conn.commit()
        return {"ok": True, "article_id": article_id, "stat_date": today,
                "stats": stats, "note": "已回流一次"}
    finally:
        if own:
            conn.close()


def refresh_all(db_path: str | None = None, only_due: bool = False,
                loader=None) -> dict:
    """一键刷新 = **先反查发布状态,再抓取反馈数据**。

    ⚠️ 顺序不能反,两步职责不同:
      · 反查(`sync_published`)负责写 `published_url` —— 看板「后台核对」列的依据;
      · 抓取(`backfill_all`)只写指标。
    只抓取不反查,就会出现**自相矛盾的一行**:有反馈数据、却标着「后台未找到」。
    实测踩过:#45/#46 指标抓得到(说明后台明明有这篇),但 `published_url` 为空
    → 看板判为"未找到"。原因是那两个动作**在两个按钮上**,用户只点了其中一个。

    后台**只请求一次**:把同一份发表记录快照喂给反查与抓取(`fetch=` / `loader=`)——
    两个入口本来就支持注入,不需要改匹配逻辑(避免再出现"两套口径")。
    会话失效时抛 `MpSessionError`,由调用方转成可执行提示。

    `loader` 仅供测试注入;生产走 `list_published()`。
    """
    from app.pusher.mp_api import list_published

    load = loader or list_published
    cache: dict = {}

    def page() -> dict:
        if "page" not in cache:
            cache["page"] = load()
        return cache["page"]

    sync = sync_published(fetch=page, db_path=db_path)
    run = backfill_once if only_due else backfill_all
    stats = run(fetcher=MpStatsFetcher(loader=page), db_path=db_path)
    return {"sync": sync, "stats": stats}


# ---------------- 反查:哪些文章真的发出去了 ----------------

def _appmsgid_of(draft_url: str) -> str:
    """从草稿链接里取 appmsgid(与发表记录的 draft_msgid 同源)。"""
    import re

    m = re.search(r"appmsgid=(\d+)", draft_url or "")
    return m.group(1) if m else ""


def sync_published(fetch=None, db_path: str | None = None) -> dict:
    """从公众号后台「发表记录」反查发布状态,推进本地文章到 `published`。

    为什么需要它:文章发出去之后**系统本来不知道** —— 一直是 `pushed`,
    于是回流(只取 published)永远筛不出待办。实测库里只有 1 篇 published,
    后台却已有几十篇发表记录。

    **范围 = 已推送过的稿件**(pushed/published/backfilled)—— 也就是"可能是我们发的"那一批。
    ⚠️ **不要求 `draft_url` 非空**:它只是精确匹配的手段,不是身份判据;
    曾经要求它,导致人工确认发布时丢了草稿链接的稿子**永远查不出来**。
    从未推送过的稿子(generated/approved)不参与 —— 它们不可能已经发表。

    匹配优先级:
    1. `draft_msgid` == 草稿 appmsgid —— 精确,首选;
    2. 标题完全相同 —— 兜底(实测"从草稿直接群发"的发表记录**不带 draft_msgid**,
       只能靠标题);仅在发表记录里该标题唯一时采信,避免误配。

    只**推进**不降级:本地已是 published 但发表记录里找不到的,原样保留并列入
    `missing_in_mp` 供人工核对(可能是被删除/被改标题)。
    """
    from app.pusher.mp_api import list_published

    do_fetch = fetch or (lambda: list_published())
    mp = do_fetch()
    items = mp.get("items") or []

    by_draft: dict[str, dict] = {}
    title_count: dict[str, int] = {}
    for it in items:
        dm = str(it.get("draft_msgid") or "")
        if dm:
            by_draft.setdefault(dm, it)
        t = (it.get("title") or "").strip()
        if t:
            title_count[t] = title_count.get(t, 0) + 1
    by_title = {str(it.get("title") or "").strip(): it for it in items}

    own = db_path is not None
    conn = connect(db_path) if db_path else connect()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report: dict = {"ok": True, "scanned": len(items), "matched": 0,
                    "by_draft": 0, "by_title": 0, "updated": [],
                    "missing_in_mp": [], "mp": {k: mp.get(k) for k in
                                                ("total_count", "publish_count", "masssend_count")}}
    try:
        # ⚠️ 范围 = **已推送过**的稿件(pushed/published/backfilled),
        # **不要求 `draft_url` 非空**。理由:人工「确认已发布」曾经会把草稿链接清成 NULL,
        # 只看 draft_url 就会把这些稿子永远排除在反查之外(#45 就是这样漏掉的)。
        # 反向的限制也在:从未推送过的稿子(generated/approved)不参与 —— 它们不可能已经发表。
        rows = [dict(r) for r in conn.execute(
            """SELECT id, title, status, draft_url, published_at FROM articles
               WHERE status IN ('pushed','published','backfilled')
               ORDER BY id"""
        ).fetchall()]

        for a in rows:
            msgid = _appmsgid_of(a.get("draft_url") or "")
            it, how = None, ""
            if msgid and msgid in by_draft:
                it, how = by_draft[msgid], "draft_msgid"
            else:
                t = (a.get("title") or "").strip()
                if t and title_count.get(t, 0) == 1:
                    it, how = by_title.get(t), "title"
            if not it:
                if a.get("status") in ("published", "backfilled"):
                    report["missing_in_mp"].append({"id": a["id"], "title": a["title"][:40]})
                continue

            report["matched"] += 1
            report["by_draft" if how == "draft_msgid" else "by_title"] += 1
            pub_at = _ts_to_str(it.get("create_time")) or a.get("published_at") or now
            mass = 1 if str(it.get("publish_type")) == "101" else 0
            conn.execute(
                """UPDATE articles
                   SET status = CASE WHEN status IN ('published','backfilled')
                                     THEN status ELSE 'published' END,
                       published_at = COALESCE(published_at, ?),
                       published_url = ?, mp_msgid = ?, mp_mass_sent = ?
                   WHERE id = ?""",
                (pub_at, it.get("link") or "", str(it.get("msgid") or ""), mass, a["id"]),
            )
            report["updated"].append({
                "id": a["id"], "title": (a.get("title") or "")[:36],
                "matched_by": how, "published_at": pub_at,
                "mass_sent": bool(mass), "url": it.get("link") or "",
            })
            _log_publish(conn, a["id"], how, mass)
        conn.commit()
        return report
    finally:
        if own:
            conn.close()


def _ts_to_str(ts) -> str:
    """后台给的是秒级时间戳 → 本地时间字符串。"""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return ""


def _log_publish(conn, article_id: int, how: str, mass: int) -> None:
    """落一条 run_logs,便于回溯"这篇是怎么被认成已发布的"。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    note = f"article={article_id} reviewer=mp_sync note=反查发表记录(匹配方式:{how}," \
           f"{'已群发' if mass else '仅发表未群发'})"
    conn.execute(
        """INSERT INTO run_logs(run_id, stage, status, detail, started_at, finished_at)
           VALUES(?, 'review', 'publish_sync', ?, ?, ?)""",
        (f"review_article_{article_id}", note, now, now),
    )


# ---------------- 回流数据看板 ----------------

def _is_stub_row(row: dict) -> bool:
    """这一行是不是旧 `StubStatsFetcher` 写的**假数据**。

    旧抓取器固定返回全 0,而且那时还没有 `recommend_count`/`comment_count` 两列。
    真实抓取**一定会**写 `recommend_count`(后台 `like_num` 必然存在),
    所以"新列全空 + 老列有值"是**可靠**的假数据特征。

    为什么要区分:这些 0 会让人以为"确实没人读",而真相是"当时根本没抓"。
    和「取不到数据绝不写 0」是同一条原则 —— 0 是事实,空才是"不知道"。
    """
    return row.get("recommend_count") is None and row.get("comment_count") is None


def stats_dashboard(range_key: str | None = "all", db_path: str | None = None,
                    limit: int = 500) -> dict:
    """回流数据看板:每篇**已确认发布**的文章的最新反馈指标 + 历史趋势 + 汇总。

    ⚠️ 口径与写入侧、反查侧**完全同源**(都用 `_published_articles`):
    **状态 + 发布时间**,不看 `draft_url`。三处曾经各写一套,导致"库里有数据、看板不显示"。

    每行有 `in_mp`:是否在后台发表记录里找到了(即有没有公开链接)。
    `in_mp=False` 的行**指标一律置空** —— 后台都没有这篇,旧数据不可信,
    显示出来会被当成真实读数(实际踩过:库里躺着一行旧 stub 写的假 0)。
    时间维度按 `published_at` 过滤(默认 `all`:发文不频繁,默认看全量更实用)。
    """
    from app.core.daterange import normalize_range, range_bounds, range_label

    key = normalize_range(range_key)
    lo, hi = range_bounds(key)
    own = db_path is None
    conn = connect(db_path) if db_path else connect()
    try:
        # ⚠️ 与写入侧(**同一个 `_published_articles` 口径**)完全一致:
        # 只按"状态 + 发布时间"取,不再要求 `draft_url` 非空。
        # 之前看板多要一个 draft_url,而排期回流没这个要求 → 库里有数据、看板却不显示。
        arts = _published_articles(conn)
        if lo and hi:
            arts = [a for a in arts if lo <= str(a["published_at"] or "")[:10] <= hi]
        arts = sorted(arts, key=lambda a: (a["published_at"] or "", a["id"]), reverse=True)[:limit]

        ids = [a["id"] for a in arts]
        stats_by: dict = {}
        last_fetched = None
        if ids:
            q = ",".join("?" for _ in ids)
            for r in conn.execute(
                    f"SELECT * FROM article_stats WHERE article_id IN ({q}) ORDER BY stat_date", ids):
                stats_by.setdefault(r["article_id"], []).append(dict(r))
            rr = conn.execute(
                f"SELECT MAX(fetched_at) AS m FROM article_stats WHERE article_id IN ({q})",
                ids).fetchone()
            last_fetched = rr["m"] if rr else None

        totals = {c: 0 for c in STAT_COLUMNS}
        rows, with_stats, missing, pending = [], 0, 0, 0
        for a in arts:
            hist = stats_by.get(a["id"]) or []
            # 只保留"真实抓取"写下的行:假数据(旧 stub 的 0)一律不显示,见 _is_stub_row
            real_hist = [h for h in hist if not _is_stub_row(h)]
            latest = real_hist[-1] if real_hist else None
            # "后台未找到" = 反查没能把它和发表记录对上(没有公开链接)。
            # 这是**提示性**标记:可能未发表 / 已被删除 / 标题被改,建议人工核对。
            in_mp = bool(str(a.get("published_url") or "").strip())
            if not in_mp:
                missing += 1
                if latest:
                    # 有指标说明抓取时**明明在后台找到了它**(只是还没回填链接)。
                    # 单独计数,别和"真的找不到"混在一起 —— 两者含义完全不同。
                    pending += 1
            if latest:
                with_stats += 1
                for c in STAT_COLUMNS:
                    totals[c] += int(latest.get(c) or 0)
            rows.append({
                "id": a["id"], "title": a["title"], "status": a["status"],
                "published_at": a["published_at"], "published_url": a["published_url"],
                "mass_sent": a["mp_mass_sent"],
                "in_mp": in_mp,
                "stat_date": latest["stat_date"] if latest else None,
                "latest": ({c: latest.get(c) for c in STAT_COLUMNS} if latest else None),
                "history": [{"stat_date": h["stat_date"],
                             **{c: h.get(c) for c in STAT_COLUMNS}} for h in real_hist],
            })
        return {
            "range": key, "range_label": range_label(key),
            "summary": {
                "published_total": len(arts),
                "with_stats": with_stats,
                "not_in_mp": missing,
                # 有指标但没回填链接 = 抓取时后台明明有它,只是还没反查回链接。
                # 前端据此显示「待核对」而不是「未找到」(两者混在一起会误导)。
                "pending_check": pending,
                "totals": totals,
                "last_fetched_at": last_fetched,
            },
            "rows": rows,
        }
    finally:
        if own:
            conn.close()
