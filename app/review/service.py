"""人工审核服务(PRD 5.5):状态流转 + 在线编辑 + 审核记录。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app.core.db import connect, execute, query

VALID_TRANSITIONS = {
    # current: allowed actions
    "pushed": ["approve", "reject", "edit_note"],
    "pending_review": ["approve", "reject", "edit"],
    "edited": ["approve", "reject", "edit"],
}


def _set_status(conn: sqlite3.Connection, article_id: int, status: str) -> None:
    conn.execute(
        "UPDATE articles SET status=? WHERE id=?",
        (status, article_id),
    )
    conn.commit()


def approve(article_id: int, reviewer: str = "human", db_path: str | None = None) -> dict:
    """通过:状态置 approved(待推送草稿/待人工发布)。"""
    conn = connect(db_path) if db_path else connect()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """UPDATE articles SET status='approved',
               reject_reason=NULL, published_at=NULL WHERE id=?""",
            (article_id,),
        )
        _log_review(conn, article_id, "approve", reviewer, "")
        conn.commit()
        return {"ok": True, "article_id": article_id, "status": "approved", "time": now}
    finally:
        conn.close()


def push_to_drafts(article_id: int, draft_url: str = "", reviewer: str = "human",
                   db_path: str | None = None) -> dict:
    """推送草稿(文章级):由人工/半自动把稿件推送到公众号草稿箱。

    - draft_url 为空时标记 pending_manual_push(等待人工扫码/半自动处理)
    - 状态: approved/generated/pushed → pushed(草稿已就绪,待最终发布)
    """
    conn = connect(db_path) if db_path else connect()
    try:
        row = conn.execute(
            "SELECT status, title FROM articles WHERE id=?", (article_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"文章不存在: {article_id}")
        if row["status"] in ("published", "backfilled"):
            raise ValueError("已发布文章不能再推送草稿")
        url = draft_url.strip() or "pending_manual_push"
        conn.execute(
            "UPDATE articles SET status='pushed', draft_url=? WHERE id=?",
            (url, article_id),
        )
        note = "已推送公众号草稿箱" if draft_url.strip() else "标记待推送(需人工扫码/半自动)"
        _log_review(conn, article_id, "push_draft", reviewer, note)
        conn.commit()
        return {"ok": True, "article_id": article_id, "status": "pushed", "draft_url": url, "note": note}
    finally:
        conn.close()


def clean_article(article_id: int, reason: str = "manual_clean", reviewer: str = "human",
                  db_path: str | None = None) -> dict:
    """文章清理(文章级):物理删除稿件并记录审计。仅允许清理未发布稿件。"""
    conn = connect(db_path) if db_path else connect()
    try:
        row = conn.execute(
            "SELECT status, topic_id, title FROM articles WHERE id=?", (article_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"文章不存在: {article_id}")
        if row["status"] in ("published", "backfilled"):
            raise ValueError("已发布文章不可清理,请走合规下架流程")
        conn.execute("DELETE FROM articles WHERE id=?", (article_id,))
        if row["topic_id"]:
            conn.execute(
                "INSERT INTO kb_clean_logs(topic_id, clean_type, reason) VALUES(?,?,?)",
                (row["topic_id"], "article_clean", f"{reason}({reviewer})"),
            )
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO run_logs(run_id, stage, status, detail, started_at, finished_at)
               VALUES(?, 'clean', 'success', ?, ?, ?)""",
            (f"clean_article_{article_id}", f"清理文章 #{article_id}: {row['title'][:40]}", now, now),
        )
        conn.commit()
        return {"ok": True, "article_id": article_id, "cleaned": True, "title": row["title"]}
    finally:
        conn.close()


def reject(article_id: int, reason: str, reviewer: str = "human", db_path: str | None = None) -> dict:
    """驳回:附带原因,后续触发重新生成。"""
    if not reason.strip():
        raise ValueError("驳回必须附原因")
    conn = connect(db_path) if db_path else connect()
    try:
        conn.execute(
            "UPDATE articles SET status='rejected', reject_reason=? WHERE id=?",
            (reason, article_id),
        )
        _log_review(conn, article_id, "reject", reviewer, reason)
        conn.commit()
        return {"ok": True, "article_id": article_id, "status": "rejected", "reason": reason}
    finally:
        conn.close()


# 单次批量上限:人工操作不会一次选中这么多,超了基本是误触或脚本
BULK_LIMIT = 200


def _norm_ids(article_ids) -> list[int]:
    """规范化批量 id:去重、保序、丢弃非整数。"""
    out: list[int] = []
    for x in article_ids or []:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v not in out:
            out.append(v)
    return out


def _run_bulk(article_ids, fn, action: str, **kw) -> dict:
    """逐条执行 + 收集明细。

    **逐条复用单条函数**(discard→reject / clean→clean_article),不另写一套批量 SQL ——
    否则守卫(已发布不可删)与审计日志会分叉成两套口径。
    单条失败只记入 skipped 并带上原因,不能拖垮整批。
    """
    ids = _norm_ids(article_ids)
    if len(ids) > BULK_LIMIT:
        raise ValueError(f"一次最多处理 {BULK_LIMIT} 条,当前 {len(ids)} 条")
    done: list[dict] = []
    skipped: list[dict] = []
    for aid in ids:
        try:
            r = fn(aid, **kw)
            done.append({"id": aid, "title": r.get("title") or ""})
        except ValueError as e:                      # 业务拒绝:原因明确,原样回传
            skipped.append({"id": aid, "reason": str(e)})
        except Exception as e:                       # 单条意外失败也不能中断整批
            skipped.append({"id": aid, "reason": f"未预期错误:{e}"})
    return {
        "ok": True,
        "action": action,
        "done": done,
        "skipped": skipped,
        "done_count": len(done),
        "skipped_count": len(skipped),
    }


def bulk_discard(article_ids, reason: str = "人工废弃", reviewer: str = "human",
                 db_path: str | None = None) -> dict:
    """批量废弃:状态置 rejected。**可恢复**(数据仍在,只改状态)。"""
    return _run_bulk(article_ids, reject, "discard",
                     reason=reason or "人工废弃", reviewer=reviewer, db_path=db_path)


def bulk_clean(article_ids, reason: str = "manual_clean", reviewer: str = "human",
               db_path: str | None = None) -> dict:
    """批量物理删除:仅未发布稿件。已发布/已回流的条目会跳过并在 skipped 里说明原因。"""
    return _run_bulk(article_ids, clean_article, "clean",
                     reason=reason or "manual_clean", reviewer=reviewer, db_path=db_path)


def edit_article(article_id: int, fields: dict, db_path: str | None = None) -> dict:
    """在线编辑:更新正文/标题/导语等,状态回 edited(待重新审核)。"""
    allowed = {"title", "lead", "body", "quote", "cta"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        raise ValueError("没有可更新的字段")
    conn = connect(db_path) if db_path else connect()
    try:
        sets = ", ".join(f"{k}=?" for k in updates)
        params = list(updates.values()) + [article_id]
        conn.execute(f"UPDATE articles SET {sets}, status='edited' WHERE id=?", params)
        _log_review(conn, article_id, "edit", "human", f"在线编辑字段: {list(updates)}")
        conn.commit()
        return {"ok": True, "article_id": article_id, "status": "edited"}
    finally:
        conn.close()


def confirm_published(article_id: int, draft_url: str = "", db_path: str | None = None) -> dict:
    """人工发布后确认:状态 → published(触发数据回流)。

    ⚠️ **不要把 `draft_url` 无条件写回**。旧实现是 `draft_url=?`,传空串就写 NULL ——
    而界面上这个框经常是空的,于是"确认已发布"这一步会**把已有的草稿链接清掉**。
    链接一丢,反查发表记录就再也匹配不到它(draft_msgid 是精确匹配用的),
    数据回流列表也就跟着对不上了。所以:只有传了**非空**值才更新。
    """
    conn = connect(db_path) if db_path else connect()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE articles SET status='published', published_at=? WHERE id=?",
            (now, article_id),
        )
        if str(draft_url or "").strip():
            conn.execute("UPDATE articles SET draft_url=? WHERE id=?",
                         (draft_url.strip(), article_id))
        _log_review(conn, article_id, "publish", "human", "人工发布确认")
        conn.commit()
        return {"ok": True, "article_id": article_id, "status": "published", "published_at": now}
    finally:
        conn.close()


def _log_review(conn: sqlite3.Connection, article_id: int, action: str, reviewer: str, note: str) -> None:
    """审核记录落 run_logs(带 article 标识,便于回溯)。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO run_logs(run_id, stage, status, detail, started_at, finished_at)
           VALUES(?, 'review', ?, ?, ?, ?)""",
        (f"review_article_{article_id}", action,
         f"article={article_id} reviewer={reviewer} note={note}", now, now),
    )


def list_review_queue(status_filter: str = "pending_review", db_path: str | None = None) -> list[dict]:
    """待处理列表。

    status_filter 支持逗号分隔多状态(如 "generated,pushed,pending_review"),
    便于把新生成/已推送/待审核合并展示;单状态用法兼容旧调用。
    """
    conn = connect(db_path) if db_path else connect()
    try:
        statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
        if len(statuses) == 1:
            where, params = "a.status=?", [statuses[0]]
        else:
            marks = ",".join("?" for _ in statuses)
            where, params = f"a.status IN ({marks})", statuses
        rows = conn.execute(
            f"""SELECT a.id, a.title, a.status, a.draft_url, a.reject_reason,
                       a.fact_notes, a.created_at,
                       h.title AS topic_title, h.url AS topic_url
                FROM articles a LEFT JOIN hot_topics h ON a.topic_id=h.id
                WHERE {where} ORDER BY a.created_at DESC""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _count_refs(raw: str | None) -> int:
    """统计一条文章溯源记录里的参考素材条数(主选题 + 相关报道)。"""
    if not raw:
        return 0
    try:
        rf = json.loads(raw)
    except (ValueError, TypeError):
        return 0
    if not isinstance(rf, dict):
        return 0
    return (1 if rf.get("main_topic") else 0) + len(rf.get("related_topics") or [])


def list_articles(
    status_filter: str = "all",
    limit: int = 500,
    db_path: str | None = None,
    range_key: str | None = None,
) -> dict:
    """文章库总览:按状态 + 时间范围筛选,并给出分组统计。

    返回 {status_counts, all_total, total, range, range_label, rows: [...]}

    ⚠️ 两个 total 含义不同,别混用:
    - `all_total` = **库里所有文章的总数**(各状态计数之和)→ 「全部」标签要用它
    - `total`     = **本次筛选结果的条数**(= len(rows))→ 只表示"这页拿到多少条"
    曾经前端拿 `total` 显示「全部(N)」,导致选中某个状态后「全部」也跟着变成该状态的数量。

    status_filter 支持: all(全部) / 单个状态 / 逗号分隔多状态
    """
    conn = connect(db_path) if db_path else connect()
    try:
        # 时间范围(可选):**不传 = 不加时间条件**(兼容既有调用方)。
        # 接口层的默认值是 today,所以"默认当天"由路由决定,这里保持纯粹。
        # 统计与列表必须同源 —— 否则会出现"选了今天,「已生成」还显示历史总数"。
        from app.core.daterange import normalize_range, range_bounds, range_label

        lo, hi = range_bounds(range_key) if range_key else (None, None)
        applied = normalize_range(range_key) if range_key else "all"
        time_where, time_params = "", []
        if lo and hi:
            time_where = " AND date(a.created_at) BETWEEN ? AND ?"
            time_params = [lo, hi]

        # 状态分布统计(与列表同一时间口径)
        count_rows = conn.execute(
            "SELECT a.status, COUNT(*) AS n FROM articles a WHERE 1=1" + time_where
            + " GROUP BY a.status",
            time_params,
        ).fetchall()
        counts = {r["status"]: r["n"] for r in count_rows}

        if status_filter == "all":
            where, params = "1=1", []
        else:
            statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
            if len(statuses) == 1:
                where, params = "a.status=?", [statuses[0]]
            else:
                marks = ",".join("?" for _ in statuses)
                where, params = f"a.status IN ({marks})", statuses
        rows = conn.execute(
            f"""SELECT a.id, a.title, a.status, a.draft_url, a.reject_reason,
                       a.fact_notes, a.published_at, a.created_at, a.source_refs,
                       a.published_url, a.mp_mass_sent,
                       h.title AS topic_title, h.url AS topic_url
                FROM articles a LEFT JOIN hot_topics h ON a.topic_id=h.id
                WHERE {where}{time_where} ORDER BY a.id DESC LIMIT ?""",
            params + time_params + [limit],
        ).fetchall()
        # source_refs 的完整素材文本较大,列表页只回传参考条数
        out = []
        for r in rows:
            d = dict(r)
            raw = d.pop("source_refs", None)
            d["ref_count"] = _count_refs(raw)
            out.append(d)
        return {
            "status_counts": counts,
            # 「全部」标签用这个:与状态筛选无关,但**受时间范围影响**(与列表同源)
            "all_total": sum(counts.values()),
            "total": len(out),                   # 本次筛选命中的条数
            "range": applied,
            "range_label": range_label(applied),
            "rows": out,
        }
    finally:
        conn.close()


def get_article(article_id: int, db_path: str | None = None) -> dict | None:
    conn = connect(db_path) if db_path else connect()
    try:
        row = conn.execute(
            """SELECT a.*, h.title AS topic_title, h.url AS topic_url, h.summary AS topic_summary
               FROM articles a LEFT JOIN hot_topics h ON a.topic_id=h.id
               WHERE a.id=?""",
            (article_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
