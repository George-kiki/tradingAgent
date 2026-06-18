"""编排器：串联数据采集 -> 量化策略 -> 多智能体分析 -> 最终决策。"""
from __future__ import annotations

import datetime as dt
import pandas as pd

from agents.analysts import ANALYSTS
from agents.base import StockContext
from agents.llm import get_llm
from agents.researchers import run_debate
from agents.risk_manager import RiskManager
from agents.trader import TraderAgent
from core.config import settings
from core.indicators import latest_snapshot
from data.fetcher import get_fetcher, _require_ak
from strategies.library import STRATEGY_REGISTRY


def _financials_to_text(df: pd.DataFrame, max_rows: int = 8) -> str:
    if df is None or df.empty:
        return ""
    try:
        cols = list(df.columns)[:5]
        return df[cols].head(max_rows).to_string(index=False)
    except Exception:
        return ""


class AgentOrchestrator:
    def __init__(self):
        self.fetcher = get_fetcher()
        self.llm = get_llm()

    # ---------- 上下文构建 ----------
    def build_context(self, symbol: str) -> tuple[StockContext, pd.DataFrame]:
        f = self.fetcher
        df = f.get_kline(symbol, days=250)
        if df.empty:
            raise ValueError(f"无法获取 {symbol} 的行情数据，请检查代码是否正确")

        name = f.get_name(symbol)
        snapshot = latest_snapshot(df)

        # 用全市场实时快照覆盖 K 线收盘价，保证分析页显示盘中最新价而非昨日收盘
        try:
            spot = f.get_market_spot()
            if spot is not None and not spot.empty:
                code_col = next((c for c in spot.columns if c in ("代码", "股票代码")), None)
                price_col = next((c for c in spot.columns if "最新价" in c or c == "最新价"), None)
                pct_col = next((c for c in spot.columns if "涨跌幅" in c), None)
                if code_col and price_col:
                    row = spot[spot[code_col].astype(str).str.replace("","") == symbol]
                    if not row.empty:
                        r = row.iloc[0]
                        rt_price = float(pd.to_numeric(r[price_col], errors="coerce")) if pd.notna(r.get(price_col)) else None
                        rt_pct = float(pd.to_numeric(r[pct_col], errors="coerce")) if pct_col and pd.notna(r.get(pct_col)) else None
                        if rt_price is not None:
                            snapshot["close"] = round(rt_price, 3)
                            snapshot["change_pct"] = round(rt_pct, 3) if rt_pct is not None else snapshot.get("change_pct")
        except Exception:
            pass

        # 全市场快照未覆盖时（如快照不完整/沪市缺失），尝试个股实时报价兜底
        if snapshot.get("date", "") < str(dt.date.today().strftime("%Y-%m-%d")):
            try:
                _require_ak()
                import akshare as ak
                m = "sh" if str(symbol).startswith(("6",)) else "sz"
                q = ak.stock_zh_a_spot_em(symbol=str(symbol), market=m)
                if q is not None and not q.empty:
                    pc = next((c for c in q.columns if "最新价" in c), None)
                    pcc = next((c for c in q.columns if "涨跌幅" in c), None)
                    if pc:
                        p = float(pd.to_numeric(q.iloc[0][pc], errors="coerce"))
                        if pd.notna(p):
                            snapshot["close"] = round(float(p), 3)
                        if pcc:
                            cp = float(pd.to_numeric(q.iloc[0][pcc], errors="coerce"))
                            if pd.notna(cp):
                                snapshot["change_pct"] = round(float(cp), 3)
            except Exception:
                pass

        # 全部策略信号
        signals = []
        for strat in STRATEGY_REGISTRY.values():
            try:
                signals.append(strat.current_signal(df).to_dict())
            except Exception:
                pass

        ctx = StockContext(
            symbol=symbol,
            name=name,
            snapshot=snapshot,
            fund_flow=f.get_fund_flow(symbol),
            news=f.get_news(symbol),
            financials=_financials_to_text(f.get_financials(symbol)),
            strategy_signals=signals,
        )
        return ctx, df

    def _build_scorecard(self, symbol: str) -> dict:
        """基本面评分卡（移植自 stock-analysis skill）。"""
        from report.scorecard import build_scorecard
        try:
            return build_scorecard(self.fetcher.get_valuation_metrics(symbol))
        except Exception:
            return {"available": False, "categories": [], "rating": "数据不足"}

    # ---------- 量化综合评分（无 LLM 时降级使用）----------
    @staticmethod
    def quant_consensus(ctx: StockContext) -> dict:
        scores = [s["score"] for s in ctx.strategy_signals]
        avg = sum(scores) / len(scores) if scores else 0.0
        buy = sum(1 for s in ctx.strategy_signals if s["signal"] == "BUY")
        sell = sum(1 for s in ctx.strategy_signals if s["signal"] == "SELL")
        if avg > 0.4:
            action = "买入"
        elif avg > 0.15:
            action = "增持"
        elif avg < -0.4:
            action = "卖出"
        elif avg < -0.15:
            action = "减持"
        else:
            action = "持有" if avg >= 0 else "观望"
        return {
            "action": action,
            "confidence": int(min(95, 50 + abs(avg) * 50)),
            "avg_score": round(avg, 3),
            "buy_signals": buy,
            "sell_signals": sell,
            "total_strategies": len(ctx.strategy_signals),
        }

    @staticmethod
    def _fund_score(scorecard: dict) -> float:
        """把基本面评分卡折算成 0-100 分（good=100/mid=55/bad=15 加权平均）。"""
        if not scorecard or not scorecard.get("available"):
            return 50.0
        pts, n = 0.0, 0
        for cat in scorecard.get("categories", []):
            for row in cat.get("rows", []):
                lv = row.get("level")
                if lv == "good":
                    pts += 100; n += 1
                elif lv == "mid":
                    pts += 55; n += 1
                elif lv == "bad":
                    pts += 15; n += 1
        return round(pts / n, 1) if n else 50.0

    @staticmethod
    def _verdict(overall: float) -> dict:
        """综合评分 -> 结论分档（参考 UZI 阈值 80/65/50/35）。"""
        if overall >= 80:
            return {"label": "值得重仓", "tone": "buy", "emoji": "🚀"}
        if overall >= 65:
            return {"label": "可以蹲一蹲", "tone": "buy", "emoji": "👀"}
        if overall >= 50:
            return {"label": "观望（偏多）", "tone": "hold", "emoji": "⚖️"}
        if overall >= 35:
            return {"label": "观望（偏空）", "tone": "hold", "emoji": "🤔"}
        return {"label": "回避", "tone": "sell", "emoji": "⛔"}

    # ---------- 完整分析 ----------
    def analyze(self, symbol: str, verbose: bool = False) -> dict:
        ctx, df = self.build_context(symbol)
        quant = self.quant_consensus(ctx)
        scorecard = self._build_scorecard(symbol)

        # 投资大佬评审团（规则驱动，无需 LLM）
        from agents.judges import run_jury
        try:
            metrics = self.fetcher.get_valuation_metrics(symbol)
        except Exception:
            metrics = {}
        jury = run_jury(metrics, ctx.snapshot, quant, scorecard, ctx.fund_flow)

        # DCF 估值与三情景
        from agents.valuation import compute_valuation
        try:
            valuation = compute_valuation(metrics, ctx.snapshot, ctx.name)
        except Exception:
            valuation = {"available": False, "note": "估值测算失败"}

        # 业绩前瞻（官方预告 + 产业链景气 + 历史趋势外推 + LLM 推演）
        from agents.earnings_forecast import analyze_earnings_forecast
        try:
            industry = (metrics or {}).get("行业") or (metrics or {}).get("所处行业")
            if not industry:
                industry = self.fetcher.get_stock_industry(symbol) or None
            earnings = analyze_earnings_forecast(
                self.fetcher, symbol, name=ctx.name, industry=industry, llm=self.llm)
        except Exception as e:
            earnings = {"available": False, "summary": f"业绩前瞻分析失败: {e}"}

        # 产业链/渠道线索：Agent 自动从公开新闻与搜索结果中寻找客户、订单、物料、产能等线索
        from agents.channel_research import analyze_public_channel_research
        try:
            channel_research = analyze_public_channel_research(
                self.fetcher, symbol, name=ctx.name, industry=industry, llm=self.llm)
        except Exception as e:
            channel_research = {"available": False, "summary": f"产业链线索自动检索失败: {e}"}

        # 综合评分 = 基本面 ×0.6 + 评委共识 ×0.4，并给出结论分档
        fund_score = self._fund_score(scorecard)
        consensus = jury.get("consensus", 50.0)
        overall = round(fund_score * 0.6 + consensus * 0.4, 1)
        verdict = self._verdict(overall)

        result = {
            "symbol": symbol,
            "name": ctx.name,
            "snapshot": ctx.snapshot,
            "strategy_signals": ctx.strategy_signals,
            "quant_consensus": quant,
            "scorecard": scorecard,
            "jury": jury,
            "valuation": valuation,
            "earnings_forecast": earnings,
            "channel_research": channel_research,
            "overall_score": overall,
            "fund_score": fund_score,
            "consensus_score": consensus,
            "verdict": verdict,
            "llm_enabled": self.llm.available,
        }

        if not self.llm.available:
            # 纯量化降级：直接用量化共识作为决策
            result["decision"] = {
                "action": quant["action"],
                "confidence": quant["confidence"],
                "summary": f"量化综合评分 {quant['avg_score']}，"
                           f"{quant['buy_signals']}买/{quant['sell_signals']}卖（共{quant['total_strategies']}策略）",
                "reasons": [s["reason"] for s in ctx.strategy_signals if s["signal"] != "HOLD"][:5],
                "mode": "quant_only",
            }
            return result

        # ---- 多智能体流程 ----
        analyst_reports = {}
        for cls in ANALYSTS:
            agent = cls()
            try:
                analyst_reports[agent.cn_role] = agent.run(ctx)
            except Exception as e:
                analyst_reports[agent.cn_role] = f"[分析失败: {e}]"
        if channel_research.get("available"):
            analyst_reports["产业链调研分析师"] = (
                channel_research.get("llm_view") or channel_research.get("summary") or "已录入产业链/渠道线索，需进一步核验。")
        reports_text = "\n\n".join(f"【{k}】\n{v}" for k, v in analyst_reports.items())

        debate = run_debate(ctx, reports_text, rounds=settings.debate_rounds)
        debate_summary = (
            f"多头观点：{debate['final_bull']}\n空头观点：{debate['final_bear']}"
        )

        risk_view = RiskManager().run(ctx, reports_text, debate_summary)
        decision = TraderAgent().run(ctx, reports_text, debate_summary, risk_view)
        decision["mode"] = "multi_agent"

        result.update({
            "analyst_reports": analyst_reports,
            "debate": debate,
            "risk_assessment": risk_view,
            "decision": decision,
        })
        return result


def analyze_stock(symbol: str) -> dict:
    """便捷入口。"""
    return AgentOrchestrator().analyze(symbol)
