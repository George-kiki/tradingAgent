"""Agent 基类与共享上下文。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents.llm import get_llm


@dataclass
class StockContext:
    """单只股票的全量分析上下文，供各 Agent 共享。"""
    symbol: str
    name: str
    snapshot: dict = field(default_factory=dict)        # 技术指标快照
    fund_flow: dict = field(default_factory=dict)       # 资金流
    news: list = field(default_factory=list)            # 新闻
    financials: str = ""                                # 财务摘要文本
    strategy_signals: list = field(default_factory=list)  # 各策略信号
    backtest: list = field(default_factory=list)        # 回测结果

    def tech_brief(self) -> str:
        s = self.snapshot
        return (
            f"最新价 {s.get('close')}（涨跌 {s.get('change_pct')}%），"
            f"MA5/10/20/60={s.get('ma5')}/{s.get('ma10')}/{s.get('ma20')}/{s.get('ma60')}，"
            f"MACD(DIF/DEA/柱)={s.get('macd_dif')}/{s.get('macd_dea')}/{s.get('macd_hist')}，"
            f"RSI={s.get('rsi')}，KDJ(K/D/J)={s.get('kdj_k')}/{s.get('kdj_d')}/{s.get('kdj_j')}，"
            f"布林上/下轨={s.get('boll_up')}/{s.get('boll_low')}，ATR={s.get('atr')}，"
            f"动量={s.get('momentum')}%，量/5日均量={s.get('volume')}/{s.get('vol_ma5')}"
        )

    def signal_brief(self) -> str:
        if not self.strategy_signals:
            return "无量化策略信号"
        return "；".join(
            f"{s['strategy']}={s['signal']}({s['reason']})" for s in self.strategy_signals
        )

    def news_brief(self, limit: int = 6) -> str:
        if not self.news:
            return "暂无相关新闻"
        return "\n".join(f"- {n.get('time','')} {n.get('title','')}" for n in self.news[:limit])

    def fund_brief(self) -> str:
        if not self.fund_flow:
            return "无资金流数据"
        items = [f"{k}:{v}" for k, v in list(self.fund_flow.items())[:8]]
        return "，".join(items)


class Agent:
    """LLM 驱动的分析 Agent 基类。"""

    role: str = "agent"
    cn_role: str = "智能体"
    system_prompt: str = "你是一个专业的金融分析助手。"

    def __init__(self):
        self.llm = get_llm()

    def _ask(self, user_prompt: str, temperature: float | None = None, max_tokens: int = 1200) -> str:
        return self.llm.chat(self.system_prompt, user_prompt, temperature, max_tokens)

    def run(self, ctx: StockContext, **kwargs: Any) -> str:  # pragma: no cover
        raise NotImplementedError
