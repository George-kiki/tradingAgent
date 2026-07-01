"""尾盘选股专属引擎 — 独立于通用框架。

=== 与通用选股的差异 ===

| 维度        | 通用(daily)        | 尾盘(tail)             |
|------------|-------------------|------------------------|
| 持仓        | 1-5天             | 隔夜(14:30→次日开盘)    |
| 核心风险    | 趋势反转           | 尾盘回落+次日跳空低开    |
| 关键指标    | 均线/MACD/RSI     | 午后动量/VWAP/抢筹      |
| 策略        | 趋势+回归+形态     | 资金流+尾盘动量(纯短线)  |
| 选股池      | 全市场             | 午后活跃股(量比>1.5)     |
| 风控        | 追高/板块/基本面   | 午后回落/低开风险/流动性  |

=== 专属指标 ===
1. 午后动量(PM)  — 13:00至今涨跌幅，判断尾盘是否在积极拉升
2. 分时强度(VWAP) — 当前价 > VWAP = 多头控盘，有利次日
3. 尾盘抢筹     — 14:00后成交量占全日比例>30%，大资金尾盘建仓信号
4. 午后回落幅度  — (日内最高-当前价)/日内最高，>3%拒绝
5. 尾盘15分钟趋势 — 14:30-14:45方向，用来判断收盘前最后一波
6. 筹码集中度   — 大单买入占比，过滤散户驱动的脉冲
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any

import pandas as pd

from core.indicators import add_all_indicators
from data.fetcher import get_fetcher
from recommend.engine import resolve_name

# ============================================================
# 配置
# ============================================================
TAIL_START = dt.time(13, 0)
TAIL_FORMAL_START = dt.time(14, 0)
TAIL_CAUTION = dt.time(14, 55)
TAIL_END = dt.time(15, 0)


def _num(v: Any, default: float = 0.0) -> float:
    if v is None: return default
    if isinstance(v, (int, float)): return float(v) if not math.isnan(v) else default
    try: return float(str(v).replace(",", "").replace("%", "").strip())
    except: return default


def _clip(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _eligible_tail(code: str) -> bool:
    """尾盘专属标的过滤：仅保留沪深主板 + 排除ST/退市。"""
    s = (code or "").strip().zfill(6)
    if len(s) != 6 or not s.isdigit(): return False
    if "ST" in s or "退" in s: return False
    return s.startswith(("600", "601", "603", "605", "000", "001", "002", "003",
                          "688", "300", "301"))  # 尾盘不排斥科创板/创业板


# ============================================================
# Stage 1: 午后活跃股池
# ============================================================
def build_afternoon_universe(fetcher, max_pct: float = 6.0, min_amount_yi: float = 0.8,
                              min_turnover: float = 2.0) -> list[dict]:
    """从全市场快照筛选午后活跃候选股。

    关键差异：保留科创板/创业板（尾盘20cm弹性标的），
    但涨幅上限从9.5%收紧到6%（防止追板），下限提高到-2%（弱股不碰）。
    """
    spot = fetcher.get_market_spot()
    if spot is None or spot.empty: return []

    code_c = _col(spot, "代码", "symbol", "code")
    name_c = _col(spot, "名称", "name")
    pct_c = _col(spot, "涨跌幅")
    price_c = _col(spot, "最新价", "最新", "现价")
    turn_c = _col(spot, "换手率", "换手")
    vr_c = _col(spot, "量比")
    amount_c = _col(spot, "成交额", "成交金额")
    mv_c = _col(spot, "总市值")
    main_c = _col(spot, "主力净流入", "主力净额")

    # 尾盘策略强依赖实时活跃度。缺少成交额/换手/量比时宁缺毋滥，
    # 避免用降级快照硬凑隔夜票。
    if not (code_c and pct_c and amount_c and turn_c and vr_c):
        return []

    rows = []
    for _, r in spot.iterrows():
        code = str(r.get(code_c, "")).strip().replace("sh", "").replace("sz", "").zfill(6)
        name = str(r.get(name_c, "")) if name_c else code
        if not _eligible_tail(code): continue

        pct = _num(r.get(pct_c))
        # 尾盘专属涨跌幅窗口：+0.5%~+6%（不追极高，不碰绿盘）
        if pct < 0.5 or pct > max_pct: continue

        amount = _num(r.get(amount_c))
        if amount < min_amount_yi * 1e8: continue

        turnover = _num(r.get(turn_c))
        if turnover < min_turnover: continue

        vr = _num(r.get(vr_c), 1.0)
        # 尾盘只做放量股（缩量盘尾不值得参与）
        if vr < 1.0: continue

        rows.append({
            "symbol": code, "name": name,
            "price": round(_num(r.get(price_c)), 2) if price_c else None,
            "pct_change": round(pct, 2),
            "amount_yi": round(amount / 1e8, 2) if amount else None,
            "turnover": round(turnover, 2) if turnover else None,
            "volume_ratio": round(vr, 2),
            "total_mv_yi": round(_num(r.get(mv_c)) / 1e8, 1) if mv_c else None,
            "spot_main_net": _num(r.get(main_c)) if main_c else 0.0,
        })

    # 排序：量比>换手>成交额，聚焦真正有资金参与的
    rows.sort(key=lambda x: (x["volume_ratio"] * 0.4 + (x.get("turnover") or 0) * 0.02 +
                              (x.get("amount_yi") or 0) * 0.01), reverse=True)
    return rows[:60]


# ============================================================
# Stage 2: 尾盘专属技术指标（替代日线MACD/均线）
# ============================================================
def compute_tail_indicators(fetcher, symbol: str, price: float) -> dict:
    """计算尾盘专属技术指标（基于日K线+估算分时特征）。

    无法获取真实分时线时，用日K线的高开低收估算：
    - 午后动量 ≈ 当日涨幅（上半场+下半场的合成）
    - VWAP偏离 ≈ (收盘-均价) / 均价，均价≈(高+低+收)/3
    - 尾盘抢筹 ≈ 量比>1.5 且 涨幅<5%（温和放量上涨=吸筹）
    - 午后回落 ≈ (高-收)/高
    """
    df = fetcher.get_kline(symbol, days=30)
    if df is None or df.empty or len(df) < 10:
        return {"score": 50, "indicators": {}, "note": "K线数据不足"}

    d = add_all_indicators(df).fillna(0)
    last = d.iloc[-1]
    close = float(last.get("close", 0) or 0)
    high = float(last.get("high", 0) or close)
    low = float(last.get("low", 0) or close)
    vol = float(last.get("volume", 0) or 0)
    vol_ma5 = float(d["volume"].tail(6).head(5).mean()) if "volume" in d else vol
    rsi = float(last.get("rsi", 50) or 50)

    # 1) 午后动量（用当日涨幅近似）
    pct_today = (close - float(d.iloc[-2]["close"])) / float(d.iloc[-2]["close"]) * 100 if len(d) >= 2 else 0
    pm_score = _clip(50 + pct_today * 15, 20, 90)

    # 2) VWAP偏离（用典型价格估算均价）
    typical = (high + low + close) / 3
    vwap_dev = (close - typical) / typical * 100 if typical > 0 else 0
    vwap_score = _clip(50 + vwap_dev * 25, 25, 85)  # 价在均价上方=多头控盘

    # 3) 尾盘抢筹信号（量比>1.5 + 涨幅温和 = 大资金尾盘建仓）
    vol_ratio = vol / vol_ma5 if vol_ma5 > 0 else 1.0
    is_tail_buying = vol_ratio > 1.5 and 0.5 < pct_today < 5
    tail_score = 75 if is_tail_buying else 55 if vol_ratio > 1.2 else 40

    # 4) 午后回落风险（日内高点回落幅度）
    retreat = (high - close) / high * 100 if high > 0 else 0
    retreat_ok = retreat < 2.5  # 日内高点回撤<2.5%才安全

    # 5) 关键均线位
    ma5 = float(last.get("ma5", 0) or 0)
    ma10 = float(last.get("ma10", 0) or 0)
    above_ma5 = close > ma5 if ma5 else True
    above_ma10 = close > ma10 if ma10 else True

    # 6) 近3日动量（防连涨透支）
    if len(d) >= 4:
        pct_3d = (close - float(d.iloc[-4]["close"])) / float(d.iloc[-4]["close"]) * 100
    else:
        pct_3d = pct_today
    overbought = pct_3d > 12  # 3日涨超12%=透支

    # 综合评分
    score = round(
        pm_score * 0.25 +           # 午后动量
        vwap_score * 0.25 +         # VWAP
        tail_score * 0.30 +         # 尾盘抢筹（权重最高）
        (60 if above_ma5 else 35) * 0.10 +
        (55 if above_ma10 else 40) * 0.10,
        1
    )
    if retreat > 3 or overbought:
        score = min(score, 50)  # 回落过大或连涨透支=硬上限50分

    return {
        "score": score,
        "indicators": {
            "pm_score": pm_score, "vwap_score": vwap_score,
            "tail_buying_score": tail_score, "retreat_pct": round(retreat, 1),
            "vol_ratio": round(vol_ratio, 2), "pct_3d": round(pct_3d, 1),
            "rsi": round(rsi, 1),
        },
        "retreat_ok": retreat_ok and not overbought,
        "vwap_above": vwap_dev > 0,
        "is_tail_buying": is_tail_buying,
        "note": (f"午后动量{pm_score:.0f} VWAP{vwap_score:.0f} "
                 f"{'尾盘抢筹' if is_tail_buying else '尾盘温和'}"
                 f"{' ⚠️回落' + str(round(retreat,1)) + '%' if retreat > 2 else ''}"
                 f"{' ⚠️连涨透支' if overbought else ''}"),
    }


# ============================================================
# Stage 2b: 资金流分析（尾盘权重拉高，主力净流入是关键）
# ============================================================
def compute_tail_fund(item: dict) -> dict:
    """尾盘资金流：净流入即加分，强度按市值归一化。"""
    main_net = item.get("spot_main_net") or 0.0
    amount = item.get("amount_yi", 0) * 1e8 or 1e8
    mcap = (item.get("total_mv_yi") or 50) * 1e8 or 5e10

    # 主力净流入占成交额比例
    inflow_ratio = (main_net / amount * 100) if amount else 0
    # 主力净流入占总市值比例（大票归一化）
    intensity = (main_net / mcap * 10000) if mcap else 0  # 万分比

    # 评分：净流入幅度+强度
    main_score = _clip(50 + main_net / 1e8 * 20, 15, 90)
    ratio_score = _clip(50 + inflow_ratio * 8, 20, 85)
    intensity_score = _clip(50 + intensity * 50, 25, 80)

    score = round(main_score * 0.4 + ratio_score * 0.35 + intensity_score * 0.25, 1)

    return {
        "score": score,
        "main_net": round(main_net / 1e8, 2),
        "inflow_ratio": round(inflow_ratio, 2),
        "intensity": round(intensity, 2),
        "is_inflow": main_net > 0,
        "note": f"主力净{'流入' if main_net>0 else '流出'}{abs(main_net/1e8):.2f}亿 | 占成交{inflow_ratio:.1f}%",
    }


# ============================================================
# Stage 3: 午后回落风控
# ============================================================
def compute_tail_risk(item: dict, tech: dict, fund: dict, market: dict) -> dict:
    """尾盘专属风控（不同于通用追高/板块集中度）。

    核心检查：
    1. 午后回落幅度 > 2.5% → 尾盘有资金出货
    2. 主力净流出 → 不支持隔夜持仓
    3. 市场情绪<45 → 次日低开概率大
    4. RSI>80 → 日内过热，尾盘易跳水
    """
    flags = []
    penalty = 0.0

    ind = tech.get("indicators") or {}
    retreat = ind.get("retreat_pct", 0)

    # 1) 午后回落检查
    if retreat > 3:
        penalty += 15
        flags.append(f"午后回落{retreat:.1f}%，尾盘资金出货信号")
    elif retreat > 2.5:
        penalty += 8
        flags.append(f"午后回落{retreat:.1f}%，注意尾盘下行风险")

    # 2) 主力资金检查（尾盘核心）
    if not fund.get("is_inflow"):
        penalty += 12
        flags.append("主力资金净流出，不支持尾盘买入")
    elif fund.get("inflow_ratio", 0) < 0.5:
        penalty += 4
        flags.append("主力净流入占比偏低")

    # 3) 市场情绪
    if market.get("score", 50) < 45:
        penalty += 10
        flags.append("市场情绪偏弱，次日低开风险高")

    # 4) RSI过热
    if ind.get("rsi", 50) > 80:
        penalty += 8
        flags.append("RSI>80，日内过热易尾盘跳水")

    # 5) 3日连涨透支
    if ind.get("pct_3d", 0) > 12:
        penalty += 6
        flags.append(f"近3日涨{ind['pct_3d']:.0f}%，短线透支")

    # 资金流数据缺失时不硬拦（Sina等备用源无资金流字段）
    fund_data_available = fund.get("main_net", 0) != 0 or fund.get("inflow_ratio", 0) != 0
    if not fund_data_available:
        can_buy = penalty < 25  # 无资金流数据时放宽
    else:
        can_buy = penalty < 22 and all(f not in " ".join(flags) for f in
                                        ["主力资金净流出", "市场情绪偏弱"])
    return {"penalty": round(penalty, 1), "flags": flags, "can_buy": can_buy}


# ============================================================
# Stage 4: 市场情绪（继承现有逻辑，调低权重）
# ============================================================
def compute_tail_market(fetcher) -> dict:
    """尾盘市场情绪（权重从40%降至25%）。"""
    breadth = fetcher.get_market_breadth() or {}
    indices = fetcher.get_index_spot() or []
    sectors = fetcher.get_hot_sectors(limit=6) or []

    up = breadth.get("up") or 0
    down = breadth.get("down") or 0
    total = max(up + down + (breadth.get("flat") or 0), 1)
    up_ratio = up / total
    avg_pct = _num(breadth.get("avg_pct"))
    limit_up = breadth.get("limit_up") or 0
    limit_down = breadth.get("limit_down") or 0
    idx_pct = [i.get("pct") for i in indices if isinstance(i.get("pct"), (int, float))]
    idx_avg = sum(idx_pct) / len(idx_pct) if idx_pct else 0

    trend_score = _clip(50 + idx_avg * 12)
    breadth_score = _clip(up_ratio * 100 + avg_pct * 8)
    limit_score = _clip(50 + (limit_up - limit_down * 2) * 0.8)
    sector_score = _clip(50 + max([_num(s.get("pct", s.get("day_pct", 0))) for s in sectors] or [0]) * 5)
    score = round(trend_score * 0.3 + breadth_score * 0.25 + limit_score * 0.2 + sector_score * 0.25, 1)
    temp = "强" if score >= 75 else "中强" if score >= 60 else "偏弱" if score >= 45 else "弱"

    return {
        "score": score, "temperature": temp,
        "note": f"指数均值{idx_avg:+.2f}% 上涨占比{up_ratio*100:.0f}% 涨停{limit_up}/跌停{limit_down} 情绪{temp}",
        "indices": indices, "main_themes": sectors[:6],
        "limit_up": limit_up, "limit_down": limit_down,
    }


# ============================================================
# 主引擎
# ============================================================
class TailEngine:
    """尾盘专属选股引擎（完全独立于通用框架）。"""

    def __init__(self):
        self.fetcher = get_fetcher()

    def run(self, count: int = 5, max_pct: float = 6.0, force: bool = False,
            min_amount_yi: float = 0.8, min_turnover: float = 2.0) -> dict:
        """执行完整尾盘选股流程。

        Returns: {as_of, picks, market, engine: "tail-dedicated", ...}
        """
        from data.cache import invalidate_cache
        # 强制清缓存，确保14:30实时数据
        for key in ("spot:all:v2", "spot:all:v3", "ts_spot_all", "as_spot_all",
                     "index_spot", "as_sectors:10", "as_sectors:5",
                     "as_sectors_snapshot", "sectors:10"):
            invalidate_cache(key)

        now = dt.datetime.now()
        t = now.time()
        if not force:
            in_window = TAIL_START <= t <= TAIL_END
            if not in_window:
                phase = "closed" if t > TAIL_END else "before"
                return {"error": f"尾盘窗口未开放(13:00-15:00)，当前{phase}", "phase": phase}

        # Stage 1: 午后活跃股池
        print("[尾盘] 1/4 午后股池...")
        candidates = build_afternoon_universe(
            self.fetcher, max_pct=max_pct,
            min_amount_yi=min_amount_yi, min_turnover=min_turnover)
        source = getattr(self.fetcher, "last_market_spot_source", "未知")
        stale = bool(getattr(self.fetcher, "last_market_spot_stale", False))
        if stale:
            return {
                "engine": "tail-dedicated",
                "as_of": now.strftime("%Y-%m-%d %H:%M"),
                "strategy": "尾盘14:30买入，次日早盘卖出（专属引擎）",
                "data_source": source,
                "market": {},
                "weights": {"fund": 0.40, "tail_tech": 0.35, "market": 0.25},
                "filters": {"max_pct": max_pct, "min_amount_yi": min_amount_yi,
                            "min_turnover": min_turnover},
                "candidates_count": 0,
                "picks": [],
                "fund_chart": {"symbols": [], "series": [], "ratio": [], "unit": "亿元"},
                "risk_note": "行情快照为缓存兜底，尾盘策略要求实时数据，今日不强行推荐。",
            }

        # Stage 2: 市场情绪
        print("[尾盘] 2/4 市场情绪...")
        market = compute_tail_market(self.fetcher)
        if market.get("score", 50) < 45:
            return {
                "engine": "tail-dedicated",
                "as_of": now.strftime("%Y-%m-%d %H:%M"),
                "strategy": "尾盘14:30买入，次日早盘卖出（专属引擎）",
                "data_source": source,
                "market": market,
                "weights": {"fund": 0.40, "tail_tech": 0.35, "market": 0.25},
                "filters": {"max_pct": max_pct, "min_amount_yi": min_amount_yi,
                            "min_turnover": min_turnover},
                "candidates_count": len(candidates),
                "picks": [],
                "fund_chart": {"symbols": [], "series": [], "ratio": [], "unit": "亿元"},
                "risk_note": "市场情绪偏弱，尾盘隔夜低开风险高，今日不强行推荐。",
            }

        # Stage 3: 逐股分析
        print(f"[尾盘] 3/4 逐股技术+资金 ({len(candidates[:20])}只)...")
        out = []
        for item in candidates[:20]:
            item["name"] = item.get("name") or resolve_name(self.fetcher, item["symbol"])
            tech = compute_tail_indicators(self.fetcher, item["symbol"], item.get("price", 0))
            fund = compute_tail_fund(item)
            risk = compute_tail_risk(item, tech, fund, market)
            if not risk["can_buy"]: continue

            # 尾盘专属权重：资金流40% + 技术35% + 市场25%
            final = round(
                fund["score"] * 0.40 +
                tech["score"] * 0.35 +
                market["score"] * 0.25 -
                risk["penalty"] * 0.5,
                1
            )
            # 卖点/止损
            buy_plan = "14:30后站稳分时均价可小仓试买，不放量不追"
            sell_plan = "次日高开+2%~+4%分批止盈；低开30分钟不翻红止损"
            VWAP_DESC = "分时均线破位" if tech.get("vwap_above") else "持仓成本线"
            stop_loss = f"跌破买入价2%或{VWAP_DESC}"

            if final < 58:
                continue

            out.append({
                **item,
                "score": final,
                "entry_price": item.get("price"),
                "fund": fund,
                "tech_score": tech["score"],
                "tech_note": tech.get("note", ""),
                "risk_penalty": risk["penalty"],
                "risk_flags": risk["flags"],
                "buy_plan": buy_plan,
                "sell_plan": sell_plan,
                "stop_loss": stop_loss,
                "reason": f"{tech.get('note','')} | {fund.get('note','')} | {market.get('note','')}",
                "market_score": round(market.get("score", 50), 1),
                "fund_score": round(fund.get("score", 50), 1),
            })

        out.sort(key=lambda x: x["score"], reverse=True)
        picks = out[:count]

        # Fund chart
        fund_chart = {
            "symbols": [f"{p['name']}\n{p['symbol']}" for p in picks],
            "series": [{"name": "主力净流入", "data": [p["fund"]["main_net"] for p in picks]}],
            "ratio": [p["fund"].get("inflow_ratio") for p in picks],
            "unit": "亿元",
        }

        return {
            "engine": "tail-dedicated",
            "as_of": now.strftime("%Y-%m-%d %H:%M"),
            "strategy": "尾盘14:30买入，次日早盘卖出（专属引擎）",
            "data_source": source,
            "market": market,
            "weights": {"fund": 0.40, "tail_tech": 0.35, "market": 0.25},
            "filters": {"max_pct": max_pct, "min_amount_yi": min_amount_yi,
                        "min_turnover": min_turnover},
            "candidates_count": len(candidates),
            "picks": picks,
            "fund_chart": fund_chart,
        }


# ============================================================
# 辅助
# ============================================================
def _col(df: pd.DataFrame, *names: str) -> str | None:
    for n in names:
        if n in df.columns: return n
    for c in df.columns:
        if any(n in str(c) for n in names): return c
    return None
