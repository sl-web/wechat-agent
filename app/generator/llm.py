"""LLM 统一封装(OpenAI 兼容接口,PRD 附录 B5):模型/参数可切换。

默认通义千问 qwen3.6-plus,base_url/api_key 从 .env 读取。
"""
from __future__ import annotations

from typing import Any

from app.core.logger import get_logger
from app.core.settings import settings

logger = get_logger("llm")

# 识别"支持 enable_thinking 参数"的供应商/模型(auto 模式用)
_THINKING_PROVIDERS = ("dashscope", "aliyuncs")
_THINKING_MODELS = ("qwen",)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ):
        self.base_url = base_url or settings.llm_base_url
        self.api_key = api_key or settings.llm_api_key
        self.model = model or settings.llm_model
        self.temperature = temperature if temperature is not None else settings.llm_temperature
        self.max_tokens = max_tokens or settings.llm_max_tokens

        if not self.api_key:
            logger.warning("LLM_API_KEY 未配置,调用将失败")
        if self.should_disable_thinking():
            logger.info("已对 %s 关闭思考模式(实测可省 ~90%% 生成耗时)", self.model)

    def should_disable_thinking(self) -> bool:
        """是否给这次请求下发 `enable_thinking=False`。

        背景:思考模式会生成大量**不返回给用户**的推理 token ——
        实测 `qwen3.6-plus` 一次生成输出 9589 tokens,其中可见正文只占约 1400,
        首字要等 148 秒,占掉整次生成的 90% 时间。关掉后 165s → 19s。

        ⚠️ 但 `enable_thinking` 是**供应商特有参数**:对不支持的供应商下发可能直接 400。
        所以默认 auto 只对已知支持的下发,换了供应商请显式设 `LLM_THINKING=on`。
        """
        mode = str(getattr(settings, "llm_thinking", "auto") or "auto").strip().lower()
        if mode in ("on", "true", "1", "yes", "开"):
            return False
        if mode in ("off", "false", "0", "no", "关"):
            return True
        hay = f"{self.base_url} {self.model}".lower()
        return any(k in hay for k in _THINKING_PROVIDERS + _THINKING_MODELS)

    def _apply_thinking(self, kwargs: dict) -> None:
        """按配置补上 extra_body(调用方显式传了就不覆盖)。"""
        extra = dict(kwargs.get("extra_body") or {})
        if "enable_thinking" in extra:
            return
        if self.should_disable_thinking():
            extra["enable_thinking"] = False
            kwargs["extra_body"] = extra
        elif extra:
            kwargs["extra_body"] = extra

    def _client(self):
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise LLMError("openai 库未安装,请 pip install openai") from e
        # 明确超时与不自动重试:失败要尽快暴露给上层(由应用层熔断/重试控制),
        # 否则模型无响应时请求会长时间挂起,前端只能干等。
        return OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=settings.llm_timeout,
            max_retries=0,
        )

    def chat(self, messages: list[dict], **kwargs: Any) -> str:
        """通用对话补全。"""
        client = self._client()
        self._apply_thinking(kwargs)
        try:
            resp = client.chat.completions.create(
                model=kwargs.pop("model", self.model),
                messages=messages,
                temperature=kwargs.pop("temperature", self.temperature),
                max_tokens=kwargs.pop("max_tokens", self.max_tokens),
                **kwargs,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            logger.error("LLM 调用失败: %s", e)
            raise LLMError(f"LLM 调用失败: {e}") from e

    def generate(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> str:
        """简化入口:system + user 两段 prompt。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return self.chat(messages, **kwargs)


# 全局单例(默认配置);控制面板切换模型时重新构造
_default_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client


def reset_client() -> None:
    global _default_client
    _default_client = None
