"""抖音热点采集器(PRD 5.1.3):API 模式优先,爬虫兜底。

POC-02 结论(2026-09-09 实测):
- 公开接口 aweme/v1/web/hot/search/list 需 msToken/a_bogus 签名,直连不可用
- www.douyin.com/hot 为动态渲染 SSR + 反爬,静态请求无热点数据
- → 爬虫兜底不可行;生产需用户在控制面板配置抖音开放平台 API 授权
"""
from __future__ import annotations

import json
import logging
import urllib.request

from app.collectors.base import BaseCollector, BaseHotTopic
from app.core.settings import settings

logger = logging.getLogger("app.douyin")

_HOT_API = "https://www.douyin.com/aweme/v1/web/hot/search/list/"
_PAGE = "https://www.douyin.com/hot"


class DouyinCollector(BaseCollector):
    source = "douyin"

    def fetch(self, mode: str = "auto") -> list[BaseHotTopic]:
        if mode in ("auto", "api"):
            if settings.douyin_api_key:
                try:
                    return self._fetch_api()
                except Exception as e:  # noqa: BLE001
                    if mode == "api":
                        raise
                    logger.warning("抖音 API 失败(%s),尝试爬虫", e)
            elif mode == "api":
                raise RuntimeError("未配置 douyin_api_key")
        if mode in ("auto", "crawler"):
            return self._fetch_crawler()
        return []

    def _fetch_api(self) -> list[BaseHotTopic]:
        req = urllib.request.Request(
            _HOT_API,
            headers={"User-Agent": "Mozilla/5.0", "Authorization": f"Bearer {settings.douyin_api_key}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        words = data.get("data", {}).get("word_list", [])
        out = []
        for w in words:
            out.append(
                BaseHotTopic(
                    source="douyin",
                    fetch_mode="api",
                    title=w.get("word", ""),
                    platform_hot=float(w.get("hot_value") or 0),
                    category=",".join([c.get("word", "") for c in w.get("word_cover", [])][:3]),
                    url=f"https://www.douyin.com/hot/{w.get('sentence_id', '')}",
                    summary=w.get("sentence", "")[:200],
                )
            )
        return out

    def _fetch_crawler(self) -> list[BaseHotTopic]:
        # POC 结论:静态爬虫不可行(需签名 + 动态渲染),明确失败提示走 API
        raise RuntimeError(
            "抖音爬虫模式不可用:热榜需签名鉴权与动态渲染(POC-02)。请在控制面板配置 douyin_api_key"
        )
