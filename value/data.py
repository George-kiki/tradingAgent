"""价值挖掘数据层：为「财务拐点穿透」步骤注入真实财务数据。

- 毛利率季度趋势（判断供需失衡导致的毛利率拐点）
- CapEx / 在建工程趋势（判断是否秘密爬坡承接未来需求）
- 标的市值 / 估值校验（锁定 30-150 亿中小盘）

所有取数 best-effort，限流/缺失时返回空，由上层 LLM 做定性推断并标注数据缺口。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from data.cache import cached_call
from data.fetcher import get_fetcher, normalize_symbol

try:
    import akshare as ak
except Exception:  # pragma: no cover
    ak = None


def _date_cols(df: pd.DataFrame) -> list[str]:
    return sorted([c for c in df.columns if c not in ("选项", "指标") and str(c).isdigit()],
                  reverse=True)


def gross_margin_trend(symbol: str, periods: int = 6) -> list[dict]:
    """近 N 期销售毛利率(%)趋势（升序返回，便于看拐点）。来自财务摘要，较稳。"""
    f = get_fetcher()
    fa = f.get_financials(symbol)
    if fa is None or fa.empty or "指标" not in fa.columns:
        return []
    cols = _date_cols(fa)[:periods]
    if not cols:
        return []
    mask = fa["指标"].astype(str).str.contains("毛利率")
    if not mask.any():
        return []
    row = fa[mask].iloc[0]
    out = []
    for c in sorted(cols):  # 升序
        try:
            v = float(pd.to_numeric(row[c], errors="coerce"))
            if v == v:  # 非 NaN
                out.append({"period": str(c), "gross_margin": round(v, 2)})
        except Exception:
            continue
    return out


def metric_trend(symbol: str, keywords: tuple[str, ...], periods: int = 6) -> list[dict]:
    """通用财务摘要指标趋势（升序）。如净利率、ROE、营收等。"""
    f = get_fetcher()
    fa = f.get_financials(symbol)
    if fa is None or fa.empty or "指标" not in fa.columns:
        return []
    cols = _date_cols(fa)[:periods]
    if not cols:
        return []
    mask = fa["指标"].astype(str).apply(lambda s: any(k in s for k in keywords))
    if not mask.any():
        return []
    row = fa[mask].iloc[0]
    out = []
    for c in sorted(cols):
        try:
            v = float(pd.to_numeric(row[c], errors="coerce"))
            if v == v:
                out.append({"period": str(c), "value": round(v, 2)})
        except Exception:
            continue
    return out


def capex_trend(symbol: str, periods: int = 5) -> list[dict]:
    """CapEx 代理趋势：优先现金流量表「购建固定资产无形资产...支付的现金」，
    取不到则用资产负债表「在建工程」作为扩产爬坡代理。单位：亿元（升序）。
    """
    if ak is None:
        return []
    sym = normalize_symbol(symbol)
    em = ("SH" if sym.startswith("6") else "SZ") + sym
    key = f"capex:{sym}"

    def _extract(df: pd.DataFrame, keys: tuple[str, ...]) -> list[dict]:
        if df is None or df.empty:
            return []
        # 找日期列（报告期），找目标科目行
        date_col = next((c for c in df.columns if "报告" in c or "日期" in c or c.upper() == "REPORT_DATE"), None)
        item_col = next((c for c in df.columns if c in ("项目", "科目", "ITEM")), None)
        # em 接口通常是「宽表」：每行一个报告期，列为各科目
        target_cols = [c for c in df.columns if any(k in str(c) for k in keys)]
        rows = []
        if target_cols and date_col:
            tcol = target_cols[0]
            sub = df[[date_col, tcol]].dropna()
            for _, r in sub.iterrows():
                try:
                    v = float(pd.to_numeric(r[tcol], errors="coerce"))
                    if v == v:
                        rows.append({"period": str(r[date_col])[:10], "capex": round(v / 1e8, 2)})
                except Exception:
                    continue
        elif item_col:  # 长表
            mask = df[item_col].astype(str).apply(lambda s: any(k in s for k in keys))
            if mask.any():
                row = df[mask].iloc[0]
                for c in df.columns:
                    if c == item_col:
                        continue
                    try:
                        v = float(pd.to_numeric(row[c], errors="coerce"))
                        if v == v:
                            rows.append({"period": str(c)[:10], "capex": round(v / 1e8, 2)})
                    except Exception:
                        continue
        # 去重 + 升序 + 取近 periods
        seen, uniq = set(), []
        for x in sorted(rows, key=lambda d: d["period"]):
            if x["period"] not in seen:
                seen.add(x["period"])
                uniq.append(x)
        return uniq[-periods:]

    def _fetch():
        # 现金流量表（资本开支）
        for caller in (
            lambda: ak.stock_cash_flow_sheet_by_report_em(symbol=em),
            lambda: ak.stock_financial_report_sina(stock=em, symbol="现金流量表"),
        ):
            try:
                df = caller()
                rows = _extract(df, ("购建固定资产", "资本开支", "购置固定"))
                if rows:
                    return {"kind": "capex", "series": rows}
            except Exception:
                continue
        # 资产负债表（在建工程，作为扩产爬坡代理）
        for caller in (
            lambda: ak.stock_balance_sheet_by_report_em(symbol=em),
            lambda: ak.stock_financial_report_sina(stock=em, symbol="资产负债表"),
        ):
            try:
                df = caller()
                rows = _extract(df, ("在建工程",))
                if rows:
                    return {"kind": "cip", "series": rows}
            except Exception:
                continue
        return None

    res = cached_call(key, _fetch, ttl=86400)
    return res or {}


def target_snapshot(symbol: str) -> dict:
    """标的基础校验：名称、总市值(亿)、PE/PB、毛利率趋势、CapEx 趋势。"""
    sym = normalize_symbol(symbol)
    f = get_fetcher()
    snap: dict = {"symbol": sym}
    try:
        snap["name"] = f.get_name(sym)
    except Exception:
        snap["name"] = sym
    try:
        m = f.get_valuation_metrics(sym)
        snap["pe"] = m.get("pe")
        snap["pb"] = m.get("pb")
        snap["roe"] = m.get("roe")
        snap["revenue_growth"] = m.get("revenue_growth")
        snap["profit_growth"] = m.get("profit_growth")
        mv = m.get("total_mv")
        snap["market_cap_yi"] = round(float(mv) / 1e8, 1) if mv else None
    except Exception:
        pass
    # 市值兜底：基本信息
    if not snap.get("market_cap_yi"):
        try:
            info = f.get_basic_info(sym)
            mv = info.get("总市值")
            if mv:
                snap["market_cap_yi"] = round(float(mv) / 1e8, 1)
        except Exception:
            pass
    snap["gross_margin_trend"] = gross_margin_trend(sym)
    snap["net_margin_trend"] = metric_trend(sym, ("销售净利率", "净利率"))
    snap["capex"] = capex_trend(sym)
    return snap


def validate_targets(targets: list[dict],
                     mc_min: float = 30, mc_max: float = 150) -> list[dict]:
    """校验候选标的：补全市值/名称，标注是否落在 30-150 亿区间。"""
    out = []
    f = get_fetcher()
    for t in targets or []:
        sym = str(t.get("symbol") or "").strip()
        sym = normalize_symbol(sym) if sym and sym[:1].isdigit() else sym
        item = dict(t)
        if sym and sym.isdigit() and len(sym) == 6:
            item["symbol"] = sym
            try:
                m = f.get_valuation_metrics(sym)
                mv = m.get("total_mv")
                mc = round(float(mv) / 1e8, 1) if mv else None
                if not mc:
                    info = f.get_basic_info(sym)
                    mv = info.get("总市值")
                    mc = round(float(mv) / 1e8, 1) if mv else None
                item["market_cap_yi"] = mc
                item["in_cap_range"] = (mc is not None and mc_min <= mc <= mc_max)
                nm = f.get_name(sym)
                if nm and nm != sym:
                    item["name"] = nm
            except Exception:
                item["market_cap_yi"] = None
                item["in_cap_range"] = None
        else:
            item["symbol"] = sym
            item["market_cap_yi"] = None
            item["in_cap_range"] = None
        out.append(item)
    return out


def fmt_trend(series: list[dict], key: str, unit: str = "%") -> str:
    """趋势序列转紧凑文本，供 LLM 阅读。"""
    if not series:
        return "（数据暂缺）"
    parts = []
    for x in series:
        v = x.get(key)
        p = str(x.get("period", ""))[:8]
        parts.append(f"{p}:{v}{unit}")
    return " → ".join(parts)
