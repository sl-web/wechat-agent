"""生成编排与熔断集成(PRD 5.3.3):生成 → 校验 → 重试(≤3)→ 兜底 → 黑名单。

为保证在无 LLM Key 环境下可测,支持注入 mock 生成器。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

from app.core.checkpoint import RunRecorder
from app.core.circuit_breaker import CircuitBreaker, HotspotBlacklist
from app.core.db import connect, execute
from app.core.logger import get_logger
from app.generator.llm import LLMClient
from app.generator.prompt import assemble_prompt
from app.generator.validator import Validator

logger = get_logger("generate")


class GenerationError(RuntimeError):
    pass


def _extract_json_object(text: str) -> str:
    """从任意文本中提取最可能的 JSON 对象子串。"""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


_MD_FIELD_KEYS = {
    "title": ("候选标题", "标题", "title"),
    "lead": ("导语", "lead"),
    "quote": ("金句", "quote"),
    "cta": ("结尾", "cta", "引导"),
    "body": ("正文", "body"),
}


def _markdown_to_sections(text: str) -> dict:
    """Markdown 章节容错:逐行扫描带 **加粗** / # 标题 的章节标记。

    LLM 若未按 JSON 输出(如返回 **导语** xxx 排版文本),尽力还原字段。
    body 会一直累积到下一个章节标记为止。
    """
    out: dict = {}
    lines = text.splitlines()
    cur_key = None
    buf: list[str] = []

    def flush():
        nonlocal buf, cur_key
        if cur_key and buf:
            chunk = "".join(buf).strip()
            if chunk:
                if cur_key == "title":
                    # 候选标题取第一行
                    chunk = chunk.splitlines()[0].strip().lstrip("*# ")
                    chunk = re.sub(r"^\d+[.、]\s*", "", chunk)
                out.setdefault(cur_key, chunk)
        buf = []
        cur_key = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if cur_key == "body":
                buf.append("\n")  # 保留 body 内分段空行
            continue
        # 识别章节标记:去掉加粗符号/标题符号后以字段名开头(或整行即为标记名)
        core = stripped.replace("*", "").replace("#", "").strip().lstrip()
        matched = None
        for key, names in _MD_FIELD_KEYS.items():
            for nm in names:
                if core == nm:  # 整行即标记(如 "**候选标题**")
                    matched = (key, "")
                    break
                if core.startswith(nm) and core[len(nm)] in (":", "：", " ", "\t"):
                    matched = (key, core[len(nm):].lstrip(":： \t"))
                    break
            if matched:
                break
        if matched:
            flush()
            key, rest = matched
            cur_key = key
            if rest:
                buf.append(rest)
        else:
            # 无标记行:归属当前 body 或丢弃
            if cur_key:
                buf.append(stripped)
    flush()
    return out


def _parse_raw(content: str) -> dict:
    """解析 LLM 输出为结构化 dict。

    顺序:去码围栏 → 整体 JSON → 提取子串 JSON → Markdown 章节容错。
    """
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.I)
        text = re.sub(r"```$", "", text).strip()
    # 1) 整体即 JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 2) 内嵌 JSON 对象(前后有解释文字)
    sub = _extract_json_object(text)
    if sub:
        try:
            obj = json.loads(sub)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    # 3) Markdown 容错
    md = _markdown_to_sections(text)
    if md:
        return md
    raise GenerationError("LLM 输出无法解析为结构化内容(非 JSON 且无可用章节)")


def _coerce_fields(obj: dict) -> dict:
    """把 LLM 可能返回的非字符串值统一成字符串。

    真实踩过:结构描述里写了"标题生成 3 个候选",模型就真的把 title 返回成**数组**,
    于是违禁词检查里的 `title + body` 和落库直接 TypeError: list + str,
    整篇生成白跑。这里是最后一道防线 —— 不管模型返回什么类型,下游拿到的都是 str。
    """
    for k, v in list(obj.items()):
        if isinstance(v, list):
            parts = [str(x).strip() for x in v if str(x).strip()]
            # 标题/论点/金句只取最优的一条,其余字段按行拼接
            if k in ("title", "thesis", "quote") and parts:
                obj[k] = parts[0]
            else:
                obj[k] = "\n".join(parts)
        elif isinstance(v, (int, float)):
            obj[k] = str(v)
        elif v is None:
            obj[k] = ""
    return obj


def _parse_llm_output(content: str) -> dict:
    """解析 LLM 输出并规范化字段类型(所有调用方都从这一层拿结果)。"""
    return _coerce_fields(_parse_raw(content))


def _normalize_topic(topic: dict) -> dict:
    """把选题统一成含 id/title/url/source/summary 的形状。

    选题有两个来源,结构不同:
    - 热点库行:{id, title, url, source, summary, heat_score}
    - 分析环节的聚类主题:{topic, hot_count, summary, sample, members} —— **没有 title/url**。
    后者若不归一化,生成时 prompt 的【写作主题】与素材会全为空(真实踩过的坑)。
    """
    if topic.get("title"):
        return topic
    sample = topic.get("sample") or {}
    if not sample and topic.get("members"):
        sample = topic["members"][0]
    return {
        "id": topic.get("id") or sample.get("id"),
        "title": sample.get("title") or topic.get("topic", ""),
        "url": sample.get("url", ""),
        "source": sample.get("source", ""),
        "summary": sample.get("summary") or "",
        "heat_score": topic.get("heat_sum") or sample.get("heat") or sample.get("heat_score"),
    }


def _material_from_refs(refs: dict) -> str:
    """把「分析归纳背景 + 主选题 + 聚类成员」拼成喂给 LLM 的素材。

    与溯源记录一一对应:记录里写了哪些参考,素材里就用了哪些,便于事后复核。
    """
    main = refs.get("main_topic") or {}
    rel = refs.get("related_topics") or []
    ana = refs.get("analysis") or {}
    if not main.get("title"):
        return ""
    lines: list[str] = []
    if ana.get("cluster_topic"):
        lines.append("【分析归纳背景】")
        lines.append(
            f"所属主题: {ana.get('cluster_topic')}"
            f"(该主题聚合 {ana.get('cluster_count', 0)} 条报道)"
        )
        if ana.get("suggested_angle"):
            lines.append(f"建议角度: {ana.get('suggested_angle')}")
        if ana.get("cluster_summary"):
            lines.append(f"聚类摘要: {ana.get('cluster_summary')}")
        lines.append("")
    lines += [
        "【主选题】",
        f"标题: {main.get('title', '')}",
        f"摘要: {main.get('summary', '')}",
        f"原文链接: {main.get('url', '')}",
        f"来源: {main.get('source', '')}",
    ]
    if rel:
        lines.append("")
        lines.append(
            f"【同主题相关报道 {len(rel)} 条(来自分析归纳环节,可作交叉印证;不是事实扩展许可)】"
        )
        for i, r in enumerate(rel[:8], 1):
            lines.append(f"{i}. {r.get('title', '')}")
            if r.get("summary"):
                lines.append(f"   摘要: {r.get('summary', '')}")
            if r.get("url"):
                lines.append(f"   链接: {r.get('url', '')}")
    return "\n".join(lines)


def build_default_refs(
    topic: dict,
    template_id: str,
    structure_id: str,
    related: list[dict] | None = None,
    analysis: dict | None = None,
) -> dict:
    """生成「参考信息来源」溯源记录:回答"这篇文章是依据哪些信息写出来的"。

    - main_topic: 直接作为选题的那条热点
    - related_topics: 同一聚类下的其它相关报道(分析环节产出)
    - analysis: 所属分析报告的 run_id / 主题名 / 聚类条数
    - template: 使用的模板与结构
    - material: 实际喂给 LLM 的素材原文(与 prompt 一致,便于复核)
    """
    t = _normalize_topic(topic)
    main = {
        "id": t.get("id"),
        "title": t.get("title", ""),
        "url": t.get("url", ""),
        "source": t.get("source", ""),
        "summary": (t.get("summary") or "")[:300],
        "heat_score": t.get("heat_score"),
    }
    rel = []
    tid = t.get("id")
    main_title = main["title"]
    for r in related or []:
        # 主选题不重复列入参考列表(按 id 或标题判定)
        if (tid is not None and r.get("id") == tid) or r.get("title") == main_title:
            continue
        rel.append({
            "id": r.get("id"),
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "source": r.get("source", ""),
            "summary": (r.get("summary") or "")[:200],
        })
    refs = {
        "main_topic": main,
        "related_topics": rel[:12],
        "analysis": analysis or {},
        "template": {"template_id": template_id, "structure_id": structure_id},
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "run_id": "",
    }
    refs["material"] = _material_from_refs(refs)
    return refs


def generate_article(
    topic: dict,
    template_id: str,
    structure_id: str,
    run_id: str,
    llm: LLMClient | None = None,
    db_path: str | None = None,
    refs: dict | None = None,
    chosen_thesis: str = "",
    chosen_tension: str = "",
    article_type: str = "",
) -> dict:
    """生成单篇稿件。返回文章记录 dict。

    refs: 生成参考溯源信息;未传时自动按 topic 构建(见 build_default_refs)。
    topic 无论来自热点库还是分析报告的聚类主题,都会先归一化再进 prompt。

    `chosen_thesis` / `chosen_tension` / `article_type`:两步生成里用户选定的
    论点、张力与论证姿态。只影响这一篇(不写回全局配置),三者都会变成 prompt 里的**硬约束**。
    """
    topic = _normalize_topic(topic)
    run = RunRecorder(run_id, db_path)
    llm = llm or LLMClient()
    # 熔断重试上限**来自配置**(以前写死 3,面板上那个「熔断重试上限」形同虚设)。
    # ⚠️ 从**本实例的库**读:带 db_path 跑(测试/隔离环境)时不能读到默认库的设置。
    from app.core.config import get_config as _get_config

    _conn = connect(db_path)
    try:
        try:
            _limit = int(_get_config("retry_limit", _conn) or 3)
        except (TypeError, ValueError):
            _limit = 3
    finally:
        _conn.close()
    cb = CircuitBreaker(name=f"gen_{topic.get('id', 'topic')}", limit=max(1, _limit))
    validator = Validator(template_id, structure_id, db_path=db_path)
    fail_count = 0
    refs = refs or build_default_refs(topic, template_id, structure_id)
    refs["run_id"] = run_id
    if chosen_thesis.strip():
        refs["chosen_thesis"] = chosen_thesis.strip()
    if chosen_tension.strip():
        refs["chosen_tension"] = chosen_tension.strip()
    if article_type:
        refs["article_type"] = article_type
    # 素材与溯源同源:记录里列了哪些参考,就真的喂给 LLM 哪些,避免"记录了却没用"
    extra_material = refs.get("material") or ""
    refs["material"] = extra_material
    refs_json = json.dumps(refs, ensure_ascii=False)

    try:
        # 上一稿没过审的原因 —— 重试时回灌给模型,让它**针对性改**而不是盲重写一遍。
        # 盲重试的代价很高:每次都是一整次 LLM 生成(实测 150 秒以上),
        # 而模型并不知道上次为什么挂,只能靠温度随机性撞过去。
        last_issues: list[str] = []
        while not cb.is_open:
            sys_p, usr_p = assemble_prompt(
                template_id, structure_id, topic, extra_material, db_path=db_path,
                # 用户选定的论证姿态只影响这一篇,不写回全局配置
                overrides={"article_type": article_type} if article_type else None,
                chosen_thesis=chosen_thesis,
                chosen_tension=chosen_tension,
                retry_issues=last_issues,
            )
            content = llm.generate(sys_p, usr_p)
            try:
                result = _parse_llm_output(content)
            except GenerationError as exc:
                last_issues = [f"上一稿输出不是合法 JSON({str(exc)[:80]});"
                               "请严格按输出格式只输出一个 JSON 对象,不要任何解释文字"]
                fail_count += 1
                cb.record_failure()
                continue

            v = validator.validate(result)
            if v["ok"]:
                # 落库
                conn = connect(db_path) if db_path else connect()
                try:
                    aid = execute(
                        conn,
                        """INSERT INTO articles(topic_id, template_id, structure_id,
                           title, lead, thesis, body, quote, cta, status, fact_notes, source_refs)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            topic.get("id"), template_id, structure_id,
                            result.get("title", ""), result.get("lead", ""),
                            result.get("thesis", ""),          # 论点驱动结构的核心论点
                            result.get("body", ""), result.get("quote", ""),
                            result.get("cta", ""), "generated",
                            json.dumps(v["fact_notes"], ensure_ascii=False),
                            refs_json,
                        ),
                    )
                    conn.close()
                finally:
                    pass
                run.log("generate", "success", f"文章 #{aid} 生成成功")
                return {"id": aid, **result, "fact_notes": v["fact_notes"], "ok": True}

            fail_count += 1
            cb.record_failure()
            # 记录不合格原因 —— 否则熔断后只看到一句"已熔断",无从改进提示词或护栏
            detail = "; ".join(v.get("issues") or [])[:320]
            last_issues = list(v.get("issues") or [])       # 下一次生成时回灌
            logger.warning("第 %d 次校验未通过: %s", fail_count, detail)
            run.log("generate", "checkpoint", f"第 {fail_count} 次校验未通过:{detail}")
            # ⚠️ 这里**不要**再写死一个次数上限:重试上限的唯一来源是配置 `retry_limit`
            # (经上面的 CircuitBreaker)。曾经这里多了一句 `if fail_count >= 3: break`,
            # 于是把 retry_limit 调到 5 也没用 —— 两个东西管同一件事,配置被架空。

        # 熔断兜底:记录失败
        conn = connect(db_path) if db_path else connect()
        aid = execute(
            conn,
            """INSERT INTO articles(topic_id, template_id, structure_id, title, status,
               reject_reason, fail_count, source_refs) VALUES(?,?,?,?,?,?,?,?)""",
            (
                topic.get("id"), template_id, structure_id,
                topic.get("title", ""), "generation_failed",
                "连续生成校验失败,触发熔断兜底", fail_count, refs_json,
            ),
        )
        conn.close()
        run.log("generate", "failed", f"熔断兜底,文章 #{aid} 生成失败")
        raise GenerationError(f"生成失败(已熔断): topic={topic.get('title')}")
    finally:
        run.close()


def generate_with_retry_and_blacklist(
    topic: dict,
    template_id: str,
    structure_id: str,
    run_id: str,
    blacklist: HotspotBlacklist,
    llm: LLMClient | None = None,
    db_path: str | None = None,
    refs: dict | None = None,
) -> dict | None:
    """带热点黑名单的生成入口:黑名单内热点直接跳过。"""
    topic_id = topic.get("id")
    if topic_id and blacklist.is_blacklisted(int(topic_id)):
        logger.warning("热点 %s 已在黑名单,跳过", topic_id)
        return None
    try:
        return generate_article(topic, template_id, structure_id, run_id, llm, db_path, refs)
    except GenerationError as e:
        if topic_id:
            blacklist.record_failure(int(topic_id))
        logger.error("生成失败: %s", e)
        return None
