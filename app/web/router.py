"""Web API 聚合:控制面板/审核台/看板/流水线接口统一挂载到 /api。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.config import get_all_config, get_config, set_config
from app.dashboard.service import dashboard_summary, metric_detail, review_stale
from app.generator.templates import (
    delete_template,
    get_template,
    list_templates,
    save_template,
    seed_default_template,
    toggle_template,
)
from app.knowledge.cleaner import bulk_soft_delete, clean_knowledge_base, soft_delete_by_url
from app.pipeline import get_last_report, plan_theses, register_pipeline_handlers, run_clean, run_pipeline
from app import pipeline_state
from app.pusher import service as pusher
from app.pusher.markdown import markdown_to_html
from app.review.service import (
    approve,
    bulk_clean,
    bulk_discard,
    clean_article,
    confirm_published,
    edit_article,
    get_article,
    list_articles,
    list_review_queue,
    push_to_drafts,
    reject,
)
from app.backfill.service import backfill_article, stats_dashboard

router = APIRouter()

# ---- 系统 ----
@router.get("/ping")
def ping() -> dict:
    return {"pong": True}


# ---- 配置 ----
@router.get("/config")
def read_all_config() -> dict:
    return get_all_config()


@router.get("/config/{key}")
def read_config(key: str) -> dict:
    return {"key": key, "value": get_config(key)}


@router.post("/config/{key}")
def write_config(key: str, payload: dict) -> dict:
    """Body: {"value": ...}"""
    set_config(key, payload.get("value"), updated_by="api")
    return {"ok": True, "key": key, "value": get_config(key)}


# ---- 模板 ----
@router.get("/templates")
def api_list_templates() -> list[dict]:
    """列出**全部**模板(含已停用的)。

    ⚠️ 必须带 `enabled_only=False`。曾经用函数默认值 True,于是点「停用」之后模板
    直接从列表里消失 —— 面板上再也看不到它、也就无法重新启用,用户看到的是
    「我明明保存了,列表里却没有」(实测反馈)。停用是一个**状态**,要显示成状态,
    不是把条目藏起来。是否可用于生成由 `enabled` 字段自己表达。
    """
    return list_templates(enabled_only=False)


@router.get("/templates/{tid}")
def api_get_template(tid: str) -> dict:
    tpl = get_template(tid)
    if not tpl:
        raise HTTPException(404, "模板不存在")
    return tpl


@router.post("/templates")
def api_save_template(payload: dict) -> dict:
    tid = save_template(payload)
    return {"ok": True, "template_id": tid}


@router.post("/templates/{tid}/toggle")
def api_toggle_template(tid: str, payload: dict) -> dict:
    toggle_template(tid, bool(payload.get("enabled", True)))
    return {"ok": True}


@router.delete("/templates/{tid}")
def api_delete_template(tid: str) -> dict:
    delete_template(tid)
    return {"ok": True}


@router.post("/templates/seed")
def api_seed_template() -> dict:
    tid = seed_default_template()
    return {"ok": True, "template_id": tid}


# ---- 审核 ----
@router.get("/review/articles")
def api_articles(status: str = "all", limit: int = 500, range: str = "today") -> dict:
    """文章库总览:按状态 + 时间范围筛选。

    status=all|单状态|多状态逗号;range=today(默认)|7d|30d|all。
    统计与列表同一时间口径,避免"选了今天、计数还是历史总数"。
    """
    return list_articles(status, limit=min(limit, 1000), range_key=range)


@router.get("/review/queue")
def api_review_queue(status: str = "pending_review") -> list[dict]:
    return list_review_queue(status)


@router.get("/review/articles/{aid}")
def api_get_article(aid: int) -> dict:
    art = get_article(aid)
    if not art:
        raise HTTPException(404, "文章不存在")
    # 正文预览用与推送**完全相同**的转换器渲染 —— 所见即所推。
    # 否则前端会把 `## 小标题` 原样显示出来,看起来像文章坏了。
    art["body_html"] = markdown_to_html(art.get("body") or "")
    return art


@router.post("/review/approve/{aid}")
def api_approve(aid: int) -> dict:
    return approve(aid)


@router.post("/review/reject/{aid}")
def api_reject(aid: int, payload: dict) -> dict:
    return reject(aid, payload.get("reason", ""))


@router.post("/review/edit/{aid}")
def api_edit(aid: int, payload: dict) -> dict:
    return edit_article(aid, payload)


@router.post("/review/publish/{aid}")
def api_publish(aid: int, payload: dict = {}) -> dict:
    return confirm_published(aid, payload.get("draft_url", ""))


@router.post("/review/push/{aid}")
def api_push_draft(aid: int, payload: dict = {}) -> dict:
    """文章级:推送到公众号草稿箱(或标记待人工推送)。"""
    try:
        return push_to_drafts(aid, payload.get("draft_url", ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ---- 公众号推送(Playwright 自动化)----
@router.get("/pusher/status")
def api_pusher_status() -> dict:
    """登录会话 + 推送任务状态(前端轮询)。"""
    return pusher.snapshot()


@router.get("/pusher/publish-window")
def api_publish_window() -> dict:
    """读者活跃时段建议:现在是否适合推送、下一个推荐时段在什么时候。

    ⚠️ 默认时段是**经验值**,不是从你的数据算出来的;
    后台「数据分析 → 内容分析」里有真实活跃时段,照着改配置 `publish_window` 即可。
    """
    from app.core.publish_window import status

    return status()


@router.get("/pusher/diagnose")
def api_pusher_diagnose(in_thread: bool = False) -> dict:
    """推送环境自检:Playwright 能否在本进程启动 + 关键环境变量。

    in_thread=1 时在自建线程里测(与推送任务相同的线程环境)。
    """
    return pusher.diagnose(in_thread=in_thread)


@router.post("/pusher/login")
def api_pusher_login(payload: dict = {}) -> dict:
    """启动扫码登录(后台线程 + 可见浏览器),立即返回。

    force=1 用于「重新扫码登录」:忽略已保存会话,强制弹出二维码。
    """
    return pusher.start_login(
        payload.get("scan_wait"), force=bool(payload.get("force", False))
    )


@router.post("/pusher/logout")
def api_pusher_logout() -> dict:
    """清除本地登录会话。"""
    return pusher.logout()


@router.post("/pusher/push/{aid}")
def api_pusher_push(aid: int, payload: dict = {}) -> dict:
    """真·推送草稿:后台打开公众号后台建稿并保存,进度见 /pusher/status。"""
    return pusher.start_push(aid, headless=bool(payload.get("headless", False)))


@router.get("/pusher/plan-b/{aid}")
def api_pusher_plan_b(aid: int) -> dict:
    """方案 B:导出排好版的可粘贴正文(自动推送失败时的降级路径)。"""
    return pusher.plan_b_text(aid)


@router.post("/pusher/cover/{aid}")
def api_pusher_cover_gen(aid: int, payload: dict = {}) -> dict:
    """为文章生成封面图(独立进程渲染,通常 2~5 秒)。

    模板化合成:中文字体完美、风格稳定、零版权风险;
    生成的图在推送时会自动用作封面(优先于 mp_cover_image)。
    """
    return pusher.generate_cover(aid, str(payload.get("style") or ""))


@router.get("/pusher/cover/{aid}")
def api_pusher_cover_file(aid: int, style: str = ""):
    """回传已生成的封面 PNG,供前端预览。未生成时 404。"""
    from fastapi.responses import FileResponse

    p = pusher.cover_path(aid, style)
    if not p:
        raise HTTPException(404, "封面尚未生成")
    return FileResponse(p, media_type="image/png")


@router.post("/review/backfill/{aid}")
def api_backfill_article(aid: int) -> dict:
    """文章级:已发布稿件手动立即回流一次效果数据。"""
    try:
        return backfill_article(aid)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/backfill/sync-published")
def api_sync_published() -> dict:
    """反查发布状态:从公众号「发表记录」确认哪些文章真的发出去了。

    文章发表后系统本来不知道(一直停在 pushed),回流因此永远筛不出待办。
    这个动作**只认本系统推送过的文章**(draft_url 带 appmsgid),并回填
    发表时间 / 公开链接 / 是否群发。

    ⚠️ 只读后台、只推进不降级;后台会话失效会返回可读的提示。
    """
    from app.backfill.service import sync_published as _sync
    from app.pusher.mp_api import MpSessionError

    try:
        return _sync()
    except MpSessionError as e:
        raise HTTPException(409, f"公众号会话不可用:{e}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"反查失败:{type(e).__name__}: {str(e)[:200]}") from e


@router.get("/backfill/stats")
def api_backfill_stats(range: str = "all") -> dict:
    """回流数据看板:每篇本系统发布文章的反馈指标(阅读/点赞/分享/推荐/留言)+ 汇总。

    `range` 按**发表时间**过滤(今天/近7天/近30天/全部时间),默认全部时间。
    """
    return stats_dashboard(range)


@router.post("/backfill/refresh")
def api_backfill_refresh() -> dict:
    """立即刷新:**先反查发布状态,再抓取全部已发布文章的反馈数据**(不看 T+1/3/7 排期)。

    ⚠️ 两步都做,顺序不能反 —— 反查写「后台核对」列的依据(`published_url`),抓取只写指标。
    只抓取不反查会出现"有反馈数据、却标着未找到"的矛盾行(#45/#46 实测踩过)。
    后台只请求一次(反查与抓取共用同一份快照)。会话失效返回 409 + 可执行提示。
    """
    from app.backfill.service import refresh_all
    from app.pusher.mp_api import MpSessionError

    try:
        out = refresh_all()
    except MpSessionError as e:
        raise HTTPException(409, f"公众号会话不可用:{e}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"回流失败:{type(e).__name__}: {str(e)[:200]}") from e

    sync, stats = out["sync"], out["stats"]
    # 顶层保持既有字段(backfilled/skipped/failed/error/hint),前端判错逻辑不用改
    return {**stats,
            "sync": {k: sync.get(k) for k in
                     ("scanned", "matched", "by_draft", "by_title", "mp")}}


@router.post("/review/clean/{aid}")
def api_clean_article(aid: int, payload: dict = {}) -> dict:
    """文章级:清理(删除)未发布稿件并记录审计。"""
    try:
        return clean_article(aid, reason=payload.get("reason", "manual_clean"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/review/bulk")
def api_review_bulk(payload: dict) -> dict:
    """文章批量操作(单条也走这里,保证口径一致)。

    - action=discard:废弃 → status=rejected,**可恢复**,数据不删
    - action=clean:物理删除 → 仅未发布稿件;已发布/已回流会跳过并给出原因

    返回 {done:[...], skipped:[{id, reason}], done_count, skipped_count},
    部分失败不是整体失败 —— 前端据此提示"成功 N 条,跳过 M 条(原因)"。
    """
    action = str(payload.get("action") or "").strip()
    ids = payload.get("ids") or []
    if action not in ("discard", "clean"):
        raise HTTPException(400, "action 必须是 discard(废弃) 或 clean(删除)")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "ids 不能为空")
    try:
        if action == "discard":
            return bulk_discard(ids, reason=payload.get("reason") or "人工废弃")
        return bulk_clean(ids, reason=payload.get("reason") or "manual_clean")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ---- 知识库/合规 ----
@router.post("/clean")
def api_clean() -> dict:
    return run_clean()


@router.post("/hot/delete-by-url")
def api_delete_by_url(payload: dict) -> dict:
    url = payload.get("url", "")
    ok = soft_delete_by_url(url)
    if not ok:
        raise HTTPException(404, "未找到该链接热点")
    return {"ok": True}


@router.post("/hot/bulk-delete")
def api_bulk_delete_hot(payload: dict) -> dict:
    """热点批量废弃(分析报告里把素材移出热点池)。

    软删除 + kb_clean_logs 审计,不物理删数据(合规要求可追溯)。
    urls 与 ids 二选一或同时给均可;部分未命中只记 skipped,不算整体失败。
    """
    urls = payload.get("urls") or []
    ids = payload.get("ids") or []
    if not urls and not ids:
        raise HTTPException(400, "urls 与 ids 不能都为空")
    try:
        return bulk_soft_delete(urls=urls, ids=ids,
                                reason=payload.get("reason") or "manual_bulk")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ---- 看板 ----
@router.get("/dashboard")
def api_dashboard() -> dict:
    return dashboard_summary()


@router.get("/dashboard/stale")
def api_stale(hours: int | None = None) -> list[dict]:
    """滞留稿件列表。

    `hours` 不传就用配置 `review_stale_hours` —— 面板上那个「审核滞留告警(小时)」
    以前不生效(前端写死 24),现在以**配置为默认**、显式传参仍可覆盖。
    """
    if hours is None:
        try:
            from app.core.config import get_config

            hours = int(get_config("review_stale_hours") or 24)
        except (TypeError, ValueError):
            hours = 24
    return review_stale(stale_hours=hours)


@router.get("/dashboard/metric/{metric}")
def api_metric_detail(metric: str) -> dict:
    """指标点击详情。metric: fetch_today/generated_today/pending_review/
    generation_failed/published/backfilled"""
    return metric_detail(metric)


# ---- 流水线 ----
# ⚠️ 这条必须注册在 `/pipeline/{stage}` **之前** —— FastAPI 按注册顺序匹配,
# 否则 "theses" 会被当成一个 stage 名吃掉。
@router.post("/pipeline/theses")
def api_plan_theses(payload: dict = {}) -> dict:
    """两步生成第一步:按人工选材产出候选论点(不写文章)。

    传入 `{"source": {...}, "n": 4}`;返回候选论点 + 推荐的文章类型。
    选定后再调 `/pipeline/generate`,并带上 `source.thesis` 与 `source.article_type`。
    """
    body = payload or {}
    return plan_theses(body.get("source") or {}, int(body.get("n") or 4))


@router.post("/pipeline/titles")
def api_suggest_titles(payload: dict = {}) -> dict:
    """给一篇文章出多个**形态不同**的候选标题,供人工挑选。

    传入 `{"article_id": 12, "n": 3}`。只调 LLM、**不改库** ——
    选定哪个用,走已有的文章编辑流程落库。

    为什么值得单做:打开率决定阅读量,而转发的前提是被打开;
    模型默认只会给一个"稳妥的标准标题",让人在几个成型方向里挑更划算。
    """
    from app.generator.llm import LLMError
    from app.generator.thesis import GenerationError
    from app.generator.titles import suggest

    body = payload or {}
    aid = body.get("article_id")
    if not aid:
        raise HTTPException(400, "缺少 article_id")
    art = get_article(int(aid))
    if not art:
        raise HTTPException(404, f"文章不存在: {aid}")
    try:
        return suggest(art, n=int(body.get("n") or 3))
    except (GenerationError, LLMError) as e:
        raise HTTPException(400, str(e)) from e


@router.post("/pipeline/{stage}")
def api_run_pipeline(stage: str, payload: dict = {}) -> dict:
    """执行流水线环节。

    generate / all 可带 `source` 指定**人工选定的生成素材**:
    `{"source": {"cluster_topic": "AI", "topic_ids": [67, 30], "angle": "...", "summary": "..."}}`
    给了 source 就不再自动取分析报告 Top1。

    两步生成还可在 source 里带:
    - `thesis`:人工选定的核心论点 → 变成 prompt 里的**硬约束**;
    - `article_type`:人工选定的论证姿态(industry_judgment/critical/teardown/forecast),
      只影响这一篇,不写回全局配置。
    """
    return run_pipeline(stage, source=(payload or {}).get("source"))


@router.get("/pipeline/status")
def api_pipeline_status() -> dict:
    """当前/最近一次流水线的执行进度:阶段步骤、耗时、实时日志、失败原因。

    前端在执行期间轮询此接口渲染进度条,失败时直接展示 error + hint。
    """
    return pipeline_state.snapshot()


@router.get("/analyze/report")
def api_last_report(range: str = "today") -> dict:
    """按时间范围返回归纳报告(供「分析报告」页),默认**当天**。

    说明:报告按所选范围**实时聚类**,不是回放某次分析的历史快照 ——
    快照里的 `members` 只截了前 10 条,按时间过滤成员算不出该主题在时段内的真实条数,
    计数会和列表对不上。聚类本身只要几毫秒(不调 LLM),所以直接重算,数字天然一致。
    `range=all` 即"全部时间",看所有累积信息。
    """
    from app.analyzer.service import report_for_range

    out = report_for_range(range)
    out["updated_at"] = None  # 实时结果没有"落库时间",前端以本地加载时间为准
    return out


@router.get("/analyze/growth-profile")
def api_growth_profile() -> dict:
    """读「传播画像」:从账号自己的转发数据学到的标题形态/高频词,以及各自的 lift。

    ⚠️ 这是**弱信号**:几十篇的样本只够给出方向,所以它只作为热度的有界系数(默认 ±35%)。
    """
    from app.analyzer.growth import load_profile

    p = load_profile()
    return {"profile": p, "has_profile": bool(p.get("ok"))}


@router.post("/analyze/growth-profile")
def api_rebuild_growth_profile() -> dict:
    """重建传播画像:拉公众号「发表记录」的真实转发数据,学出什么样的标题会被转。

    只读后台。会话失效返回 409 + 可执行提示。
    """
    from app.analyzer.growth import refresh_profile
    from app.pusher.mp_api import MpSessionError

    try:
        p = refresh_profile()
    except MpSessionError as e:
        raise HTTPException(409, f"公众号会话不可用:{e}") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"重建画像失败:{type(e).__name__}: {str(e)[:200]}") from e
    if not p.get("ok"):
        raise HTTPException(400, p.get("reason") or "样本不足,无法学习")
    return {"ok": True, "profile": p}


register_pipeline_handlers()
