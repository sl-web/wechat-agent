"""模板管理(PRD 5.3.1):人工设计 JSON 模板入库,CRUD + 结构方案。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from app.core.db import connect
from app.core.logger import get_logger

logger = get_logger("templates")

# ⚠️ 内置模板的 id:生成流水线在「配置的模板不见了」时回落到它。
DEFAULT_TEMPLATE_ID = "tpl_ai_news_v1"


def _sections_sha(sections: object) -> str:
    """结构 sections 的稳定哈希 —— 用来判断这个结构用户有没有手工改过。"""
    payload = json.dumps(sections or [], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _with_seed_meta(definition: dict) -> dict:
    """复制一份模板定义,并记下每个结构的**默认 sections 哈希**。

    ⚠️ 只是内部标记(下划线开头),但会随 definition 一起入库 —— 前端模板编辑框是原始 JSON,
    所以用户能看到它。别删:删了下次 seed 就会把老库当"没标记"重新判断。
    """
    out = json.loads(json.dumps(definition, ensure_ascii=False))
    out["_seed_meta"] = {
        s["structure_id"]: _sections_sha(s.get("sections"))
        for s in out.get("structures", []) if isinstance(s, dict) and s.get("structure_id")
    }
    return out


def _merge_sections_additive(row_sections: object, default_sections: object) -> list:
    """只把默认里**新增的字段**补进用户已有的 sections,不改用户写过的文字。

    老库升级用:例如给 body 段补上 `min_len`/`max_len`(没有它,字数校验只能去猜描述里的
    数字,猜错过——见 validator._guess_len_range)。用户自己写的 desc 一个字都不动。
    """
    rows = [dict(s) for s in (row_sections or []) if isinstance(s, dict)]
    by_key = {s.get("key"): s for s in rows}
    for d in (default_sections or []):
        if not isinstance(d, dict):
            continue
        target = by_key.get(d.get("key"))
        if target is None:
            continue
        for k, v in d.items():
            target.setdefault(k, v)
    return rows


DEFAULT_TEMPLATE = {
    "template_id": DEFAULT_TEMPLATE_ID,
    "name": "AI 领域热点解读",
    "scene": "热点解析类",
    "structures": [
        {
            "structure_id": "s1",
            "name": "事件回顾式",
            "sections": [
                {"key": "title", "desc": "标题 20 字内,含核心关键词。可心里拟 3 个候选,但**只输出最优的那 1 个字符串**,不要输出数组"},
                {"key": "lead", "desc": "导语 80~120 字,点明热点+读者价值"},
                # ⚠️ 字数区间要**显式**写 min_len/max_len,别只靠描述里的数字:
                #    描述里的「段落 60~150 字」说的是段落、不是全文,正则分不清谁是谁。
                {"key": "body", "desc": "正文 1200~1800 字,分 3~4 小节(事件回顾 / 影响分析 / 观点)。每个小节**必须用 ## 起头并独占一行**;段落 60~150 字、段间留空行;关键判断用 **加粗**",
                 "min_len": 1200, "max_len": 1800},
                {"key": "quote", "desc": "金句 1 句,可引用行业观点"},
                {"key": "cta", "desc": "结尾引导 50~80 字,引导点赞/转发/关注"},
            ],
        },
        {
            "structure_id": "s2",
            "name": "盘点清单式",
            "sections": [
                {"key": "title", "desc": "标题,如《2026 值得关注的 5 大 AI 事件》"},
                {"key": "lead", "desc": "导语说明盘点背景"},
                # ⚠️ 描述里**必须先说全文多少字**,再说"每条"多少字 —— 旧版只写了
                #    「每条 150~250 字」,validator 正则抓到 150~250 当成全文区间
                #    (窗口 120~300),于是 1500 字的正文永远判不合格,而且模型为了凑数
                #    越改越短(实测 1552→1267→914 字),最后熔断。两处都修了。
                {"key": "body", "desc": "正文 1200~1800 字,按条目盘点,每条 150~250 字。**每条必须用 ## 起头并独占一行**;段落 60~150 字、段间留空行",
                 "min_len": 1200, "max_len": 1800},
                {"key": "cta", "desc": "结尾引导互动"},
            ],
        },
        {
            "structure_id": "s3",
            "name": "论点驱动式(有观点)",
            # 该结构强制表态:prompt 会据此加强论证要求,validator 会查论点与反综述
            "must_take_stance": True,
            "sections": [
                {"key": "title", "desc": "标题 20 字内,含核心关键词并带判断倾向(读者一眼看出你站在哪边)。可在心里拟 3 个候选,但**只输出最优的那 1 个字符串**,不要输出数组"},
                {"key": "lead", "desc": "导语 100~150 字:开头用一个反常的细节、数字或反差抓住读者,结尾点明本文要挑战的流行看法"},
                {"key": "thesis", "desc": "核心论点 1~2 句(40~80 字):必须是一个明确、可被反驳的判断。禁止写「值得关注」「影响深远」这类没有立场的空话"},
                {"key": "body", "desc": "正文 1200~1800 字,分 4 个小节:三条论据(每条先给判断,再引用素材中的具体事实或数字支撑) / 主流看法是什么、我为什么不同意(必须正面写出反方观点) / 真正的变量在哪(指出被忽略的关键因素) / 结论(会怎样、什么条件下会变)。**每个小节必须用 ## 起头并独占一行,不要把 ①②③ 这类序号写在段落开头**;段落 60~150 字、段间留空行;每节至少一处 **加粗** 的判断句",
                 "min_len": 1200, "max_len": 1800},
                {"key": "quote", "desc": "金句 1 句(40 字内):把核心判断压缩成一句可被引用、也能被反驳、且**脱离上下文也读得懂**的话 —— 读者转发时往往只带这一句"},
                {"key": "cta", "desc": "结尾 50~80 字:给出**转发理由**(转出去对读者有什么用)+ **关注理由**(关注能持续拿到什么,要具体),并用一个读者答得上来的具体问题引导留言/反论"},
            ],
        },
    ],
    "style": {
        "tone": "专业、有判断,不绕弯子",
        "persona": "科技观察者",  # 实际运行时由配置 writer_persona 覆盖
        # 用词组而非单字:单个"最"会把"最关键""最重要的变量"这类正常分析表达全部误杀
        "ban_words": ["最好", "最佳", "最优", "最强", "第一品牌", "国家级",
                      "世界级", "顶级", "万能", "100%", "绝对"],
    },
}


def seed_default_template(db_path: str | None = None) -> str:
    """首次初始化写入默认模板;已存在则「补齐缺的结构 + 同步用户没改过的结构」。

    ⚠️⚠️ **绝不能覆盖用户手工改过的结构**。
    旧实现一发现同名结构的 sections 与默认不一致,就用默认覆盖它 —— 本意是让"默认模板
    的结构描述升级"能落到老库,结果把**用户在「模板库」里改的模板也一起回滚了**:
    而且 `pipeline.py` 在**每次生成前**都会调一次本函数,表现就是
    「模板保存成功、一生成又变回原样」(实测踩到)。

    现在用 sections 哈希区分三种情况(哈希记在 definition 的 `_seed_meta` 里):
      · 与"上次同步的默认"一致 ⇒ 用户没动过 ⇒ 用新默认覆盖(默认升级照常生效);
      · 与"上次同步的默认"不一致 ⇒ 用户改过 ⇒ **保留用户版本,永不覆盖**;
      · 老库没有 `_seed_meta`(本次升级之前入库的)⇒ 只做**补字段**合并
        (例如给 body 补 min_len/max_len),用户写过的文字一个字都不动;
        合并后如果与默认完全一致,才登记哈希、以后恢复正常同步。
    """
    own = db_path is not None
    conn = connect(db_path) if db_path else connect()
    try:
        tid = DEFAULT_TEMPLATE_ID
        row = conn.execute("SELECT definition FROM templates WHERE id=?", (tid,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO templates(id, name, definition) VALUES(?,?,?)",
                (tid, DEFAULT_TEMPLATE["name"],
                 json.dumps(_with_seed_meta(DEFAULT_TEMPLATE), ensure_ascii=False)),
            )
            conn.commit()
            return tid

        try:
            cur = json.loads(row["definition"])
        except Exception:  # noqa: BLE001
            cur = json.loads(json.dumps(DEFAULT_TEMPLATE))
        structs = cur.setdefault("structures", [])
        by_id = {s.get("structure_id"): s for s in structs if isinstance(s, dict)}
        meta = cur.setdefault("_seed_meta", {})
        changed = False

        for s in DEFAULT_TEMPLATE["structures"]:
            sid = s["structure_id"]
            fresh = json.loads(json.dumps(s, ensure_ascii=False))  # 深拷贝,别把标记写回 DEFAULT
            fresh_sha = _sections_sha(fresh.get("sections"))
            row_struct = by_id.get(sid)
            row_sections = row_struct.get("sections") if row_struct else None

            if row_struct is None:
                structs.append(fresh)          # 新结构:直接加(旧行为,保留)
                meta[sid] = fresh_sha
                changed = True
                continue

            known = meta.get(sid)              # 上次同步过的"默认"长什么样
            if known is None:
                merged = _merge_sections_additive(row_sections, fresh.get("sections"))
                if merged != row_sections:
                    row_struct["sections"] = merged
                    changed = True
                if _sections_sha(row_struct.get("sections")) == fresh_sha:
                    meta[sid] = fresh_sha      # 与默认一致 ⇒ 以后可以正常同步
                else:
                    logger.info("模板 %s 的结构 %s 与默认不同(人工改过),跳过同步、保留用户版本", tid, sid)
                continue

            if _sections_sha(row_sections) == known:
                # 用户没动过 ⇒ 同步默认
                if row_sections != fresh.get("sections"):
                    row_struct["sections"] = fresh.get("sections")
                    changed = True
                if row_struct.get("name") != fresh.get("name"):
                    row_struct["name"] = fresh["name"]
                    changed = True
                if ("must_take_stance" in fresh
                        and row_struct.get("must_take_stance") != fresh["must_take_stance"]):
                    row_struct["must_take_stance"] = fresh["must_take_stance"]
                    changed = True
                if meta.get(sid) != fresh_sha:
                    meta[sid] = fresh_sha
                    changed = True
            else:
                logger.info("模板 %s 的结构 %s 是人工改过的,跳过默认同步(保留用户版本)", tid, sid)

        if changed:
            conn.execute(
                "UPDATE templates SET definition=? WHERE id=?",
                (json.dumps(cur, ensure_ascii=False), tid),
            )
            conn.commit()
        return tid
    finally:
        conn.close()


def sync_default_ban_words(db_path: str | None = None) -> bool:
    """把内置模板的 `style.ban_words` 同步成代码里的默认值(幂等,只改这一个键)。

    为什么需要单独同步:`seed_default_template` 只合并 structures、**不动 style**,
    所以**改了默认违禁词表之后,老库永远带着旧表**。真实踩过:
    库里还是最早期那份单字表 `['绝对','最','第一']`,而 validator 用的是词组表 ——
    prompt 让模型避开 A、validator 按 B 判罚,两套口径,怎么改都可能不过。

    只处理**已存在**的内置模板(不新建),用户自建模板不受影响。返回是否发生改动。
    """
    own = db_path is not None
    conn = connect(db_path) if db_path else connect()
    try:
        tid = DEFAULT_TEMPLATE["template_id"]
        row = conn.execute("SELECT definition FROM templates WHERE id=?", (tid,)).fetchone()
        if not row:
            return False
        try:
            cur = json.loads(row["definition"])
        except Exception:  # noqa: BLE001
            return False
        want = (DEFAULT_TEMPLATE.get("style") or {}).get("ban_words")
        if not isinstance(want, list):
            return False
        if (cur.get("style") or {}).get("ban_words") == want:
            return False
        cur.setdefault("style", {})["ban_words"] = want
        conn.execute("UPDATE templates SET definition=? WHERE id=?",
                     (json.dumps(cur, ensure_ascii=False), tid))
        conn.commit()
        return True
    finally:
        conn.close()


def resolve_structure(template: dict | None, structure_id: str) -> str:
    """在**这个模板**里挑一个可用的 structure_id(不属于它就回落到第一个)。

    为什么需要:模板与结构是两个配置项 —— 用户在「模板库」里换了模板,却不一定
    跟着改「默认文章结构」;拿一个该模板里不存在的 s1/s2/s3 去生成会直接失败。
    实测踩过:改成自定义模板后报"结构不存在",而日志里看不出是配置没配齐。
    """
    structs = ((template or {}).get("definition") or {}).get("structures") or []
    ids = [s.get("structure_id") for s in structs if isinstance(s, dict)]
    if structure_id and structure_id in ids:
        return structure_id
    return ids[0] if ids else (structure_id or "s1")


def list_templates(enabled_only: bool = True, db_path: str | None = None) -> list[dict]:
    conn = connect(db_path) if db_path else connect()
    try:
        sql = "SELECT id, name, definition, enabled, updated_at FROM templates"
        if enabled_only:
            sql += " WHERE enabled=1"
        rows = conn.execute(sql).fetchall()
        out = []
        for r in rows:
            d = {"id": r["id"], "name": r["name"], "enabled": r["enabled"],
                 "updated_at": r["updated_at"]}
            try:
                d["definition"] = json.loads(r["definition"])
            except json.JSONDecodeError:
                d["definition"] = {}
            out.append(d)
        return out
    finally:
        conn.close()


def get_template(template_id: str, db_path: str | None = None) -> dict | None:
    conn = connect(db_path) if db_path else connect()
    try:
        row = conn.execute(
            "SELECT id, name, definition FROM templates WHERE id=?", (template_id,)
        ).fetchone()
        if not row:
            return None
        return {"id": row["id"], "name": row["name"],
                "definition": json.loads(row["definition"])}
    finally:
        conn.close()


def save_template(definition: dict, template_id: str | None = None, db_path: str | None = None) -> str:
    """新增或覆盖模板。template_id 省略时用 definition.template_id。

    ⚠️ 覆盖时必须把 `enabled` 也复位成 1。旧实现只有 INSERT 分支写 enabled=1,
    ON CONFLICT 分支不碰它 —— 于是「先停用、再编辑保存」的模板会停在 enabled=0,
    在面板上直接消失(看起来就是"点了保存其实没保存")。
    语义上「保存」=「这是我要用的模板」,与 INSERT 分支保持一致。
    """
    tid = template_id or definition.get("template_id")
    if not tid:
        raise ValueError("缺少 template_id")
    own = db_path is not None
    conn = connect(db_path) if db_path else connect()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO templates(id, name, definition, enabled, updated_at) VALUES(?,?,?,1,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                 definition=excluded.definition, enabled=1,
                 updated_at=excluded.updated_at""",
            (tid, definition.get("name", tid), json.dumps(definition, ensure_ascii=False), now),
        )
        conn.commit()
        return tid
    finally:
        conn.close()


def toggle_template(template_id: str, enabled: bool, db_path: str | None = None) -> None:
    conn = connect(db_path) if db_path else connect()
    try:
        conn.execute(
            "UPDATE templates SET enabled=?, updated_at=datetime('now','localtime') WHERE id=?",
            (1 if enabled else 0, template_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_template(template_id: str, db_path: str | None = None) -> None:
    conn = connect(db_path) if db_path else connect()
    try:
        conn.execute("DELETE FROM templates WHERE id=?", (template_id,))
        conn.commit()
    finally:
        conn.close()
