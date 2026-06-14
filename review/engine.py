"""复盘引擎：按配置组装多种复盘模式，生成完整复盘报告。"""
from __future__ import annotations

import datetime as dt

from agents.llm import get_llm
from config import get_review_config
from data.fetcher import get_fetcher
from review.modes import REVIEW_MODES, ReviewContext


class ReviewEngine:
    def __init__(self, config: dict | None = None):
        self.config = config or get_review_config()
        self.fetcher = get_fetcher()
        self.llm = get_llm()

    def generate(self) -> str:
        title = self.config.get("title", "每日盘后复盘")
        enabled = self.config.get("enabled_modes", [])
        use_llm = set(self.config.get("use_llm_for", []))
        options = self.config.get("mode_options", {})

        ctx = ReviewContext(self.fetcher, options, use_llm, self.llm)

        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        parts = [f"# 📋 {title}", f"> 生成时间：{now}　|　数据源：{self.fetcher.active_source}", ""]

        for mode_name in enabled:
            cls = REVIEW_MODES.get(mode_name)
            if not cls:
                parts.append(f"> ⚠️ 未知复盘模式：{mode_name}\n")
                continue
            try:
                parts.append(cls().run(ctx))
            except Exception as e:
                parts.append(f"## {mode_name}\n\n（生成失败：{e}）\n")

        parts.append("---\n> ⚠️ 本复盘由 AI 自动生成，仅供研究参考，不构成投资建议。")
        return "\n".join(parts)


def run_review(config: dict | None = None) -> str:
    return ReviewEngine(config).generate()
