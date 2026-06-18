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
            "只输出一段完整 JSON，不要 Markdown，不要解释文字，字段不要换行截断。"
        )
        raw = self._ask(prompt, temperature=0.2, max_tokens=1200)
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """从 LLM 输出中稳健提取 JSON；不把原始 JSON 残片展示到前端。"""
        default = {
            "action": "观望", "confidence": 50, "target_price": "-",
            "stop_loss": "-", "position": "-", "horizon": "短线",
            "summary": "交易决策模型输出解析失败，已回退为观望；请结合上方分析师报告与风控意见复核。",
            "reasons": [],
            "raw": raw,
        }
        if not raw or raw.startswith("[LLM"):
            default["summary"] = raw or "LLM 未返回"
            return default

        # 去掉 ```json ``` 包裹，并提取最外层 JSON
        cleaned = re.sub(r"```(?:json)?|```", "", raw, flags=re.IGNORECASE).strip()
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                default.update({k: v for k, v in data.items() if v is not None})
                if isinstance(default.get("summary"), str) and default["summary"].lstrip().startswith("{"):
                    default["summary"] = "模型返回了异常 JSON 文本，已提取可用字段并回退缺失项。"
                return default
            except Exception:
                pass

        # JSON 被截断时，尽量按字段提取，避免前端显示半截 JSON
        fields = ["action", "target_price", "stop_loss", "position", "horizon", "summary"]
        for k in fields:
            mm = re.search(rf'"{k}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', cleaned, re.DOTALL)
            if mm:
                default[k] = mm.group(1).replace('\\"', '"').strip()
        mm = re.search(r'"confidence"\s*:\s*(\d+)', cleaned)
        if mm:
            default["confidence"] = max(0, min(100, int(mm.group(1))))
        reasons = re.findall(r'"reasons"\s*:\s*\[(.*?)\]', cleaned, re.DOTALL)
        if reasons:
            default["reasons"] = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', reasons[0])[:5]
        if not default.get("summary") or str(default["summary"]).lstrip().startswith(("{", '"action"')):
            default["summary"] = "模型输出不完整，已提取可用字段；缺失字段按保守观望处理。"
        return default
