"""风险管理 Agent：评估风险并给出仓位与止损建议。"""
from __future__ import annotations

from agents.base import Agent, StockContext


class RiskManager(Agent):
    role = "risk"
    cn_role = "风险经理"
    system_prompt = (
        "你是严格的风险管理经理。综合技术、基本面、舆情与多空辩论，"
        "评估该标的的整体风险等级（低/中/高），并给出合理的仓位建议（0%-100%）、"
        "止损位与止盈位参考。强调风险控制优先。控制在 220 字内。"
    )

    def run(self, ctx: StockContext, analyst_reports: str = "", debate_summary: str = "", **kwargs) -> str:
        atr = ctx.snapshot.get("atr")
        close = ctx.snapshot.get("close")
        prompt = (
            f"股票：{ctx.name}（{ctx.symbol}）\n"
            f"当前价：{close}，ATR(波动)：{atr}\n"
            f"技术指标：{ctx.tech_brief()}\n"
            f"分析师报告：\n{analyst_reports}\n"
            f"多空辩论：\n{debate_summary}\n\n"
            "请输出：1)风险等级 2)建议仓位 3)止损位 4)止盈位 5)风控要点。"
        )
        return self._ask(prompt)
