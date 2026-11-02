"""统一日志:控制台 + 文件轮转,敏感信息脱敏。"""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.settings import settings

# 敏感字段脱敏
_SENSITIVE_KEYS = re.compile(
    r"(api[_-]?key|secret|token|password|session|authorization)", re.IGNORECASE
)
_SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9_-]{6,}|Bearer\s+\S+|key['\"]?\s*[:=]\s*['\"]?\S+)", re.IGNORECASE)


def _redact(msg: str) -> str:
    return _SECRET_PATTERN.sub("[REDACTED]", str(msg))


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.msg)
        if record.args:
            record.args = tuple(_redact(a) if isinstance(a, str) else a for a in record.args)
        return True


def setup_logger(name: str = "app", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:  # 已初始化
        return logger
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(RedactFilter())
    logger.addHandler(console)

    log_dir: Path = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_h = RotatingFileHandler(
        log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_h.setFormatter(fmt)
    file_h.addFilter(RedactFilter())
    logger.addHandler(file_h)

    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    """获取子 logger(触发 setup_logger 初始化根格式)。"""
    setup_logger()
    return logging.getLogger(f"app.{name}")
