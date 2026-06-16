"""估值建模（参考 UZI-Skill / Anthropic financial-services 方法，适配 A股参数）。

提供：
1. DCF 现金流折现：WACC 拆解 + 两段式 FCF（高增长段 + 永续段）+ Gordon Growth 终值
   + 5×5 敏感性热力图（WACC × 永续增长率 -> 每股内在价值）
2. IC 投委会三情景（Bull / Base / Bear）：不同增长/折现假设下的目标价与概率

A股默认参数：无风险利率 rf=2.5%，股权风险溢价 ERP=6%，税率 25%，永续增长 g=2.5%。

所有结果为基于公开财务数据的模型测算，强依赖假设，**仅供研究，不构成投资建议**。
"""
from __future__ import annotations

from typing import Optional

RF = 0.025          # 无风险利率
ERP = 0.06          # 股权风险溢价
TAX = 0.25          # 企业所得税率
TERMINAL_G = 0.025  # 永续增长率
HIGH_YEARS = 5      # 高增长期年数


def _num(v) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None
    except Exception:
        return None


def _estimate_beta(snapshot: dict) -> float:
    """无现成 beta 时，按波动/动量粗略给一个 beta（0.8~1.4）。"""
    mom = abs(_num((snapshot or {}).get("momentum")) or 5)
    beta = 0.9 + min(mom, 30) / 60.0  # 动量越大波动越大，beta 越高
    return round(min(max(beta, 0.8), 1.4), 2)


def _dcf_per_share(fcf0: float, growth: float, wacc: float, term_g: float,
                   shares: float, net_debt: float = 0.0) -> Optional[float]:
    """两段式 DCF，返回每股内在价值。"""
    if wacc <= term_g or shares <= 0 or fcf0 is None:
        return None
    pv = 0.0
    fcf = fcf0
    for t in range(1, HIGH_YEARS + 1):
        fcf = fcf * (1 + growth)
        pv += fcf / ((1 + wacc) ** t)
    # 终值（Gordon Growth）
    tv = fcf * (1 + term_g) / (wacc - term_g)
    pv += tv / ((1 + wacc) ** HIGH_YEARS)
    equity = pv - net_debt
    return round(equity / shares, 2)


def compute_valuation(metrics: dict, snapshot: dict, name: str = "") -> dict:
    """基于财务指标 + 行情，做 DCF 估值与三情景测算。"""
    m = metrics or {}
    s = snapshot or {}
    price = _num(s.get("close"))
    total_mv = _num(m.get("total_mv"))   # 总市值（元，东财通常为元；若为亿需上层统一）
    pe = _num(m.get("pe"))
    profit_g = _num(m.get("profit_growth"))
    rev_g = _num(m.get("revenue_growth"))
    net_margin = _num(m.get("net_margin"))

    # 数据不足，无法 DCF：至少需要价格；若有 PE 可用 EPS 反推每股 FCF，
    # 若有总市值则用总量口径计算再折回每股。
    if not price:
        return {"available": False,
                "note": "缺少股价数据，无法进行 DCF 估值测算。"}

    # 估算每股 FCF：优先用 PE 反推 EPS（EPS≈Price/PE），FCF/share≈EPS×0.8（保守）
    # 这样即使全市场快照缺少总市值，也能产出可比较的每股 DCF。
    estimated_pe = False
    if pe and pe > 0:
        fcf0 = (price / pe) * 0.8
        shares = 1.0
    elif total_mv and total_mv > 0:
        net_profit = total_mv * (0.05 if net_margin else 0.04)
        fcf0 = net_profit * 0.8
        shares = total_mv / price
    else:
        # 兜底：部分备用行情源无 PE/总市值。为保证报告结构完整，使用保守默认 PE=25
        # 做“假设型 DCF”，并在 note/assumptions 明确标记为估算。
        pe = 25.0
        estimated_pe = True
        fcf0 = (price / pe) * 0.8
        shares = 1.0

    beta = _estimate_beta(s)
    coe = RF + beta * ERP                 # 股权成本（CAPM）
    wacc = round(coe, 4)                  # 简化：以股权成本近似 WACC（无完整资本结构数据）

    # 增长假设：取净利/营收增速的合理区间
    base_g = profit_g if profit_g is not None else (rev_g if rev_g is not None else 8)
    base_g = max(min(base_g, 35), -5) / 100.0  # 限定 -5%~35%

    base_value = _dcf_per_share(fcf0, base_g, wacc, TERMINAL_G, shares)

    # 5×5 敏感性热力图：WACC（行）× 永续增长 g（列）
    waccs = [round(wacc + d, 4) for d in (-0.01, -0.005, 0, 0.005, 0.01)]
    term_gs = [round(TERMINAL_G + d, 4) for d in (-0.01, -0.005, 0, 0.005, 0.01)]
    heatmap = []
    for w in waccs:
        row = []
        for g in term_gs:
            v = _dcf_per_share(fcf0, base_g, w, g, shares)
            row.append(v)
        heatmap.append(row)

    # 三情景：Bull / Base / Bear（不同增长 + 折现假设）
    def _scenario(gr, wd, label, prob):
        v = _dcf_per_share(fcf0, gr, wacc + wd, TERMINAL_G, shares)
        upside = round((v / price - 1) * 100, 1) if (v and price) else None
        return {"label": label, "value": v, "upside": upside, "prob": prob,
                "growth": round(gr * 100, 1), "wacc": round((wacc + wd) * 100, 2)}

    scenarios = [
        _scenario(min(base_g + 0.06, 0.40), -0.005, "🐂 乐观 (Bull)", 0.25),
        _scenario(base_g, 0.0, "⚖️ 中性 (Base)", 0.50),
        _scenario(max(base_g - 0.06, -0.10), 0.005, "🐻 悲观 (Bear)", 0.25),
    ]
    # 概率加权目标价
    wavg = None
    vals = [(sc["value"], sc["prob"]) for sc in scenarios if sc["value"]]
    if vals:
        wavg = round(sum(v * p for v, p in vals) / sum(p for _, p in vals), 2)

    upside = round((base_value / price - 1) * 100, 1) if (base_value and price) else None
    verdict = "—"
    if upside is not None:
        if upside >= 30:
            verdict = "显著低估"
        elif upside >= 10:
            verdict = "低估"
        elif upside >= -10:
            verdict = "合理"
        elif upside >= -30:
            verdict = "高估"
        else:
            verdict = "显著高估"

    return {
        "available": base_value is not None,
        "price": price,
        "intrinsic_value": base_value,
        "upside": upside,
        "verdict": verdict,
        "weighted_target": wavg,
        "assumptions": {
            "rf": RF * 100, "erp": ERP * 100, "beta": beta,
            "wacc": round(wacc * 100, 2), "terminal_g": TERMINAL_G * 100,
            "base_growth": round(base_g * 100, 1), "high_years": HIGH_YEARS,
            "pe_used": pe, "pe_estimated": estimated_pe,
        },
        "heatmap": {
            "waccs": [round(w * 100, 2) for w in waccs],
            "term_gs": [round(g * 100, 2) for g in term_gs],
            "values": heatmap,
        },
        "scenarios": scenarios,
        "note": ("DCF 基于净利润近似 FCF 与简化 WACC，强依赖假设，仅供研究参考。"
                 + ("当前行情源缺少 PE/总市值，已用保守 PE=25 做假设型估算。" if estimated_pe else "")),
    }
