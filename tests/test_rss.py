"""RSS 聚合采集器测试:解析 RSS/Atom、去重、关键词过滤、入库。"""
from __future__ import annotations

from app.collectors.rss import RssCollector, _hot_from_age, _strip_html
from app.collectors.storage import save_hot_topics
from app.core.db import init_db, query

# 模拟量子位风格 RSS 2.0
RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>量子位</title>
<item>
  <title>实测星火X2.5：手搓粒子月亮、拆完61页财报还揪出了Bug</title>
  <link>https://www.qbitai.com/2026/09/1.html</link>
  <description>&lt;p&gt;国产大模型实测,AI 代码能力提升&lt;/p&gt;</description>
  <pubDate>Tue, 09 Sep 2026 08:00:00 +0800</pubDate>
</item>
<item>
  <title>今天的晚餐吃什么</title>
  <link>https://www.qbitai.com/2026/09/2.html</link>
  <description>与科技无关的生活内容</description>
  <pubDate>Tue, 09 Sep 2026 07:00:00 +0800</pubDate>
</item>
</channel></rss>"""

# 模拟 Atom(InfoQ 风格)
ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>InfoQ</title>
<entry>
  <title>Kubernetes 推广 KYAML 配置规范</title>
  <link href="https://www.infoq.cn/article/1"/>
  <summary>云原生与 AI 基础设施</summary>
  <updated>2026-09-09T06:30:00Z</updated>
</entry>
</feed>"""


class FakeRssCollector(RssCollector):
    """注入本地 XML,避免联网。"""

    def __init__(self, xml_map: dict, **kw):
        super().__init__(**kw)
        self.xml_map = xml_map

    def _http_get(self, url, timeout=15):
        if url not in self.xml_map:
            raise RuntimeError(f"no feed {url}")
        return self.xml_map[url]


def test_parse_rss2():
    c = RssCollector(feeds=[])
    items = c._parse_feed("量子位", RSS_XML)
    assert len(items) == 2
    it = items[0]
    assert it.title.startswith("实测星火X2.5")
    assert it.url == "https://www.qbitai.com/2026/09/1.html"
    assert "大模型" in it.summary
    assert it.platform_hot > 0          # 新鲜度近似热度
    assert it.source == "rss"
    assert it.author == "量子位"


def test_parse_atom():
    c = RssCollector(feeds=[])
    items = c._parse_feed("InfoQ", ATOM_XML)
    assert len(items) == 1
    assert items[0].title == "Kubernetes 推广 KYAML 配置规范"


def test_collect_filters_and_dedups():
    c = FakeRssCollector(
        {"https://a/feed": RSS_XML, "https://b/feed": ATOM_XML},
        keywords=["AI", "大模型"], top_n=10,
        feeds=[{"name": "A", "url": "https://a/feed", "enabled": True},
               {"name": "B", "url": "https://b/feed", "enabled": True}],
    )
    items = c.collect()
    # RSS 2 条中 AI 相关 1 条;Atom 的 KYAML 条目摘要含 "AI" → 也保留,共 2 条
    assert len(items) == 2
    assert items[0]["title"].startswith("实测星火X2.5")
    assert items[0]["fetch_mode"] == "rss"
    assert "expired_at" in items[0]


def test_single_feed_failure_isolated():
    c = FakeRssCollector(
        {"https://b/feed": ATOM_XML},
        keywords=["AI", "Kubernetes"], top_n=10,
        feeds=[{"name": "A", "url": "https://a/feed", "enabled": True},
               {"name": "B", "url": "https://b/feed", "enabled": True}],
    )
    items = c.collect()   # A 源失败,不应抛异常
    assert len(items) == 1  # B 源入库


def test_hot_from_age():
    assert _hot_from_age(None) == 1.0
    old = _hot_from_age("2026-08-01 08:00:00")
    assert old <= 10.0        # 一个月前热度很低


def test_strip_html():
    assert _strip_html("<p>hello <b>world</b></p>") == "hello world"


def test_rss_save_to_db(tmp_path):
    from pathlib import Path

    db_path = tmp_path / "test.db"
    init_db(db_path)
    c = FakeRssCollector(
        {"https://a/feed": RSS_XML},
        keywords=["AI", "大模型"], top_n=10,
        feeds=[{"name": "A", "url": "https://a/feed", "enabled": True}],
    )
    items = c.collect()
    n = save_hot_topics(items, str(db_path))
    assert n == 1
    from app.core.db import connect

    conn = connect(db_path)
    row = query(conn, "SELECT source, title FROM hot_topics WHERE deleted=0")
    conn.close()
    assert row[0]["source"] == "rss"
