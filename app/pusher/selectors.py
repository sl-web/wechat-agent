"""选择器配置加载与页面改版自检(PRD 5.4)。"""
from __future__ import annotations

from pathlib import Path

import yaml

_SELECTORS_PATH = Path(__file__).parent / "selectors.yaml"


class SelectorManager:
    """加载 selectors.yaml,提供自检与版本管理。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else _SELECTORS_PATH
        self.data: dict = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            raise FileNotFoundError(f"选择器配置文件不存在: {self.path}")
        with open(self.path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def get(self, group: str, name: str) -> dict | None:
        """取单个选择器。"""
        g = self.data.get(group, {})
        return g.get(name)

    def all_of(self, group: str) -> dict:
        return self.data.get(group, {})

    def version(self) -> str:
        """配置版本(用于变更记录)。"""
        return self.data.get("version", "v1")

    def check_required(self, required: list[tuple[str, str]]) -> list[str]:
        """自检关键元素是否存在,返回缺失列表。"""
        missing = []
        for group, name in required:
            if self.get(group, name) is None:
                missing.append(f"{group}.{name}")
        return missing


def load_selectors(path: str | Path | None = None) -> SelectorManager:
    return SelectorManager(path)
