"""多空研究员：基于分析师报告进行多轮辩论。"""
from __future__ import annotations

from agents.base import Agent, StockContext


class BullResearcher(Agent):
    role = "bull"
    cn_role = "多头研究员"
    system_prompt = (
        "你是坚定的多头研究员。基于分析师报告，论证买入/看多的理由，"
        "强调上涨催化、成长与机会，同时理性回应空头观点。控制在 200 字内。"
    )

    def debate(self, ctx: StockContext, analyst_reports: str, bear_view: str = "") -> str:
        prompt = (
            f"股票：{ctx.name}（{ctx.symbol}）\n"
            f"分析师报告汇总：\n{analyst_reports}\n\n"
            + (f"空头最新观点：\n{bear_view}\n\n" if bear_view else "")
            + "请作为多头，提出看多论据，并反驳空头观点（如有）。"
        )
        return self._ask(prompt, max_tokens=600)


class BearResearcher(Agent):
    role = "bear"
    cn_role = "空头研究员"
    system_prompt = (
        "你是严谨的空头研究员。基于分析师报告，论证谨慎/看空的理由，"
        "强调下行风险、估值与不确定性，同时理性回应多头观点。控制在 200 字内。"
    )

    def debate(self, ctx: StockContext, analyst_reports: str, bull_view: str = "") -> str:
        prompt = (
            f"股票：{ctx.name}（{ctx.symbol}）\n"
            f"分析师报告汇总：\n{analyst_reports}\n\n"
            + (f"多头最新观点：\n{bull_view}\n\n" if bull_view else "")
            + "请作为空头，提出看空/风险论据，并反驳多头观点（如有）。"
        )
        return self._ask(prompt, max_tokens=600)


def run_debate(ctx: StockContext, analyst_reports: str, rounds: int = 2) -> dict:
    """执行多空多轮辩论，返回辩论记录。"""
    bull, bear = BullResearcher(), BearResearcher()
    transcript = []
    bull_view, bear_view = "", ""
    for r in range(1, rounds + 1):
        bull_view = bull.debate(ctx, analyst_reports, bear_view)
        bear_view = bear.debate(ctx, analyst_reports, bull_view)
        transcript.append({"round": r, "bull": bull_view, "bear": bear_view})
    return {"transcript": transcript, "final_bull": bull_view, "final_bear": bear_view}
