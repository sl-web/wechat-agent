"""领域关键词过滤与清洗(PRD 5.1.4):去重/去广告/去标题党。"""
from __future__ import annotations

import re

_AD_KEYWORDS = ["加微信", "扫码领", "限时特惠", "点击购买", "广告", "推广", "福利群", "拼团"]
_CLICKBAIT_PATTERN = re.compile(r"(震惊|速看|删前速看|不转不是|99%的人|真相竟是|惊呆)")


class Cleaner:
    def __init__(
        self,
        ad_keywords: list[str] | None = None,
        clickbait: re.Pattern | None = None,
    ):
        self.ad_keywords = ad_keywords or _AD_KEYWORDS
        self.clickbait = clickbait or _CLICKBAIT_PATTERN

    def is_ad(self, title: str, summary: str = "") -> bool:
        text = f"{title} {summary}"
        return any(k in text for k in self.ad_keywords)

    def is_clickbait(self, title: str) -> bool:
        return bool(self.clickbait.search(title))

    def has_no_info(self, title: str, summary: str = "") -> bool:
        """无实际信息量:长度过短或纯标点/emoji。"""
        text = f"{title} {summary}".strip()
        letters = re.sub(r"[\s\W_]+", "", text)
        return len(letters) < 8

    def clean(self, items: list[dict]) -> list[dict]:
        """过滤广告/标题党/无信息量条目。"""
        out = []
        for it in items:
            if self.is_ad(it.get("title", ""), it.get("summary", "")):
                continue
            if self.is_clickbait(it.get("title", "")):
                continue
            if self.has_no_info(it.get("title", ""), it.get("summary", "")):
                continue
            out.append(it)
        return out
