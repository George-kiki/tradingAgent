"""DeepSeek LLM 客户端（OpenAI 兼容协议）。"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from core.config import settings


class LLMClient:
    """对 DeepSeek 的薄封装，统一 chat 接口。"""

    def __init__(self):
        self._client = None

    @property
    def available(self) -> bool:
        return settings.llm_ready

    def _ensure_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except Exception as e:  # pragma: no cover
                raise RuntimeError("未安装 openai，请执行 pip install -r requirements.txt") from e
            self._client = OpenAI(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
            )
        return self._client

    def chat(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: int = 1500,
        json_mode: bool = False,
    ) -> str:
        """单轮对话，返回文本。LLM 不可用时返回提示串。

        json_mode=True 时启用 response_format=json_object，保证返回合法 JSON（不含围栏/多余文字）。
        """
        if not self.available:
            return "[LLM 未启用：未配置 DEEPSEEK_API_KEY 或 ENABLE_LLM=false]"
        client = self._ensure_client()
        kwargs = dict(
            model=settings.deepseek_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=settings.llm_temperature if temperature is None else temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = client.chat.completions.create(**kwargs)
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            # 兼容不支持 response_format 的端点：去掉后重试一次
            if json_mode:
                try:
                    kwargs.pop("response_format", None)
                    resp = client.chat.completions.create(**kwargs)
                    return (resp.choices[0].message.content or "").strip()
                except Exception as e2:
                    return f"[LLM 调用失败: {e2}]"
            return f"[LLM 调用失败: {e}]"


@lru_cache(maxsize=1)
def get_llm() -> LLMClient:
    return LLMClient()
