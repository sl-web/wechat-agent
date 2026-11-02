"""应用设置:从 .env 加载敏感项 + 运行目录。"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen3.6-plus"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 2000
    # 单次 LLM 请求超时(秒)。真实生成较慢(实测 3~6 分钟),
    # 这里给足时间但**必须有上限**,否则模型无响应时前端会无限等待。
    llm_timeout: float = 420.0
    # 思考模式(思维链):auto / on / off
    #
    # 实测(qwen3.6-plus + DashScope):思考阶段占掉整次生成 **90% 的时间** ——
    # 输出 9589 tokens 里约 8000 个是**不返回给用户**的隐藏推理,首字要等 148 秒。
    # 关掉后同一份提示词从 165 秒降到 19 秒,正文长度与结构不变、照样过质量护栏。
    #
    # auto(默认):只在识别为 DashScope/Qwen 时下发 enable_thinking=False。
    #   ⚠️ 这个参数是供应商特有的,对不支持的供应商(OpenAI/Ollama 等)下发可能直接 400 ——
    #   所以换了供应商要显式设为 on。
    # on  :不下发,保留供应商默认(即思考开)。
    # off :总是下发 enable_thinking=False。
    llm_thinking: str = "auto"

    # 数据源 API(可选)
    wechat_api_key: str = ""
    wechat_api_secret: str = ""
    douyin_api_key: str = ""

    # 通知
    serverchan_key: str = ""
    wecom_webhook: str = ""

    # 运行目录
    data_dir: Path = _PROJECT_ROOT / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"


settings = Settings()
