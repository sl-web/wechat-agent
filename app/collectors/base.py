"""数据源抽象层(PRD 5.1.1):API/爬虫双模式统一接口 + 合规过滤。

所有采集器输出统一的 BaseHotTopic 元数据(不含正文全文,合规约束)。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta


@dataclass
class BaseHotTopic:
    """统一热点数据(仅元数据,合规)。"""

    source: str                    # wechat / douyin / rss
    title: str
    url: str = ""
    platform_hot: float = 0.0      # 平台原始热度(未归一化)
    author: str = ""
    summary: str = ""              # 摘要,非全文
    category: str = ""
    # 抓取时间:默认当前时刻。入库归入 fetch_date(今日抓取统计),勿覆盖!
    fetched_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    fetch_mode: str = "crawler"    # api / crawler / rss
    # 原文发布时间(可选):用于时效衰减评分,与 fetched_at(抓取时间)语义分离
    pub_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fetch_date"] = self.fetched_at[:10]
        # 合规存储期限:默认 30 天后(实际以控制面板配置为准,此处给默认)
        expired = datetime.now() + timedelta(days=30)
        d["expired_at"] = expired.strftime("%Y-%m-%d")
        return d


class BaseCollector(ABC):
    """数据源采集器抽象。子类实现 API 模式与爬虫模式。"""

    source: str = "base"

    def __init__(self, keywords: list[str] | None = None, top_n: int = 20):
        self.keywords = keywords or []
        self.top_n = top_n
        # 最近一次抓取的诊断信息(逐源结果/过滤漏斗),供上层生成准确的失败原因
        self.source_diag: list[dict] = []
        self.funnel: dict = {}

    @abstractmethod
    def fetch(self, mode: str = "auto") -> list[BaseHotTopic]:
        """抓取热点。
        mode: auto=API 优先,失败降级爬虫;api=强制 API;crawler=强制爬虫
        """
        raise NotImplementedError

    def _dedup(self, topics: list[BaseHotTopic]) -> list[BaseHotTopic]:
        """标题 MD5 去重。"""
        seen: set[str] = set()
        out: list[BaseHotTopic] = []
        for t in topics:
            key = abs(hash(t.title))
            if key not in seen:
                seen.add(key)
                out.append(t)
        return out

    def _filter_keywords(self, topics: list[BaseHotTopic]) -> list[BaseHotTopic]:
        """领域关键词过滤:标题/摘要任一命中即保留。"""
        if not self.keywords:
            return topics
        out = []
        for t in topics:
            text = f"{t.title} {t.summary} {t.category}"
            if any(k.lower() in text.lower() for k in self.keywords):
                out.append(t)
        return out

    def collect(self, mode: str = "auto") -> list[dict]:
        """对外入口:抓取 → 去重 → 关键词过滤 → 截断 top_n → 元数据 dict。"""
        topics = self.fetch(mode=mode)
        raw = len(topics)
        topics = self._dedup(topics)
        deduped = len(topics)
        topics = self._filter_keywords(topics)
        filtered = len(topics)
        topics = topics[: self.top_n]
        self.funnel = {
            "raw": raw,             # 源返回条数
            "deduped": deduped,     # 标题去重后
            "filtered": filtered,   # 关键词命中后
            "kept": len(topics),    # 实际进入下游(top_n 截断后)
        }
        return [t.to_dict() for t in topics]
