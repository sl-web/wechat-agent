"""生成参考溯源测试。

背景:分析报告的主题结构 {topic, hot_count, summary, sample, members} 没有 title/url,
若直接进生成 prompt,会导致【写作主题】与素材全空。这里锁定归一化与溯源行为。
"""
from __future__ import annotations

import json

import pytest

from app.core.db import connect, init_db, query
from app.generator.llm import LLMClient
from app.generator.service import (
    _material_from_refs,
    _normalize_topic,
    build_default_refs,
    generate_article,
)
from app.generator.templates import seed_default_template
from app.pipeline import _build_gen_refs
from app.review.service import _count_refs


CLUSTER_TOPIC = {
    "topic": "AI",
    "hot_count": 34,
    "heat_sum": 0.38,
    "summary": "该主题聚合 34 条相关报道,报道要点包括……",
    "suggested_angle": "围绕「AI」展开事件回顾与影响解读",
    "sample": {
        "id": 67, "title": "腾讯调整混元 AI 模型限免权益",
        "url": "https://x/1", "source": "rss", "summary": "腾讯宣布调整……",
    },
    "members": [
        {"id": 67, "title": "腾讯调整混元 AI 模型限免权益", "url": "https://x/1",
         "source": "rss", "summary": "腾讯宣布调整……", "heat": 0.9},
        {"id": 30, "title": "商务部回应 AI 蒸馏指控", "url": "https://x/2",
         "source": "rss", "summary": "商务部发言人表示……", "heat": 0.8},
    ],
}

HOT_TOPIC = {
    "id": 30, "title": "商务部回应 AI 蒸馏指控", "url": "https://x/2",
    "source": "rss", "summary": "商务部发言人表示……", "heat_score": 0.8,
}


# ---------- 选题归一化 ----------

def test_normalize_cluster_topic_fills_title_and_url():
    """聚类主题必须能解析出真实标题/链接,否则 prompt 会空。"""
    t = _normalize_topic(CLUSTER_TOPIC)
    assert t["title"] == "腾讯调整混元 AI 模型限免权益"
    assert t["url"] == "https://x/1"
    assert t["id"] == 67
    assert t["summary"]


def test_normalize_hot_topic_is_passthrough():
    assert _normalize_topic(HOT_TOPIC) is HOT_TOPIC


def test_normalize_cluster_topic_without_sample_uses_first_member():
    t = _normalize_topic({"topic": "芯片", "members": [{"id": 5, "title": "某芯片新闻"}]})
    assert t["title"] == "某芯片新闻"
    assert t["id"] == 5


# ---------- 溯源记录 ----------

def test_refs_contain_main_related_analysis_template():
    refs = _build_gen_refs(CLUSTER_TOPIC, "tpl_ai_news_v1", "s1", "run-1")
    assert refs["main_topic"]["title"]
    assert refs["main_topic"]["url"]
    # 主选题不重复出现在参考列表里
    titles = [r["title"] for r in refs["related_topics"]]
    assert "腾讯调整混元 AI 模型限免权益" not in titles
    assert "商务部回应 AI 蒸馏指控" in titles
    assert refs["analysis"]["cluster_topic"] == "AI"
    assert refs["analysis"]["run_id"] == "run-1"
    assert refs["template"] == {"template_id": "tpl_ai_news_v1", "structure_id": "s1"}


def test_refs_material_mentions_analysis_background():
    """素材里要显式给出分析报告背景,让"参考了什么"可核对。"""
    refs = _build_gen_refs(CLUSTER_TOPIC, "tpl_ai_news_v1", "s1", "run-1")
    m = refs["material"]
    assert "【分析归纳背景】" in m
    assert "【主选题】" in m
    assert "同主题相关报道" in m
    assert "https://x/1" in m


def test_refs_material_is_what_gets_fed_to_llm():
    """refs 里的 material 就是喂给 LLM 的素材(记录与使用同源)。"""
    refs = build_default_refs(HOT_TOPIC, "tpl", "s1")
    assert refs["material"] == _material_from_refs(refs)
    assert HOT_TOPIC["title"] in refs["material"]


def test_refs_empty_when_topic_unresolvable():
    """标题无法解析时不产出假素材,避免污染 prompt。"""
    assert _material_from_refs({"main_topic": {}}) == ""


@pytest.mark.parametrize("n", [0, 3, 20])
def test_related_topics_capped_at_twelve(n):
    members = [{"id": i + 100, "title": f"报道{i}"} for i in range(n)]
    refs = build_default_refs(HOT_TOPIC, "tpl", "s1", related=members)
    assert len(refs["related_topics"]) <= 12


# ---------- 参考条数统计(文章库列表用) ----------

def test_count_refs():
    refs = build_default_refs(HOT_TOPIC, "tpl", "s1",
                              related=[{"id": 1, "title": "A"}, {"id": 2, "title": "B"}])
    assert _count_refs(json.dumps(refs, ensure_ascii=False)) == 3  # 主选题 + 2 条相关
    assert _count_refs(None) == 0
    assert _count_refs("") == 0
    assert _count_refs("not-json") == 0


# ---------- 端到端:溯源随文章落库 ----------

class _MockLLM(LLMClient):
    def __init__(self):
        pass

    def generate(self, system_prompt, user_prompt, **kw):
        return json.dumps({
            "title": "测试文章标题",
            "lead": "导语" * 60,
            "body": "据公开报道,正文内容再补充一些细节叙述展开细节与影响分析" * 35,
            "quote": "引用金句",
            "cta": "关注我们" * 20,
        }, ensure_ascii=False)


def _prepare_db(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    seed_default_template(str(db))
    conn = connect(db)
    tid = conn.execute(
        """INSERT INTO hot_topics(source, fetch_mode, title, url, fetch_date)
           VALUES('rss','rss','主选题标题','http://x','2026-09-10')"""
    ).lastrowid
    conn.commit()
    conn.close()
    return db, tid


def test_generate_persists_source_refs(tmp_path):
    """生成成功后,source_refs 必须落库且内容可核对。"""
    db, tid = _prepare_db(tmp_path)
    refs = build_default_refs(
        {"id": tid, "title": "主选题标题", "url": "http://x", "source": "rss", "summary": "摘要"},
        "tpl_ai_news_v1", "s1",
        related=[{"id": 9999, "title": "相关报道", "url": "http://y", "source": "rss", "summary": "摘要2"}],
        analysis={"cluster_topic": "AI", "cluster_count": 5, "run_id": "run-1"},
    )
    r = generate_article(
        {"id": tid, "title": "主选题标题", "url": "http://x", "source": "rss", "summary": "摘要"},
        "tpl_ai_news_v1", "s1", "run-1", llm=_MockLLM(), db_path=str(db), refs=refs,
    )
    conn = connect(db)
    row = query(conn, "SELECT source_refs FROM articles WHERE id=?", (r["id"],))[0]
    conn.close()
    saved = json.loads(row["source_refs"])
    assert saved["main_topic"]["title"] == "主选题标题"
    assert [x["title"] for x in saved["related_topics"]] == ["相关报道"]
    assert saved["analysis"]["cluster_topic"] == "AI"
    assert saved["analysis"]["run_id"] == "run-1"
    assert "【分析归纳背景】" in saved["material"]


def test_chosen_tension_persisted_into_source_refs(tmp_path):
    """人工选定的张力必须与论点一样写进 source_refs —— 否则事后无法核对"这篇是围绕什么写的"。"""
    db, tid = _prepare_db(tmp_path)
    tension = "对外讲的是技术普惠,但计费规则把推理成本推给了付费用户"
    # 用 s1(不强制表态)即可:本测试关心的是 refs 落库,不是护栏
    r = generate_article(
        {"id": tid, "title": "主选题标题", "url": "http://x", "source": "rss", "summary": "摘要"},
        "tpl_ai_news_v1", "s1", "run-t", llm=_MockLLM(), db_path=str(db),
        chosen_thesis="这是一次成本转嫁。", chosen_tension=tension,
    )
    conn = connect(db)
    row = query(conn, "SELECT source_refs FROM articles WHERE id=?", (r["id"],))[0]
    conn.close()
    saved = json.loads(row["source_refs"])
    assert saved["chosen_tension"] == tension
    assert saved["chosen_thesis"] == "这是一次成本转嫁。"


def test_no_chosen_tension_leaves_no_key(tmp_path):
    """没选张力就不要写这个字段 —— 空字段会让溯源看起来"选过但没内容"。"""
    db, tid = _prepare_db(tmp_path)
    r = generate_article(
        {"id": tid, "title": "主选题标题", "url": "http://x", "source": "rss", "summary": "摘要"},
        "tpl_ai_news_v1", "s1", "run-t2", llm=_MockLLM(), db_path=str(db),
    )
    conn = connect(db)
    row = query(conn, "SELECT source_refs FROM articles WHERE id=?", (r["id"],))[0]
    conn.close()
    assert "chosen_tension" not in json.loads(row["source_refs"])


def test_generate_from_cluster_topic_uses_sample(tmp_path):
    """选题来自分析报告的聚类主题时,也要能产出非空素材并落到真实热点 id。"""
    db, tid = _prepare_db(tmp_path)
    cluster = {
        "topic": "AI", "hot_count": 3, "heat_sum": 0.5,
        "summary": "聚类摘要文本",
        "suggested_angle": "围绕「AI」展开",
        "sample": {"id": tid, "title": "主选题标题", "url": "http://x",
                   "source": "rss", "summary": "代表报道摘要"},
        "members": [{"id": tid, "title": "主选题标题", "url": "http://x",
                     "source": "rss", "summary": "代表报道摘要", "heat": 0.9}],
    }
    r = generate_article(cluster, "tpl_ai_news_v1", "s1", "run-2",
                         llm=_MockLLM(), db_path=str(db))
    conn = connect(db)
    row = query(conn, "SELECT source_refs, topic_id FROM articles WHERE id=?", (r["id"],))[0]
    conn.close()
    assert row["topic_id"] == tid  # 归一化后指向真实热点,不再是 NULL
    saved = json.loads(row["source_refs"])
    assert saved["main_topic"]["title"] == "主选题标题"
    assert "主选题标题" in saved["material"]
