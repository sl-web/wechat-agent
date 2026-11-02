"""RSS/Atom 垂直源聚合采集器(默认信息源,替代付费热榜 API)。

从多个科技/AI 媒体 RSS 抓取 → 归一为 BaseHotTopic。
- 免费、无需申请授权
- 支持 RSS 2.0 (<item>) 与 Atom (<entry>)
- 关键词在标题/摘要命中过滤(复用基类)
- platform_hot 无真实值 → 用发布时间新鲜度近似(0~100,越新越高),
  保证下游归一化评分有意义
"""
from __future__ import annotations

import logging
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from app.collectors.base import BaseCollector, BaseHotTopic

logger = logging.getLogger("app.rss")

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}

# 默认订阅源(可经控制面板 config:rss_feeds 覆盖)
DEFAULT_FEEDS = [
    {"name": "量子位", "url": "https://www.qbitai.com/feed", "enabled": True},
    {"name": "IT之家", "url": "https://www.ithome.com/rss/", "enabled": True},
    {"name": "InfoQ", "url": "https://www.infoq.cn/feed", "enabled": True},
    {"name": "雷峰网", "url": "https://www.leiphone.com/feed", "enabled": True},
    {"name": "开源中国", "url": "https://www.oschina.net/news/rss", "enabled": True},
    {"name": "Solidot", "url": "https://www.solidot.org/index.rss", "enabled": True},
    {"name": "少数派", "url": "https://sspai.com/feed", "enabled": True},
    {"name": "新浪科技", "url": "https://rss.sina.com.cn/tech/rollnews.xml", "enabled": True},
]


def _local(tag: str) -> str:
    """去掉命名空间前缀:<atom:entry> -> entry"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(node, *names) -> str:
    for n in node.iter():
        if _local(n.tag) in names and n.text:
            return n.text.strip()
    return ""


def _find_link(node) -> str:
    # <link>text</link>(RSS)或 <link href="..."/>(Atom)
    for n in node.iter():
        if _local(n.tag) == "link":
            href = n.attrib.get("href")
            if href:
                return href.strip()
            if n.text and n.text.strip():
                return n.text.strip()
    return ""


def _parse_date(raw: str) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:300]


def _hot_from_age(published_at: str | None, now=None) -> float:
    """用新鲜度近似热度:24h 内线性衰减 100→10,更早 <10,无时间给 1。"""
    if not published_at:
        return 1.0
    try:
        dt = datetime.strptime(published_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        dt = datetime.fromisoformat(published_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    hours = max(0.0, (now - dt).total_seconds() / 3600.0)
    if hours <= 24:
        return round(100 - hours * 3.75, 2)   # 0h→100, 24h→10
    return round(max(1.0, 10 - hours * 0.2), 2)


class RssCollector(BaseCollector):
    """聚合多个 RSS 源。source 统一记 'rss',orig 记录实际媒体名。"""

    source = "rss"

    def __init__(self, keywords=None, top_n: int = 20, feeds: list[dict] | None = None):
        super().__init__(keywords=keywords, top_n=top_n)
        self.feeds = feeds or DEFAULT_FEEDS

    # ---- 抓取 ----
    def _http_get(self, url: str, timeout: int = 15) -> str:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            import gzip

            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "ignore")

    def _parse_feed(self, media: str, xml_text: str) -> list[BaseHotTopic]:
        head = (xml_text or "").lstrip()[:200].lower()
        if not head:
            raise ValueError("响应为空")
        if not (head.startswith("<?xml") or "<rss" in head or "<feed" in head):
            raise ValueError(f"返回内容不是 RSS/Atom(前 60 字:{head[:60]!r})")
        root = ET.fromstring(xml_text)
        out: list[BaseHotTopic] = []
        items = [n for n in root.iter() if _local(n.tag) in ("item", "entry")]
        for it in items:
            title = _child_text(it, "title")
            if not title:
                continue
            link = _find_link(it)
            summary = _strip_html(_child_text(it, "description", "summary", "content"))
            pub = _parse_date(_child_text(it, "pubDate", "published", "updated"))
            t = BaseHotTopic(
                source="rss",
                title=title.strip()[:200],
                url=link,
                platform_hot=_hot_from_age(pub),
                author=media,
                summary=summary,
                category=media,
                fetch_mode="rss",
            )
            # 原文发布时间放入 pub_at(供时效衰减);fetched_at 保持抓取时刻 → fetch_date 归入今日
            if pub:
                t.pub_at = pub
            out.append(t)
        return out

    def fetch(self, mode: str = "auto") -> list[BaseHotTopic]:
        """逐源抓取并合并。单源失败不影响其余;每源结果写入 source_diag。

        失败重试 1 次(网络抖动常见),最终仍失败则记录**真实异常**,
        供上层给出可读的失败原因(而非笼统的"热点池为空")。
        """
        if mode not in ("auto", "api", "crawler"):
            raise ValueError(f"未知模式: {mode}")
        merged: list[BaseHotTopic] = []
        self.source_diag = []
        for feed in self.feeds:
            if not feed.get("enabled", True):
                continue
            name = feed.get("name", "rss")
            url = feed["url"]
            t0 = time.time()
            last_err: Exception | None = None
            parsed: list[BaseHotTopic] = []
            for attempt in (1, 2):
                try:
                    xml_text = self._http_get(url)
                    parsed = self._parse_feed(name, xml_text)
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if attempt == 1:
                        time.sleep(0.8)  # 退避后重试一次
            cost = round(time.time() - t0, 1)
            if last_err is not None:
                err = f"{type(last_err).__name__}: {last_err}"
                logger.warning("RSS 源 %s 抓取失败(重试后): %s", name, err)
                self.source_diag.append(
                    {"name": name, "url": url, "ok": False, "count": 0,
                     "error": err[:160], "elapsed": cost}
                )
                continue
            logger.info("RSS 源 %s 抓到 %d 条", name, len(parsed))
            self.source_diag.append(
                {"name": name, "url": url, "ok": True, "count": len(parsed),
                 "error": "", "elapsed": cost}
            )
            merged.extend(parsed)
        return merged


def load_feeds(cfg: dict | None = None) -> list[dict]:
    """从配置读 rss_feeds,配置为空时回落到 DEFAULT_FEEDS。

    注意:这里**不做默认源自动合并**——配置是用户的最终意图(可增删/禁用某个源),
    若每次都把 DEFAULT_FEEDS 补回去,用户就永远删不掉不想要的源。
    新增默认源通过一次性迁移写入配置(见 app/core/config.py 的 DEFAULTS)。
    """
    if not cfg:
        return DEFAULT_FEEDS
    feeds = cfg.get("rss_feeds")
    if isinstance(feeds, list) and feeds:
        return [f for f in feeds if isinstance(f, dict)]
    return DEFAULT_FEEDS
