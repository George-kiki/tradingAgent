"""交易决策 Agent：汇总所有信息给出最终决策。"""
from __future__ import annotations

import json
import re

from agents.base import Agent, StockContext


class TraderAgent(Agent):
    role = "trader"
    cn_role = "首席交易决策"
    system_prompt = (
        "你是首席交易决策官。综合分析师报告、多空辩论、风控建议与量化策略信号，"
        "做出最终交易决策。必须严格按 JSON 输出，不要多余文字。\n"
        "JSON 字段：\n"
        '{"action":"买入|增持|持有|减持|卖出|观望", '
        '"confidence":0-100整数, '
        '"target_price":"目标价或区间字符串", '
        '"stop_loss":"止损位字符串", '
        '"position":"建议仓位百分比字符串", '
        '"horizon":"短线|中线|长线", '
        '"summary":"一句话核心结论", '
        '"reasons":["理由1","理由2","理由3"]}'
    )

    def run(self, ctx: StockContext, analyst_reports: str = "", debate_summary: str = "",
            risk_view: str = "", **kwargs) -> dict:
        prompt = (
            f"股票：{ctx.name}（{ctx.symbol}）\n"
            f"技术指标：{ctx.tech_brief()}\n"
            f"量化策略信号：{ctx.signal_brief()}\n"
            f"分析师报告：\n{analyst_reports}\n"
            f"多空辩论：\n{debate_summary}\n"
            f"风控建议：\n{risk_view}\n\n"
            "请综合以上信息，按系统要求的 JSON 格式输出最终决策。"
        )
        raw = self._ask(prompt, temperature=0.2, max_tokens=800)
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """从 LLM 输出中稳健提取 JSON。"""
        default = {
            "action": "观望", "confidence": 50, "target_price": "-",
            "stop_loss": "-", "position": "-", "horizon": "短线",
            "summary": raw[:80] if raw else "解析失败", "reasons": [],
            "raw": raw,
        }
        if not raw or raw.startswith("[LLM"):
            default["summary"] = raw or "LLM 未返回"
            return default
        # 去掉 ```json ``` 包裹
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return default
        try:
            data = json.loads(m.group(0))
            default.update({k: v for k, v in data.items() if v is not None})
            return default
        except Exception:
            return default
