"""多维度综合评分：在技术面之外，纳入基本面/资金/消息面，使荐股更全面。

维度（每项给有界加/减分，汇总为 multi_bonus，叠加到技术面评分）：
1. 估值      —— PE / PB（合理低估加分，亏损/高估减分）
2. 业绩      —— ROE / 净利润同比 / 营收同比（高成长高盈利加分）
3. 北向资金  —— 陆股通持股占比及近5日增减持趋势（增持加分）
4. 龙虎榜    —— 近一月是否上榜及净买额（资金博弈关注，净买入加分）
5. 放量      —— 温和放量加分（来自技术因子）
6. 近期利好  —— 新闻标题关键词情感（利好加分、利空减分）

所有数据 best-effort：取不到的维度按中性(0分)处理，不影响其余维度，符合系统容错原则。
"""
from __future__ import annotations

import os
from typing import Optional


def _w(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default


# 各维度权重系数（可经环境变量覆盖）。业绩/利好/行业景气度已调高话语权。
DIM_WEIGHTS = {
    "valuation": _w("REC_W_VALUATION", 1.0),
    "performance": _w("REC_W_PERFORMANCE", 1.8),   # 业绩 ↑
    "northbound": _w("REC_W_NORTHBOUND", 1.0),
    "lhb": _w("REC_W_LHB", 1.0),
    "volume": _w("REC_W_VOLUME", 1.0),
    "catalyst": _w("REC_W_CATALYST", 1.8),         # 利好 ↑
    "industry": _w("REC_W_INDUSTRY", 1.6),         # 行业景气度 ↑（新增）
}

# 利好 / 利空 关键词（用于新闻标题情感打分，无需 LLM）
_POS_WORDS = [
    "中标", "中签", "增持", "回购", "业绩预增", "预盈", "扭亏", "净利润增长", "营收增长",
    "订单", "签约", "合作", "战略", "获批", "通过", "新高", "涨停", "重组", "并购",
    "高送转", "分红", "利好", "突破", "放量", "龙头", "首发", "量产", "扩产", "提价",
]
_NEG_WORDS = [
    "减持", "亏损", "预亏", "下滑", "下降", "问询", "处罚", "立案", "退市", "质押",
    "解禁", "诉讼", "违规", "下调", "跌停", "商誉减值", "停牌", "风险警示", "利空", "套现",
]


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _score_valuation(pe, pb) -> tuple[float, str]:
    s = 0.0
    notes = []
    if pe is not None:
        if pe <= 0:
            s -= 0.05
            notes.append(f"PE{pe:.1f}(亏损)")
        elif pe <= 20:
            s += 0.06
            notes.append(f"PE{pe:.1f}(低估)")
        elif pe <= 35:
            s += 0.03
            notes.append(f"PE{pe:.1f}(合理)")
        elif pe <= 60:
            notes.append(f"PE{pe:.1f}(偏高)")
        else:
            s -= 0.05
            notes.append(f"PE{pe:.1f}(高估)")
    if pb is not None:
        if pb <= 2:
            s += 0.03
            notes.append(f"PB{pb:.1f}")
        elif pb > 10:
            s -= 0.03
            notes.append(f"PB{pb:.1f}(高)")
    return s, "、".join(notes)


def _score_performance(roe, profit_g, rev_g) -> tuple[float, str]:
    s = 0.0
    notes = []
    if roe is not None:
        if roe >= 20:
            s += 0.06
        elif roe >= 12:
            s += 0.04
        elif roe >= 6:
            s += 0.01
        elif roe < 0:
            s -= 0.05
        notes.append(f"ROE{roe:.1f}%")
    if profit_g is not None:
        if profit_g >= 30:
            s += 0.06
        elif profit_g >= 10:
            s += 0.03
        elif profit_g < 0:
            s -= 0.04
        notes.append(f"净利{profit_g:+.0f}%")
    if rev_g is not None:
        if rev_g >= 15:
            s += 0.03
        elif rev_g < 0:
            s -= 0.02
        notes.append(f"营收{rev_g:+.0f}%")
    return s, "、".join(notes)


def _score_northbound(north: dict) -> tuple[float, str]:
    if not north:
        return 0.0, ""
    s = 0.0
    notes = []
    ratio = north.get("ratio")
    trend = north.get("trend_5d")
    if ratio is not None:
        notes.append(f"北向持股{ratio:.2f}%")
    if trend is not None:
        if trend > 0.05:
            s += 0.05
            notes.append(f"近5日增持+{trend:.2f}%")
        elif trend < -0.05:
            s -= 0.02
            notes.append(f"近5日减持{trend:.2f}%")
    return s, "、".join(notes)


def _score_lhb(lhb: dict) -> tuple[float, str]:
    if not lhb or not lhb.get("times"):
        return 0.0, ""
    s = 0.0
    net = lhb.get("net_buy", 0.0)
    times = lhb.get("times", 0)
    if net > 0:
        s += 0.05
    elif net < 0:
        s -= 0.03

    def _fmt(v):
        return f"{v/1e8:+.2f}亿" if abs(v) >= 1e8 else f"{v/1e4:+.0f}万"

    return s, f"近月上榜{times}次，净买{_fmt(net)}"


def _score_catalyst(news: list[dict]) -> tuple[float, str, list]:
    if not news:
        return 0.0, "", []
    pos, neg, hits = 0, 0, []
    for n in news[:8]:
        title = n.get("title", "")
        for w in _POS_WORDS:
            if w in title:
                pos += 1
                hits.append("利好:" + w)
                break
        for w in _NEG_WORDS:
            if w in title:
                neg += 1
                hits.append("利空:" + w)
                break
    s = _clip(pos * 0.03, 0, 0.08) - _clip(neg * 0.03, 0, 0.08)
    label = []
    if pos:
        label.append(f"{pos}条利好")
    if neg:
        label.append(f"{neg}条利空")
    return s, "、".join(label), hits[:4]


def _score_industry(fetcher, industry: Optional[str]) -> tuple[float, str]:
    """行业景气度：用行业板块涨幅榜衡量个股所属行业的强弱与排名。"""
    if not industry or industry in ("—", ""):
        return 0.0, ""
    sectors = _safe(lambda: fetcher.get_hot_sectors(limit=90), [])
    if not sectors:
        return 0.0, ""
    match, rank = None, None
    for i, s in enumerate(sectors):
        nm = s.get("name", "")
        if nm and (nm in industry or industry in nm
                   or nm.replace("行业", "") in industry or industry in nm.replace("行业", "")):
            match, rank = s, i + 1
            break
    if not match:
        return 0.0, ""
    pct = match.get("pct", 0.0)
    s = 0.0
    if pct >= 2:
        s += 0.06
    elif pct >= 0.5:
        s += 0.03
    elif pct <= -2:
        s -= 0.05
    elif pct < 0:
        s -= 0.02
    if rank and rank <= 5:        # 板块涨幅榜前5，景气度高
        s += 0.04
    elif rank and rank <= 10:
        s += 0.02
    return s, f"{match['name']} {pct:+.1f}%（榜第{rank}）"


def score_multifactor(fetcher, symbol: str, valuation: Optional[dict] = None,
                      kline_factors: Optional[dict] = None,
                      industry: Optional[str] = None) -> dict:
    """对单只股票做多维综合评分，返回各维度明细 + 合计加分 multi_bonus。"""
    val = valuation if valuation is not None else _safe(lambda: fetcher.get_valuation_metrics(symbol), {})
    kf = kline_factors or {}

    pe, pb = val.get("pe"), val.get("pb")
    roe = val.get("roe")
    profit_g = val.get("profit_growth")
    rev_g = val.get("revenue_growth")

    north = _safe(lambda: fetcher.get_north_hold(symbol), {})
    lhb_map = _safe(lambda: fetcher.get_lhb_map(), {})
    lhb = lhb_map.get(symbol, {})
    news = _safe(lambda: fetcher.get_news(symbol, limit=8), [])

    # 各维度原始分
    s_val, n_val = _score_valuation(pe, pb)
    s_perf, n_perf = _score_performance(roe, profit_g, rev_g)
    s_north, n_north = _score_northbound(north)
    s_lhb, n_lhb = _score_lhb(lhb)
    s_cat, n_cat, cat_hits = _score_catalyst(news)
    s_ind, n_ind = _score_industry(fetcher, industry)
    vol_ratio = kf.get("volume_ratio")
    s_vol = 0.02 if (vol_ratio and 1.2 <= vol_ratio <= 3) else 0.0

    # 应用维度权重系数（业绩/利好/行业景气度已调高）
    W = DIM_WEIGHTS
    s_val_w = s_val * W["valuation"]
    s_perf_w = s_perf * W["performance"]
    s_north_w = s_north * W["northbound"]
    s_lhb_w = s_lhb * W["lhb"]
    s_cat_w = s_cat * W["catalyst"]
    s_ind_w = s_ind * W["industry"]
    s_vol_w = s_vol * W["volume"]

    bonus = _clip(s_val_w + s_perf_w + s_north_w + s_lhb_w + s_cat_w + s_ind_w + s_vol_w,
                  -0.4, 0.5)

    return {
        "multi_bonus": round(bonus, 4),
        "weights": W,
        "valuation": {"pe": pe, "pb": pb, "score": round(s_val_w, 3), "note": n_val},
        "performance": {"roe": roe, "profit_growth": profit_g, "revenue_growth": rev_g,
                        "score": round(s_perf_w, 3), "note": n_perf, "weight": W["performance"]},
        "northbound": {**north, "score": round(s_north_w, 3), "note": n_north},
        "lhb": {**lhb, "score": round(s_lhb_w, 3), "note": n_lhb},
        "industry": {"score": round(s_ind_w, 3), "note": n_ind, "weight": W["industry"]},
        "volume": {"ratio": vol_ratio, "score": round(s_vol_w, 3)},
        "catalyst": {"score": round(s_cat_w, 3), "note": n_cat, "highlights": cat_hits,
                     "weight": W["catalyst"]},
    }


def _safe(fn, default):
    try:
        r = fn()
        return r if r is not None else default
    except Exception:
        return default
