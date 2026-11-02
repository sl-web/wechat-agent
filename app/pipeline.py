"""流水线编排:把 抓取→分析→生成→推送 串成整条可调度流水线(PRD 4.1)。

每日流程:
  ① 抓取(所有已启用数据源) → 入库
  ② 分析(清洗/评分/聚类) → 选 Top1 选题
  ③ 生成(模板 + LLM) → 落库
  ④ 推送(Playwright) → 待审核
每环节写断点,失败可续跑;空窗场景按 4.4 终止并通知。
"""
from __future__ import annotations

from app import pipeline_state as ps
from app.backfill.service import refresh_all
from app.analyzer.service import analyze, persist_scores
from app.collectors.rss import load_feeds
from app.collectors.storage import save_hot_topics
from app.core.circuit_breaker import new_run_id
from app.core.config import get_all_config
from app.core.checkpoint import RunRecorder
from app.core.db import connect, query
from app.core.logger import get_logger
from app.core.settings import settings
from app.dashboard.notify import Notifier
from app.generator.service import build_default_refs, generate_article
from app.generator.templates import (
    DEFAULT_TEMPLATE_ID,
    get_template,
    resolve_structure,
    seed_default_template,
)
from app.knowledge.cleaner import clean_knowledge_base
from app.review.service import list_review_queue
from app.scheduler import register_stage

logger = get_logger("pipeline")

# configs 表中存储最近一次分析报告的键
LAST_REPORT_KEY = "last_analysis_report"


def _fail(report: dict, reason: str, error: str, hint: str = "") -> dict:
    """统一的失败返回:带机器可读 reason + 人类可读 error/hint。

    error 说明「发生了什么」,hint 说明「该怎么办」,前端直接展示,不再出现
    empty_hotspot 这类无信息量的代号。
    """
    return {**report, "ok": False, "reason": reason, "error": error, "hint": hint}


def _build_gen_refs(topic: dict, template_id: str, structure_id: str, rid: str) -> dict:
    """组装生成参考溯源。

    选题来源有两种:
    - 分析环节产出的聚类主题(含 members)→ 把同主题其它报道一并作为参考素材;
    - 直接取自热点库的行(仅跑 generate 时)→ 只有主选题。
    """
    members = topic.get("members") or []
    analysis: dict = {}
    if members:
        analysis = {
            "cluster_topic": topic.get("topic", ""),
            "cluster_count": topic.get("hot_count", len(members)),
            "cluster_heat_sum": topic.get("heat_sum"),
            "suggested_angle": topic.get("suggested_angle", ""),
            # 人工选材时有专用字段;自动选题(聚类主题)退回 topic.summary
            "cluster_summary": topic.get("cluster_summary") or topic.get("summary", ""),
        }
    refs = build_default_refs(topic, template_id, structure_id, related=members, analysis=analysis)
    refs["analysis_run_id"] = rid
    refs["analysis"]["run_id"] = rid
    if topic.get("by_manual"):
        # 溯源里标明:这篇是人工选材生成的,不是自动取的分析 Top1
        refs["selection"] = "manual"
    return refs


def plan_theses(source: dict, n: int = 4) -> dict:
    """两步生成第一步:按人工选材产出**张力 + 候选论点**(不写文章)。

    返回里 `tensions` 是这组素材里的冲突(可能为空 —— 张力是增强项,不是前置依赖),
    每条论点用 `tension_id` 指向它来自哪条张力。

    用户挑一个论点(可连带改选张力)之后,再调
    `run_pipeline("generate", source={...thesis, tension...})` 走第二步真正写文章 ——
    这时论点与张力都会变成 prompt 里的硬约束。

    `rule_suggestion` 字段是规则启发式的独立判断,给用户一个交叉参考:
    它与 LLM 推荐不一致时,说明这组素材的取向确实模糊,值得自己拍板。
    """
    from app.analyzer.type_advisor import suggest_type
    from app.generator import thesis as thesis_mod

    src = source or {}
    if not (src.get("topic_ids") or src.get("topic_urls")):
        return {
            "ok": False, "reason": "no_source",
            "error": "没有收到素材:需要先勾选热点条目",
            "hint": "到「分析报告」页展开主题,勾选至少 1 条成员报道后再生成论点。",
        }
    topic = _topic_from_source(src)
    if not topic:
        return {
            "ok": False, "reason": "invalid_source",
            "error": "所选素材不可用(热点可能已被清理或删除)",
            "hint": "回到「分析报告」页重新勾选素材。",
        }

    members = topic.get("members") or []
    analysis = {
        "cluster_topic": topic.get("topic", ""),
        "cluster_count": topic.get("hot_count", len(members) + 1),
        "cluster_summary": topic.get("cluster_summary") or "",
        "suggested_angle": topic.get("suggested_angle") or "",
    }
    rule = suggest_type(topic.get("topic", ""), [topic] + members)
    try:
        res = thesis_mod.generate(topic, related=members, analysis=analysis, n=n)
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:300]
        logger.warning("候选论点生成失败: %s", msg)
        return {"ok": False, "reason": "thesis_failed", "error": msg,
                "hint": _generate_hint(msg)}

    res.update({
        "ok": True,
        "cluster_topic": topic.get("topic", ""),
        "material_count": 1 + len(members),
        "rule_suggestion": rule,
    })
    return res


def _rescale_summary(text: str, n: int) -> str:
    """把聚类摘要里的条数改成**本次实际选材条数**。

    原摘要是按整个主题算的(可能"聚合 38 条"),人工只挑了 3 条时,
    摘要与实际素材对不上,会让溯源自相矛盾、也可能误导模型。
    """
    import re

    return re.sub(r"聚合\s*\d+\s*条", f"本次选材 {n} 条", text or "")


def _topic_from_source(source: dict) -> dict | None:
    """按**人工选定的素材**组装选题,让「生成什么」由人决定而不是自动取 Top1。

    source = {
        "cluster_topic": "AI",          # 可选:所属分析主题名(写进溯源,便于回看)
        "topic_ids": [67, 30, 12],      # 勾选的热点 id  } 二者其一即可,
        "topic_urls": ["https://…"],    # 勾选的热点 url } url 更稳(老报告的成员项没有 id)
        "angle": "...",                 # 可选:建议角度(来自分析报告)
        "summary": "...",               # 可选:聚类摘要
    }
    **第一条为主选题**,其余作同主题相关报道;两组键都按用户勾选顺序还原。
    返回值与「分析报告里的聚类主题」同构,可直接喂给 `_build_gen_refs`,
    这样人工选材与自动选题走**完全相同的**溯源与素材组装链路。
    """
    ids: list[int] = []
    for x in source.get("topic_ids") or []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i not in ids:                 # 去重但保持用户勾选顺序
            ids.append(i)
    urls: list[str] = []
    for u in source.get("topic_urls") or []:
        u = str(u or "").strip()
        if u and u not in urls:
            urls.append(u)
    if not ids and not urls:
        return None

    conn = connect()
    try:
        picked: list[dict] = []
        seen: set = set()
        # 先按 url 取(url 是热点的天然唯一键,且老报告的成员条目可能没有 id)
        if urls:
            marks = ",".join("?" * len(urls))
            rows = query(
                conn,
                f"SELECT * FROM hot_topics WHERE deleted=0 AND url IN ({marks})",
                tuple(urls),
            )
            by_url: dict = {}
            for r in rows:
                by_url.setdefault(r["url"], r)     # 同 url 取最新一条
            for u in urls:                          # 保持用户勾选顺序
                r = by_url.get(u)
                if r and r["id"] not in seen:
                    picked.append(r)
                    seen.add(r["id"])
        if ids:
            marks = ",".join("?" * len(ids))
            rows = query(
                conn,
                f"SELECT * FROM hot_topics WHERE deleted=0 AND id IN ({marks})",
                tuple(ids),
            )
            by_id = {r["id"]: r for r in rows}
            for i in ids:
                r = by_id.get(i)
                if r and r["id"] not in seen:
                    picked.append(r)
                    seen.add(r["id"])
    finally:
        conn.close()

    if not picked:
        return None                       # 选中的热点都被清理了

    main = picked[0]
    topic = dict(main)
    topic.update({
        "topic": source.get("cluster_topic") or main.get("title", ""),
        "hot_count": len(picked),
        "heat_sum": round(sum(float(r.get("heat_score") or 0) for r in picked), 4),
        # 不要覆盖 summary —— 那会让主选题的摘要变成聚类摘要。
        # 聚类摘要有专用字段,由 _build_gen_refs 优先读取。
        "cluster_summary": _rescale_summary(source.get("summary", ""), len(picked)),
        "suggested_angle": source.get("angle", ""),
        "members": picked[1:],            # 其余作为「同主题相关报道」
        "sample": {
            "id": main.get("id"),
            "title": main.get("title"),
            "url": main.get("url"),
            "source": main.get("source"),
            "summary": main.get("summary"),
        },
        "by_manual": True,                # 标记来源:人工选材
    })
    return topic


def _generate_hint(reason: str) -> str:
    """把 LLM 报错翻译成可执行的排查建议。"""
    r = (reason or "").lower()
    if "timeout" in r or "timed out" in r:
        return "LLM 响应超时(默认上限 420 秒):可调大 .env 的 LLM_TIMEOUT,或换更快的模型;也可稍后重试。"
    if any(k in r for k in ("connection", "connect", "unreachable")):
        return "无法连接 LLM 服务:检查本机网络/代理,并确认「控制面板」里 LLM 接口地址可达。"
    if any(k in r for k in ("api key", "apikey", "401", "unauthorized", "invalid_api_key", "authentication")):
        return "LLM 鉴权失败:到「控制面板」检查 API Key 是否填写正确、是否已过期。"
    if any(k in r for k in ("429", "rate limit", "quota", "insufficient", "balance", "余额", "欠费")):
        return "LLM 触发限流或额度不足:稍后重试,或检查账户余额/配额。"
    if "熔断" in (reason or ""):
        return "该选题连续失败已触发熔断:重新分析换个选题,或修复 LLM 配置后重试。"
    if "字数" in (reason or "") or "length" in r:
        return "生成内容未达模板字数要求:可放宽模板字数下限,或更换选题重试。"
    return "到「控制面板」检查 LLM 配置与网络连通性;失败详情见「运行监控」。"


def _save_analysis_report(rep: dict, run_id: str) -> bool:
    """把最近一次分析报告存入 configs 表,供分析报告页读取。"""
    try:
        from app.core.config import set_config

        payload = {"report": rep, "run_id": run_id}
        set_config(LAST_REPORT_KEY, payload, updated_by="pipeline")
        return True
    except Exception:  # noqa: BLE001
        return False


def get_last_report() -> dict | None:
    """读取最近一次分析报告。"""
    from app.core.config import get_config

    payload = get_config(LAST_REPORT_KEY)
    if not isinstance(payload, dict) or "report" not in payload:
        return None
    return payload


def _collectors():
    """按配置实例化数据源采集器。

    - rss:   默认信息源,免费 RSS 聚合(量子位/IT之家/InfoQ/雷峰网/开源中国/Solidot/少数派/新浪科技)
    - wechat/douyin: 保留的接口/爬虫模式(需第三方 API 授权,可选启用)
    """
    cfg = get_all_config()
    sources = cfg.get("data_sources", ["rss"])
    keywords = cfg.get("keywords", [])
    top_n = cfg.get("top_n", 20)
    feeds = load_feeds(cfg)
    collectors = []
    for s in sources:
        if s == "rss":
            try:
                from app.collectors.rss import RssCollector

                collectors.append(
                    RssCollector(keywords=keywords, top_n=top_n, feeds=feeds or None)
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("RSS 采集器初始化失败: %s", e)
        elif s == "douyin":
            try:
                from app.collectors.douyin import DouyinCollector

                collectors.append(DouyinCollector(keywords=keywords, top_n=top_n))
            except Exception as e:  # noqa: BLE001
                logger.warning("抖音采集器初始化失败: %s", e)
        elif s == "wechat":
            try:
                from app.collectors.wechat import WechatCollector

                collectors.append(WechatCollector(keywords=keywords, top_n=top_n))
            except Exception as e:  # noqa: BLE001
                logger.warning("微信采集器初始化失败: %s", e)
    return collectors


def run_pipeline(stage: str = "all", run_id: str | None = None,
                 source: dict | None = None) -> dict:
    """执行流水线。stage: all/fetch/analyze/generate/push/backfill/clean

    source: 人工选定的生成素材(仅 generate / all 生效),见 `_topic_from_source`。
            给了它就不再自动取分析报告 Top1 —— 让「写什么」由人决定。

    外层负责进度状态(供前端轮询)与最终结果归档;各环节实现见 _run_pipeline。
    """
    cfg = get_all_config()
    rid = run_id or new_run_id()
    report: dict = {"run_id": rid, "stage": stage}
    ps.begin(stage, rid)
    try:
        result = _run_pipeline(stage, rid, cfg, report, source)
    except Exception as e:  # noqa: BLE001 未预期异常也要让前端看到原因
        err = f"{type(e).__name__}: {e}"
        logger.exception("流水线执行异常")
        ps.finish(False, error=err, hint="属未预期异常,请到「运行监控」查看失败记录与后端日志。")
        raise
    ps.finish(
        bool(result.get("ok")),
        error=result.get("error"),
        hint=result.get("hint"),
        result=_brief_result(result),
    )
    return result


def _brief_result(r: dict) -> dict:
    """给进度面板用的精简结果(避免把大对象塞进运行状态)。"""
    keys = ("crawled", "fetched", "duplicated", "scored", "clusters", "article_id",
            "article_title", "backfilled", "cleaned", "reason", "analyzed_at")
    return {k: r[k] for k in keys if k in r}


def _run_pipeline(stage: str, rid: str, cfg: dict, report: dict,
                  source: dict | None = None) -> dict:
    notifier = Notifier()

    with RunRecorder(rid) as run:
        # ---------- ① 抓取 ----------
        if stage in ("all", "fetch"):
            ps.mark("fetch", "running", "正在连接 RSS 源…")
            run.log("fetch", "checkpoint", "开始抓取")
            all_items: list[dict] = []
            source_diag: list[dict] = []
            for col in _collectors():
                try:
                    items = col.collect(mode="auto")
                    for d in getattr(col, "source_diag", []) or []:
                        source_diag.append({"collector": col.source, **d})
                    all_items.extend(items)
                    logger.info("采集器 %s 抓到 %d 条", col.source, len(items))
                except Exception as e:  # noqa: BLE001
                    err = f"{type(e).__name__}: {e}"
                    logger.error("采集器 %s 失败: %s", col.source, err)
                    source_diag.append({"collector": col.source, "name": col.source, "ok": False,
                                        "count": 0, "error": err[:160], "elapsed": 0})
            report["sources"] = source_diag
            ok_src = [d for d in source_diag if d.get("ok")]
            bad_src = [d for d in source_diag if not d.get("ok")]
            report["source_failed"] = [
                {"name": d.get("name", ""), "error": d.get("error", "")} for d in bad_src
            ]

            if not all_items:
                if not ok_src and bad_src:
                    # 全部源不可达:原因具体到每个源,而不是笼统的"热点池为空"
                    detail = ";".join(f"{d.get('name')}({d.get('error', '')[:70]})" for d in bad_src)
                    msg = f"全部 {len(bad_src)} 个 RSS 源抓取失败:{detail}"
                    hint = "本机无法访问这些源(断网/代理/被限流)。确认能打开这些网站后重试即可,无需重启服务。"
                elif not source_diag:
                    msg = "未配置任何启用的数据源"
                    hint = "到「控制面板」检查数据源配置(data_sources / rss_feeds)。"
                else:
                    msg = "RSS 源可达,但没有解析到任何条目"
                    hint = "可能是源临时返回空内容,稍后重试;或到「分析报告」页放宽关键词。"
                logger.warning(msg)
                run.log("fetch", "failed", msg)
                notifier.send("抓取失败告警", msg)
                ps.mark("fetch", "failed", msg)
                return _fail(report, "fetch_failed", msg, hint)

            saved = save_hot_topics(all_items)
            dup = max(0, len(all_items) - saved)
            detail = f"抓到 {len(all_items)} 条,新入库 {saved} 条,重复 {dup} 条"
            if bad_src:
                detail += f";{len(bad_src)} 个源失败:{','.join(str(d.get('name')) for d in bad_src)}"
            run.log("fetch", "success", detail)
            report["crawled"] = len(all_items)
            report["fetched"] = saved
            report["duplicated"] = dup
            ps.mark("fetch", "success", detail)
            if stage != "all":
                return {**report, "ok": True}

        # ---------- ② 分析 ----------
        if stage in ("all", "analyze"):
            ps.mark("analyze", "running", "评分 / 聚类中…")
            run.log("analyze", "checkpoint", "开始分析")
            conn = connect()
            topics = query(conn, "SELECT * FROM hot_topics WHERE deleted=0 ORDER BY id DESC LIMIT 200")
            conn.close()
            if not topics:
                msg = "热点库为空,没有可分析的内容"
                run.log("analyze", "failed", msg)
                ps.mark("analyze", "failed", msg)
                return _fail(report, "no_topics", msg, "先点「仅抓取」把热点抓回来,再执行分析。")
            result = analyze(topics, cfg)
            # 评分写回库(看板/后续环节读取一致的热度)。
            # analyzed_at = 最近分析时间(每次刷新);first_analyzed_at = 首次分析时间(只写一次)。
            # 看板的「今日分析」用后者 —— 否则重跑一次分析就把历史条目全算进今天。
            saved_scores = persist_scores(result["scored"])
            report["scored"] = saved_scores
            report["clusters"] = len(result["clusters"])
            report["top_topic"] = result["report"]["topics"][0] if result["report"]["topics"] else None
            detail = (
                f"聚类 {len(result['clusters'])} 个主题,"
                f"Top1={report['top_topic']['topic'] if report['top_topic'] else '无'},"
                f"评分落库 {saved_scores} 条"
            )
            run.log("analyze", "success", detail)
            ps.mark("analyze", "success", detail)
            # 分析报告落库,供「分析报告」页查看
            try:
                from datetime import datetime

                report["report_saved"] = _save_analysis_report(result["report"], rid)
                report["analyzed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            except Exception as e:  # noqa: BLE001
                logger.warning("分析报告落库失败: %s", e)
            if stage != "all":
                return {**report, "ok": True}

        # ---------- ③ 生成(Top1) ----------
        if stage in ("all", "generate"):
            ps.mark("generate", "running", "调用 LLM 生成中(真实模型通常 1~5 分钟,请耐心等待)…")
            run.log("generate", "checkpoint", "开始生成")
            # ⚠️ 这条 seed 只负责"补齐内置模板缺失的结构"。它**不再覆盖用户改过的结构**
            # (2026-09-13 修):以前每次生成都调它,而它会拿代码里的默认 sections 覆盖同名结构
            # → 用户在「模板库」里的改动一到生成就被回滚,表现就是"模板保存了却不起作用"。
            # 模板被用户删掉时也不再复活它(面板上有「注入默认模板」可以显式加回来)。
            if get_template(DEFAULT_TEMPLATE_ID):
                seed_default_template()
            # 人工选材优先:用户勾了素材就按勾选来,不被自动 Top1 覆盖
            if (source or {}).get("topic_ids"):
                picked = _topic_from_source(source)
                if not picked:
                    msg = "所选素材不可用(热点可能已被清理或删除)"
                    run.log("generate", "failed", msg)
                    ps.mark("generate", "failed", msg)
                    return _fail(report, "invalid_source", msg,
                                 "回到「分析报告」页重新勾选素材后再生成。")
                report["top_topic"] = picked
                n_rel = len(picked.get("members") or [])
                run.log("generate", "checkpoint",
                        f"按人工选材生成:主选题「{str(picked.get('title'))[:26]}」+ 相关报道 {n_rel} 条")
            if not report.get("top_topic"):
                conn = connect()
                topics = query(conn, "SELECT * FROM hot_topics WHERE deleted=0 ORDER BY heat_score DESC LIMIT 1")
                conn.close()
                if not topics:
                    msg = "没有可用于生成的热点(选题为空)"
                    run.log("generate", "failed", msg)
                    ps.mark("generate", "failed", msg)
                    return _fail(report, "no_topic_for_generate", msg, "先执行「仅分析」得到选题,再点「仅生成」。")
                report["top_topic"] = topics[0]
            topic = report["top_topic"]
            # 两步生成第二步:用户选定的论点、张力与论证姿态(只影响这一篇)
            chosen_thesis = str((source or {}).get("thesis") or "").strip()
            chosen_tension = str((source or {}).get("tension") or "").strip()
            article_type = str((source or {}).get("article_type") or "").strip()
            if chosen_thesis:
                run.log("generate", "checkpoint",
                        f"按人工选定论点生成:{chosen_thesis[:60]}")
                ps.mark("generate", "running", "按你选定的论点写作中…")
            if chosen_tension:
                run.log("generate", "checkpoint",
                        f"围绕人工选定张力:{chosen_tension[:60]}")
            report["chosen_thesis"] = chosen_thesis
            report["chosen_tension"] = chosen_tension
            report["article_type"] = article_type
            template_id = str(cfg.get("default_template") or DEFAULT_TEMPLATE_ID)
            tpl = get_template(template_id)
            if not tpl:
                run.log("generate", "checkpoint",
                        f"配置的模板 {template_id} 不存在(可能已被删除),回落到内置模板")
                template_id = DEFAULT_TEMPLATE_ID
                seed_default_template()
                tpl = get_template(template_id)

            # 结构必须**属于这个模板**(用户可能换了模板却没动「默认文章结构」)
            structure_id = resolve_structure(tpl, str(cfg.get("default_structure") or ""))
            if structure_id != (cfg.get("default_structure") or ""):
                run.log("generate", "checkpoint",
                        f"模板 {template_id} 里没有结构 {cfg.get('default_structure')},改用 {structure_id}")
            refs = _build_gen_refs(topic, template_id, structure_id, rid)
            report["refs_summary"] = {
                "main_topic_id": (refs.get("main_topic") or {}).get("id"),
                "related_count": len(refs.get("related_topics") or []),
                "cluster_topic": (refs.get("analysis") or {}).get("cluster_topic", ""),
            }
            n_rel = len(refs.get("related_topics") or [])
            ps.mark("generate", "running",
                    f"调用 LLM 生成中(主选题 1 条 + 相关报道 {n_rel} 条作素材,通常 1~5 分钟)…")
            try:
                article = generate_article(
                    topic, template_id, structure_id, rid, refs=refs,
                    chosen_thesis=chosen_thesis, chosen_tension=chosen_tension,
                    article_type=article_type,
                )
                report["article_id"] = article.get("id")
                report["article_title"] = article.get("title")
                report["generated"] = True
                detail = (
                    f"文章 #{article.get('id')}: {article.get('title', '')[:36]}"
                    f"(参考 {1 + n_rel} 条素材)"
                )
                run.log("generate", "success", detail)
                ps.mark("generate", "success", detail)
            except Exception as e:  # noqa: BLE001 (LLM 无 key/网络失败等)
                reason = str(e)[:200]
                hint = _generate_hint(reason)
                logger.error("生成环节失败: %s", reason)
                report["generated"] = False
                report["generate_error"] = reason
                run.log("generate", "failed", f"生成失败: {reason}")
                ps.mark("generate", "failed", f"生成失败:{reason}")
                return _fail(report, "generate_failed", f"生成失败:{reason}", hint)

        # ---------- ④ 推送(不再纳入 all 一键流程) ----------
        # 推送草稿改为文章级动作:人工在审核台通过后,对单篇文章触发
        # POST /api/review/push/{aid}(见 review.service.push_to_drafts)。
        # 独立 stage="push" 仅保留给手动单跑/调度器。
        if stage == "push":
            from app.review.service import get_article

            ps.mark("push", "running", "标记待推送…")
            aid = report.get("article_id")
            if aid:
                conn = connect()
                conn.execute(
                    "UPDATE articles SET status='pushed', draft_url='pending_manual_push' WHERE id=? AND status='generated'",
                    (aid,),
                )
                conn.commit()
                conn.close()
                run.log("push", "success", f"文章 #{aid} 已标记待推送(人工确认草稿)")
                ps.mark("push", "success", f"文章 #{aid} 已标记待推送")
                report["push"] = "pending_manual"
            else:
                ps.mark("push", "success", "无待推送文章(推送已改为文章级操作)")
            return {**report, "ok": True}
        # ---------- ⑤ 回流(独立触发,不并入 all) ----------
        if stage == "backfill":
            ps.mark("backfill", "running", "回流效果数据…")
            # ⚠️ 先反查发布状态、再抓数据(`refresh_all` 内部保证顺序) ——
            # 反查写的是看板「后台核对」列的依据(`published_url`),抓取只写指标。
            # 不做反查就会出现"有反馈数据、却标着未找到"的矛盾行(#45/#46 实测踩过)。
            from app.pusher.mp_api import MpSessionError

            try:
                out = refresh_all(only_due=True)
            except MpSessionError as e:        # 会话失效:如实报错,不假装成功
                detail = f"回流未完成:公众号会话不可用({str(e)[:60]})"
                run.log("backfill", "failed", detail)
                ps.mark("backfill", "failed", detail)
                return {**report, "ok": False, "error": detail,
                        "hint": "到「文章处理窗口」重新扫码登录后重试"}
            sync, result = out["sync"], out["stats"]
            report["sync_matched"] = sync.get("matched", 0)
            report["backfilled"] = result.get("backfilled", 0)
            report["skipped"] = result.get("skipped", [])
            report["failed"] = result.get("failed", [])
            if result.get("error"):            # 会话失效之类:如实报错,不假装成功
                report["error"] = result["error"]
                report["hint"] = result.get("hint", "")
                detail = f"回流未完成:{result['error'][:80]}"
                run.log("backfill", "failed", detail)
                ps.mark("backfill", "failed", detail)
                return {**report, "ok": False}
            detail = (f"回流 {report['backfilled']} 条"
                      + (f",同步发布状态 {report['sync_matched']} 篇" if report["sync_matched"] else "")
                      + (f",跳过 {len(report['skipped'])} 条(后台没有)" if report["skipped"] else ""))
            run.log("backfill", "success", detail)
            ps.mark("backfill", "success", detail)
            return {**report, "ok": True}
        # ---------- ⑥ 知识库清理(独立触发,不并入 all) ----------
        if stage == "clean":
            ps.mark("clean", "running", "清理知识库…")
            stats = clean_knowledge_base()
            report["cleaned"] = stats
            detail = f"清理 {sum(stats.values())} 条({stats})"
            run.log("clean", "success", detail)
            ps.mark("clean", "success", detail)
            return {**report, "ok": True}
        # all 一键流程在此结束(抓取→分析→生成,生成完停在 generated 等人工审核)
        return {**report, "ok": True}


def run_backfill() -> dict:
    """定时回流:先反查发布状态,再按 T+1/3/7 排期抓取。

    与手动「立即抓取最新数据」**走同一条路径**(`refresh_all`),避免两套口径 ——
    只抓取不反查会留下"有数据却标未找到"的矛盾行。

    ⚠️ 受配置 `backfill_enabled` 控制(以前那个开关没人读,关掉也照样跑);
    **只管自动路径** —— 手动按钮是明确的用户意图,不受它影响。
    """
    from app.core.config import get_config
    from app.pusher.mp_api import MpSessionError

    if get_config("backfill_enabled") is False:
        logger.info("回流已按配置关闭(backfill_enabled=False),本轮跳过")
        return {"checked": 0, "backfilled": 0, "skipped": [], "failed": [],
                "disabled": True, "note": "已在控制面板关闭「效果回流开关」"}

    try:
        out = refresh_all(only_due=True)
    except MpSessionError as e:      # 定时任务不该因为会话过期而抛栈中断
        result = {"checked": 0, "backfilled": 0, "skipped": [], "failed": [],
                  "error": f"公众号会话不可用:{e}",
                  "hint": "到「文章处理窗口」重新扫码登录后重试"}
        logger.warning("回流未执行: %s", result["error"])
        return result
    result = dict(out["stats"])
    result["sync_matched"] = out["sync"].get("matched", 0)
    # ⚠️ 不能写 `logger.info("…%s", result)` —— logging 会把**唯一的 dict 参数**当成
    # 命名格式化映射,于是报 "not all arguments converted"。这条日志以前从没跑过
    # (定时任务表是空的),所以这个坑一直藏着。这里显式取字段。
    logger.info("回流完成: 抓取 %s 篇,同步发布状态 %s 篇",
                result.get("backfilled", 0), result.get("sync_matched", 0))
    return result


def run_clean() -> dict:
    stats = clean_knowledge_base()
    logger.info("知识库清理完成: %s", stats)
    return stats


def register_pipeline_handlers() -> None:
    """把流水线环节注册进调度中心。"""
    register_stage("fetch", lambda: run_pipeline("fetch"))
    register_stage("analyze", lambda: run_pipeline("analyze"))
    register_stage("generate", lambda: run_pipeline("generate"))
    register_stage("push", lambda: run_pipeline("push"))
    register_stage("backfill", run_backfill)
    register_stage("clean", run_clean)
    register_stage("all", lambda: run_pipeline("all"))
    logger.info("流水线环节已注册: fetch/analyze/generate/push/backfill/clean")
