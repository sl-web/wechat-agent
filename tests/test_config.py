"""配置中心测试:默认值、读写覆盖、版本记录。"""
from __future__ import annotations

import pytest

from app.core import config as cfg
from app.core.db import connect, init_db, query


@pytest.fixture()
def db_conn(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


def test_defaults_returned_when_empty(db_conn):
    # 断言"读不到覆盖值时回落到默认值",不硬编码具体数值(默认值会随运营需要调整)
    assert cfg.get_config("top_n", db_conn) == cfg.DEFAULTS["top_n"]
    assert cfg.get_config("source_weight", db_conn) == {
        "wechat": 0.6, "douyin": 0.4, "rss": 0.5,
    }
    assert cfg.get_config("data_sources", db_conn) == ["rss"]
    assert cfg.get_config("nonexistent_key", db_conn) is None


def test_set_and_get_override(db_conn):
    cfg.set_config("top_n", 50, db=db_conn)
    assert cfg.get_config("top_n", db_conn) == 50


def test_set_records_version(db_conn):
    cfg.set_config("keywords", ["AI"], updated_by="unit_test", db=db_conn)
    rows = query(db_conn, "SELECT key, value, updated_by FROM configs WHERE key='keywords'")
    assert rows[0]["updated_by"] == "unit_test"
    assert '"AI"' in rows[0]["value"]


def test_get_all_merges_defaults_and_overrides(db_conn):
    cfg.set_config("top_n", 33, db=db_conn)
    merged = cfg.get_all_config(db_conn)
    assert merged["top_n"] == 33
    assert merged["retention_days"] == 30  # 未被覆盖,仍为默认
