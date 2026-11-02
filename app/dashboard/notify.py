"""通知模块(PRD 5.10 告警渠道):Server酱 / 企业微信机器人,可选。"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from app.core.logger import get_logger
from app.core.settings import settings

logger = get_logger("notify")


class Notifier:
    def __init__(self, serverchan_key: str | None = None, wecom_webhook: str | None = None):
        self.serverchan_key = serverchan_key if serverchan_key is not None else settings.serverchan_key
        self.wecom_webhook = wecom_webhook if wecom_webhook is not None else settings.wecom_webhook

    def send(self, title: str, content: str) -> bool:
        """按已配置渠道发送,至少一个成功即 True。"""
        ok = False
        if self.serverchan_key:
            ok = self._send_serverchan(title, content) or ok
        if self.wecom_webhook:
            ok = self._send_wecom(title, content) or ok
        if not ok:
            logger.warning("未配置通知渠道或发送失败: %s", title)
        return ok

    def _send_serverchan(self, title: str, content: str) -> bool:
        try:
            url = f"https://sctapi.ftqq.com/{self.serverchan_key}.send"
            data = urllib.parse.urlencode({"title": title, "desp": content}).encode()
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:  # noqa: BLE001
            logger.error("Server酱发送失败: %s", e)
            return False

    def _send_wecom(self, title: str, content: str) -> bool:
        try:
            payload = json.dumps({
                "msgtype": "markdown",
                "markdown": {"content": f"**{title}**\n{content}"},
            }).encode()
            req = urllib.request.Request(
                self.wecom_webhook, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:  # noqa: BLE001
            logger.error("企业微信发送失败: %s", e)
            return False
