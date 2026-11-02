"""微信热文采集器(PRD 5.1.2):API 模式优先,爬虫兜底。

现实约束:
- 微信公众平台无公开热文榜页面;微信"看一看/搜一搜"需登录态且反爬强
- → 生产需用户配置第三方热文数据 API(新榜/清博等,见控制面板账号分组)
- 爬虫模式针对"开放页面源"(如搜狗微信搜索)仅做适配占位,标注不可靠
"""
from __future__ import annotations

import logging

from app.collectors.base import BaseCollector, BaseHotTopic
from app.core.settings import settings

logger = logging.getLogger("app.wechat")


class WechatCollector(BaseCollector):
    source = "wechat"

    def fetch(self, mode: str = "auto") -> list[BaseHotTopic]:
        if mode in ("auto", "api"):
            if settings.wechat_api_key:
                return self._fetch_api()
            elif mode == "api":
                raise RuntimeError("未配置 wechat_api_key")
        if mode in ("auto", "crawler"):
            return self._fetch_crawler()
        return []

    def _fetch_api(self) -> list[BaseHotTopic]:
        """第三方热文 API 适配(需按实际供应商实现,此处返回空并提示)。"""
        logger.warning(
            "微信 API 适配器待接入具体供应商(新榜/清博)。当前 wechat_api_key 已配置但无对应实现"
        )
        return []

    def _fetch_crawler(self) -> list[BaseHotTopic]:
        raise RuntimeError(
            "微信热文爬虫模式不可用:无公开热文榜页面(POC-02)。请配置第三方热文 API"
        )
