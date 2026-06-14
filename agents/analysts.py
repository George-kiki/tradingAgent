"""分析师团队：技术面、基本面、舆情。"""
from __future__ import annotations

from agents.base import Agent, StockContext


class TechnicalAnalyst(Agent):
    role = "technical"
    cn_role = "技术分析师"
    system_prompt = (
        "你是资深 A股技术分析师，精通均线、MACD、KDJ、布林带、量价等技术指标。"
        "请基于给定数据做客观分析，指出趋势、支撑/压力位、买卖点与风险。"
        "只做研究分析，不构成投资建议。回答简洁，控制在 250 字内。"
    )

    def run(self, ctx: StockContext, **kwargs) -> str:
        prompt = (
            f"股票：{ctx.name}（{ctx.symbol}）\n"
            f"技术指标：{ctx.tech_brief()}\n"
            f"量化策略信号：{ctx.signal_brief()}\n\n"
            "请给出技术面分析：1)当前趋势 2)关键支撑与压力位 3)短期方向判断 4)主要技术风险。"
        )
        return self._ask(prompt)


class FundamentalAnalyst(Agent):
    role = "fundamental"
    cn_role = "基本面分析师"
    system_prompt = (
        "你是 A股基本面分析师，关注盈利能力、成长性、估值与财务健康度。"
        "基于给定财务摘要客观分析，数据缺失时明确说明。只做研究，不构成投资建议。控制在 250 字内。"
    )

    def run(self, ctx: StockContext, **kwargs) -> str:
        prompt = (
            f"股票：{ctx.name}（{ctx.symbol}）\n"
            f"财务摘要：\n{ctx.financials or '（暂无财务数据）'}\n\n"
            "请给出基本面分析：1)盈利与成长 2)财务健康度 3)估值是否合理 4)主要基本面风险。"
        )
        return self._ask(prompt)


class SentimentAnalyst(Agent):
    role = "sentiment"
    cn_role = "舆情分析师"
    system_prompt = (
        "你是 A股市场舆情与资金面分析师，擅长从新闻和资金流向解读市场情绪。"
        "客观分析，避免夸大。只做研究，不构成投资建议。控制在 220 字内。"
    )

    def run(self, ctx: StockContext, **kwargs) -> str:
        prompt = (
            f"股票：{ctx.name}（{ctx.symbol}）\n"
            f"资金流：{ctx.fund_brief()}\n"
            f"近期新闻：\n{ctx.news_brief()}\n\n"
            "请给出舆情与资金面分析：1)市场情绪偏多还是偏空 2)资金动向 3)需关注的消息面催化或风险。"
        )
        return self._ask(prompt)


ANALYSTS = [TechnicalAnalyst, FundamentalAnalyst, SentimentAnalyst]
