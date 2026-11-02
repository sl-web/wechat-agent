"""采集器基类与入库测试。"""
from __future__ import annotations

from pathlib import Path

from app.collectors.base import BaseCollector, BaseHotTopic
from app.collectors.storage import save_hot_topics, soft_delete_topic
from app.core.db import connect, init_db, query


class FakeCollector(BaseCollector):
    source = "fake"

    def fetch(self, mode: str = "auto"):
        return [
            BaseHotTopic(source="fake", title="大模型新发布引发关注", url="http://a.com/1", platform_hot=900, summary="某厂商发布新模型"),
            BaseHotTopic(source="fake", title="大模型新发布引发关注", url="http://a.com/1", platform_hot=900, summary="重复标题"),
            BaseHotTopic(source="fake", title="今天天气不错", url="http://a.com/2", platform_hot=500, summary="与AI无关"),
        ]


def test_base_collector_dedup_and_filter():
    c = FakeCollector(keywords=["大模型"], top_n=10)
    items = c.collect()
    # 去重后剩 2 条,关键词过滤后剩 1 条(AI 相关)
    assert len(items) == 1
    assert items[0]["title"].startswith("大模型")
    assert "url" in items[0]
    assert "expired_at" in items[0]  # 合规字段


def test_save_and_soft_delete(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    topics = [
        {"source": "wechat", "fetch_mode": "crawler", "title": "AI 热点", "url": "http://x/1",
         "platform_hot": 100.0, "summary": "s", "fetch_date": "2026-09-09", "expired_at": "2026-10-09"},
    ]
    n = save_hot_topics(topics, str(db_path))
    assert n == 1
    # 重复 url 不再入库
    n2 = save_hot_topics(topics, str(db_path))
    assert n2 == 0

    soft_delete_topic("http://x/1", db_path=str(db_path))
    conn = connect(db_path)
    row = conn.execute("SELECT deleted FROM hot_topics WHERE url='http://x/1'").fetchone()
    log = query(conn, "SELECT clean_type FROM kb_clean_logs")
    conn.close()
    assert row["deleted"] == 1
    assert log[0]["clean_type"] == "manual"
