"""业绩前瞻分析：用官方业绩预告 + 产业链/同业景气 + 历史季报趋势，前瞻预判本季业绩。

== 为什么这样设计 ==
A股没有公开的"上下游订单"结构化数据（商业机密），无法直接抓订单预测季报。
本模块改用机构常用的可行替代法，三路信号交叉验证：
1. 官方业绩预告/快报（forecast/express）——公司自报净利区间，最硬的前瞻信号；
2. 产业链/同业景气——同板块公司的预告与营收同比聚合，作为"产业链景气"代理指标；
3. 历史季报趋势外推——近几季营收/净利同比与利润率趋势，做基线预测。
最后（可选）交给 LLM 综合推演本季季报方向。
"""
from __future__ import annotations

from typing import Optional


def _fmt_yi(v: Optional[float]) -> str:
    if v is None:
        return "—"
    try:
        v = float(v)
        return f"{v/1e8:.2f}亿" if abs(v) >= 1e8 else f"{v/1e4:.0f}万"
    except Exception:
        return "—"


def _self_forecast(fetcher, symbol: str) -> dict:
    """本公司官方业绩预告 + 快报。"""
    fc = fetcher.get_forecast(symbol) or []
    ex = fetcher.get_express(symbol) or []
    latest = fc[0] if fc else None
    note = ""
    if latest:
        pcmin, pcmax = latest.get("p_change_min"), latest.get("p_change_max")
        rng = ""
        if pcmin is not None and pcmax is not None:
            rng = f"，净利同比{pcmin:.0f}%~{pcmax:.0f}%"
        note = (f"{latest.get('end_date','')[:6]} 业绩预告：{latest.get('type','')}{rng}"
                f"（净利 {_fmt_yi(latest.get('net_profit_min'))}~{_fmt_yi(latest.get('net_profit_max'))}）")
        if latest.get("change_reason"):
            note += f"；原因：{latest['change_reason'][:60]}"
    return {"forecasts": fc, "express": ex, "latest": latest, "note": note}


def _trend(fetcher, symbol: str) -> dict:
    """历史季报趋势 + 简单外推方向。"""
    tr = fetcher.get_fina_trend(symbol, periods=6) or []
    direction, note = "数据不足", ""
    if len(tr) >= 2:
        recent = tr[0]
        np_yoy = [t.get("netprofit_yoy") for t in tr[:4] if t.get("netprofit_yoy") is not None]
        or_yoy = [t.get("or_yoy") for t in tr[:4] if t.get("or_yoy") is not None]
        # 趋势方向：最近净利同比 + 是否较前期加速/减速
        cur = recent.get("netprofit_yoy")
        if cur is not None:
            accel = ""
            if len(np_yoy) >= 2:
                accel = "加速" if np_yoy[0] > np_yoy[1] else ("放缓" if np_yoy[0] < np_yoy[1] else "持平")
            if cur > 15:
                direction = f"高增长({accel})"
            elif cur > 0:
                direction = f"增长({accel})"
            elif cur > -15:
                direction = f"承压({accel})"
            else:
                direction = f"下滑({accel})"
            parts = [f"最新季净利同比{cur:+.0f}%"]
            if or_yoy:
                parts.append(f"营收同比{or_yoy[0]:+.0f}%")
            if recent.get("net_margin") is not None:
                parts.append(f"净利率{recent['net_margin']:.1f}%")
            note = "、".join(parts) + f"，趋势{direction}"
    return {"series": tr, "direction": direction, "note": note}


def _chain_prosperity(fetcher, symbol: str, industry: Optional[str]) -> dict:
    """产业链/同业景气：取同板块公司业绩预告聚合，作为产业链景气代理指标。"""
    peers_fc = []
    sector_name = industry
    try:
        # 用个股所属热门板块/行业找同业；优先用传入 industry
        if not sector_name:
            return {"available": False, "note": "", "sector": None, "peers": []}
        cons = fetcher.get_board_cons(sector_name, "行业") or \
            fetcher.get_board_cons(sector_name, "概念") or []
        codes = [c.get("代码") for c in cons if c.get("代码")][:12]  # 控制请求量
        up = down = 0
        for code in codes:
            if code == symbol:
                continue
            fc = fetcher.get_forecast(code) or []
            if not fc:
                continue
            latest = fc[0]
            t = latest.get("type", "")
            pcmax = latest.get("p_change_max")
            bullish = (t in ("预增", "略增", "扭亏", "续盈")) or (pcmax is not None and pcmax > 0)
            if bullish:
                up += 1
            else:
                down += 1
            peers_fc.append({"symbol": code, "type": t,
                             "p_change_max": pcmax})
            if len(peers_fc) >= 8:
                break
        total = up + down
        if total == 0:
            return {"available": False, "note": "同业暂无业绩预告", "sector": sector_name, "peers": []}
        ratio = up / total
        if ratio >= 0.6:
            tone = "景气向上"
        elif ratio <= 0.4:
            tone = "景气承压"
        else:
            tone = "景气分化"
        note = f"同业{total}家有预告：{up}家向好/{down}家承压，产业链{tone}"
        return {"available": True, "note": note, "sector": sector_name,
                "up": up, "down": down, "tone": tone, "peers": peers_fc}
    except Exception:
        return {"available": False, "note": "", "sector": sector_name, "peers": []}


def analyze_earnings_forecast(fetcher, symbol: str, name: str = "",
                              industry: Optional[str] = None,
                              llm=None) -> dict:
    """业绩前瞻综合分析。返回 dict（含自预告/趋势/产业链景气/LLM推演）。"""
    self_fc = _self_forecast(fetcher, symbol)
    trend = _trend(fetcher, symbol)
    chain = _chain_prosperity(fetcher, symbol, industry)

    available = bool(self_fc.get("latest") or trend.get("series") or chain.get("available"))

    # LLM 综合推演（可选）
    llm_view = ""
    if llm is not None and getattr(llm, "available", False) and available:
        try:
            facts = []
            if self_fc.get("note"):
                facts.append("【官方业绩预告】" + self_fc["note"])
            if trend.get("note"):
                facts.append("【历史季报趋势】" + trend["note"])
            if chain.get("note"):
                facts.append("【产业链/同业景气】" + chain["note"])
            user = (
                f"股票：{name}（{symbol}）\n" + "\n".join(facts) +
                "\n\n请基于以上信息，前瞻推演该公司【本期/下期季报】的业绩方向："
                "1)营收与净利大致方向（增长/承压/下滑及力度）"
                "2)主要驱动或拖累因素（结合产业链景气）"
                "3)需警惕的不确定性。"
                "注意：无真实订单数据，结论为基于公开信息的概率性推演，控制在220字内。"
            )
            llm_view = llm.chat(
                system=("你是A股产业链与业绩前瞻分析师，擅长用业绩预告、同业景气、历史趋势"
                        "前瞻推演公司季报。客观严谨，明确这是概率性预判而非确定结论。"),
                user=user, max_tokens=600,
            )
        except Exception as e:
            llm_view = f"[业绩前瞻推演失败: {e}]"

    # 汇总信号
    signals = [s for s in [self_fc.get("note"), chain.get("note"), trend.get("note")] if s]
    return {
        "available": available,
        "self_forecast": self_fc,
        "trend": trend,
        "chain": chain,
        "llm_view": llm_view,
        "summary": "；".join(signals) if signals else "暂无业绩前瞻数据（无官方预告/财务趋势）",
    }
