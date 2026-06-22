"""全局配置：从 .env 读取，集中管理。"""
from __future__ import annotations

import os
from functools import lru_cache

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # 未安装 python-dotenv 时，仅依赖系统环境变量，不影响运行
    pass


def _get_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


class Settings:
    """应用配置（单例）。"""

    # ---- DeepSeek ----
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # ---- 数据源 ----
    tushare_token: str = os.getenv("TUSHARE_TOKEN", "")
    # 自定义 Tushare API 地址（第三方代理服务）；为空时用 SDK 默认官方地址
    tushare_api_url: str = os.getenv("TUSHARE_API_URL", "")

    # ---- 消息推送 ----
    wechat_webhook: str = os.getenv("WECHAT_WEBHOOK", "")
    feishu_webhook: str = os.getenv("FEISHU_WEBHOOK", "")
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "465"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_to: str = os.getenv("SMTP_TO", "")
    push_channels: str = os.getenv("PUSH_CHANNELS", "console")

    # ---- 定时任务 ----
    select_push_time: str = os.getenv("SELECT_PUSH_TIME", "09:00")
    review_push_time: str = os.getenv("REVIEW_PUSH_TIME", "15:30")
    recommend_push_time: str = os.getenv("RECOMMEND_PUSH_TIME", "18:30")
    # 尾盘荐股：14:30 盘中推荐，今日尾盘买入、次日验证
    tail_recommend_push_time: str = os.getenv("TAIL_RECOMMEND_PUSH_TIME", "14:30")
    tail_recommend_enabled: bool = os.getenv("TAIL_RECOMMEND_ENABLED", "true").lower() in {"1", "true", "yes", "y"}

    # ---- 每日荐股 + 反思迭代 ----
    recommend_count: int = int(os.getenv("REC_COUNT", "5"))            # 每日推荐数量
    winrate_threshold: float = float(os.getenv("REC_WINRATE", "0.7"))  # 胜率达标线（<此值触发反思）
    win_pct_threshold: float = float(os.getenv("REC_WIN_PCT", "0.0"))  # 次日涨幅>此值(%)算"赢"
    recommend_db: str = os.getenv("REC_DB", os.path.join("data_store", "recommend.db"))

    @property
    def channels(self) -> list[str]:
        return [c.strip() for c in self.push_channels.split(",") if c.strip()]

    # ---- 缓存 ----
    cache_dir: str = os.getenv("CACHE_DIR", ".cache")
    cache_ttl: int = int(os.getenv("CACHE_TTL", "3600"))

    # ---- LLM 控制 ----
    enable_llm: bool = _get_bool("ENABLE_LLM", True)
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.3"))
    debate_rounds: int = int(os.getenv("DEBATE_ROUNDS", "2"))

    # ---- Web ----
    web_host: str = os.getenv("WEB_HOST", "127.0.0.1")
    web_port: int = int(os.getenv("WEB_PORT", "8000"))

    @property
    def llm_ready(self) -> bool:
        """LLM 是否可用：开启且配置了 key。"""
        return self.enable_llm and bool(self.deepseek_api_key)

    def ensure_dirs(self) -> None:
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs("reports", exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


settings = get_settings()
