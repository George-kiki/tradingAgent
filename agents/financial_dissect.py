"""财报深度拆解与个股分级（纯规则驱动，不依赖 LLM）。

从 fetcher.get_financials() 返回的 DataFrame 中提取关键指标，
做 DuPont ROIC 拆解、现金流质量判断、利润率趋势，并输出 A/B/C/D 分级。

证据等级：A（全部基于财报公告实锤数据）
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

import pandas as pd


# ---------- 分级阈值 ----------
_GRADE_A_ROIC = 15.0       # A 级：ROIC ≥ 15%
_GRADE_BPLUS_ROIC = 10.0   # B+ 级：ROIC ≥ 10%
_GRADE_B_ROIC = 8.0        # B  级：ROIC ≥ 8%
_GRADE_C_ROIC = 0.0        # C  级：ROIC ≥ 0%

# 现金流质量：经营现金流/净利润 ≥ 0.7 视为健康
_OCF_NI_HEALTHY = 0.7
# 毛利率趋势：最近一季度 vs 一年前同期，上升 +1pp 视为改善
_MARGIN_IMPROVE_PP = 1.0


def _num(v: Any) -> Optional[float]:
    """安全数值转换，NaN/Inf 返回 None。"""
    if v is None:
        return None
    try:
        f = float(v)
        import math
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _col_at(df: pd.DataFrame, indicator: str, col: str) -> Optional[float]:
    """从 finance DataFrame 中按 指标名(支持包含匹配) + 列名 取值。"""
    # 先尝试精确匹配
    row = df[df["指标"] == indicator]
    # 再尝试包含匹配（如 "ROE" 匹配 "净资产收益率(ROE)"）
    if row.empty:
        row = df[df["指标"].str.contains(indicator, na=False, regex=False)]
    if row.empty or col not in df.columns:
        return None
    return _num(row.iloc[0][col])


def _latest_quarter_cols(df: pd.DataFrame) -> list[str]:
    """返回最近 4 个季度的列名（YYYYMMDD 格式字符串）。"""
    date_cols = [c for c in df.columns if isinstance(c, str) and c.isdigit() and len(c) == 8]
    date_cols.sort(reverse=True)
    return date_cols[:4]


def _yoy_col(df: pd.DataFrame, latest_col: str) -> Optional[str]:
    """返回 latest_col 对应一年前同期的列名。"""
    try:
        y = int(latest_col[:4]) - 1
        m = latest_col[4:]
        target = f"{y}{m}"
        if target in df.columns:
            return target
        # 回退：找最近的不超过 target 的列
        for c in sorted(df.columns, reverse=True):
            if isinstance(c, str) and c.isdigit() and c <= target:
                return c
    except Exception:
        pass
    return None


def dissect_financials(
    fetcher,
    symbol: str,
    name: str = "",
    llm=None,
) -> dict:
    """执行财报深度拆解，输出结构化结果 + 分级。

    Args:
        fetcher: data.fetcher 实例
        symbol: 股票代码
        name: 股票名称
        llm: 未使用（保留接口兼容）

    Returns:
        {
            "available": bool,
            "dissect": {
                "margin_trend": {...},
                "cashflow_quality": {...},
                "dupont_roic": {...},
                "balance_health": {...},
            },
            "grading": "A"|"B+"|"B"|"C"|"D",
            "grading_score": 0-100,
            "grading_reason": str,
            "evidence_level": "A",
            "source": "财报公告(东方财富)",
            "verified_at": str,
        }
    """
    now_str = _dt.datetime.now().isoformat()
    base = {
        "available": False,
        "dissect": {},
        "grading": "D",
        "grading_score": 30,
        "grading_reason": "",
        "evidence_level": "A",
        "source": "财报公告(东方财富)",
        "verified_at": now_str,
    }

    try:
        df = fetcher.get_financials(symbol)
    except Exception:
        base["grading_reason"] = "无法获取财报数据"
        return base

    if df is None or df.empty:
        base["grading_reason"] = "财报数据为空"
        return base

    cols = _latest_quarter_cols(df)
    if len(cols) < 2:
        base["grading_reason"] = "财报数据不足两期"
        return base

    latest = cols[0]
    prev_q = cols[1]
    yoy = _yoy_col(df, latest)

    # ---- 1. 利润率趋势 ----
    gross_margin = _col_at(df, "毛利率", latest)
    gross_margin_prev = _col_at(df, "毛利率", prev_q)
    gross_margin_yoy = _col_at(df, "毛利率", yoy) if yoy else None

    net_margin = _col_at(df, "净利率", latest)
    net_margin_prev = _col_at(df, "净利率", prev_q)
    net_margin_yoy = _col_at(df, "净利率", yoy) if yoy else None

    margin = {
        "gross_margin": gross_margin,
        "gross_margin_prev_q": gross_margin_prev,
        "gross_margin_yoy": gross_margin_yoy,
        "gross_margin_trend": "—",
        "net_margin": net_margin,
        "net_margin_prev_q": net_margin_prev,
        "net_margin_yoy": net_margin_yoy,
        "net_margin_trend": "—",
    }
    if gross_margin_yoy is not None and gross_margin is not None:
        diff = gross_margin - gross_margin_yoy
        margin["gross_margin_trend"] = f"{diff:+.1f}pp"
    if net_margin_yoy is not None and net_margin is not None:
        diff = net_margin - net_margin_yoy
        margin["net_margin_trend"] = f"{diff:+.1f}pp"

    # ---- 2. 现金流质量 ----
    net_income = _col_at(df, "净利润", latest)
    ocf = _col_at(df, "经营现金流", latest)

    # 优先用绝对值列，其次用每股相对值
    ni_abs = _col_at(df, "净利润(元)", latest)
    ocf_abs = _col_at(df, "经营活动产生的现金流量净额(元)", latest)
    if ni_abs is None:
        # 尝试从「常用指标」选项中找可比口径
        ni_abs = net_income
    if ocf_abs is None:
        ocf_abs = ocf

    ocf_ni_ratio = None
    if ocf_abs is not None and ni_abs is not None and ni_abs != 0:
        ocf_ni_ratio = round(ocf_abs / ni_abs, 3)

    total_asset = _col_at(df, "总资产", latest)
    fcf_yield = None
    capex_ratio = None
    if ocf_abs is not None and total_asset is not None and total_asset > 0:
        capex_ratio = round(ocf_abs / total_asset, 4)

    cashflow = {
        "ocf": ocf,
        "net_income": net_income,
        "ocf_to_ni": ocf_ni_ratio,
        "ocf_healthy": ocf_ni_ratio >= _OCF_NI_HEALTHY if ocf_ni_ratio else None,
        "capex_ratio": capex_ratio,
    }

    # ---- 3. DuPont ROIC 拆解 ----
    roe = _col_at(df, "ROE", latest)
    roe_annualized = None
    if roe is not None:
        # ROE 可能是单季*4 或 TTM，保守不调整
        roe_annualized = round(roe, 2)

    asset_turnover = _col_at(df, "总资产周转率", latest)
    equity_multiplier = _col_at(df, "权益乘数", latest)
    net_margin_pct = net_margin
    if net_margin_pct and asset_turnover and equity_multiplier:
        roe_dupont = round(net_margin_pct * asset_turnover * equity_multiplier, 2)
    else:
        roe_dupont = roe_annualized

    # ROIC 近似 = ROE / equity_multiplier (粗略，非精确 WACC 计算)
    roic = None
    if roe_annualized and equity_multiplier and equity_multiplier > 0:
        roic = round(roe_annualized / equity_multiplier, 2)

    dupont = {
        "roic": roic,
        "roe": roe_annualized,
        "net_margin": net_margin_pct,
        "asset_turnover": asset_turnover,
        "equity_multiplier": equity_multiplier,
        "roe_dupont": roe_dupont,
    }

    # ---- 4. 资产负债表健康度 ----
    receivable = _col_at(df, "应收账款", latest)
    inventory = _col_at(df, "存货", latest)
    goodwill = _col_at(df, "商誉", latest)
    net_equity = _col_at(df, "净资产", latest)
    debt_ratio = _col_at(df, "资产负债率", latest)

    # 应收增速 vs 营收增速
    rev_latest = _col_at(df, "营业收入", latest)
    rev_prev = _col_at(df, "营业收入", prev_q)
    rec_latest = _col_at(df, "应收账款", latest)
    rec_prev = _col_at(df, "应收账款", prev_q)

    rev_growth = None
    rec_growth = None
    receivable_warning = False
    if rev_latest and rev_prev and rev_prev != 0:
        rev_growth = round((rev_latest / rev_prev - 1) * 100, 1)
    if rec_latest and rec_prev and rec_prev != 0:
        rec_growth = round((rec_latest / rec_prev - 1) * 100, 1)
        # 应收增速 > 营收增速 × 1.5 → 收入质量存疑
        if rev_growth and rec_growth > rev_growth * 1.5:
            receivable_warning = True

    goodwill_warning = False
    if goodwill and net_equity and net_equity > 0:
        if goodwill / net_equity > 0.3:
            goodwill_warning = True

    debt_warning = False
    if debt_ratio and debt_ratio > 70:
        debt_warning = True

    balance = {
        "debt_ratio": debt_ratio,
        "receivable_growth": rec_growth,
        "revenue_growth": rev_growth,
        "goodwill_to_equity": round(goodwill / net_equity * 100, 1)
            if goodwill is not None and net_equity is not None and net_equity > 0 else None,
        "receivable_warning": receivable_warning,
        "goodwill_warning": goodwill_warning,
        "debt_warning": debt_warning,
    }

    # ---- 5. 分级判定 ----
    grade, grade_score, grade_reasons = _determine_grade(
        roic, ocf_ni_ratio, margin, receivable_warning, goodwill_warning, debt_warning)

    dissect = {
        "margin_trend": margin,
        "cashflow_quality": cashflow,
        "dupont_roic": dupont,
        "balance_health": balance,
        "data_period": latest,
    }

    return {
        "available": True,
        "dissect": dissect,
        "grading": grade,
        "grading_score": grade_score,
        "grading_reason": "; ".join(grade_reasons) if grade_reasons else "数据充分，各项指标正常",
        "evidence_level": "A",
        "source": "财报公告(东方财富)",
        "verified_at": now_str,
    }


def _determine_grade(
    roic: Optional[float],
    ocf_ni: Optional[float],
    margin: dict,
    receivable_warn: bool,
    goodwill_warn: bool,
    debt_warn: bool,
) -> tuple[str, int, list[str]]:
    """根据各项指标判定 A-D 级。"""
    reasons: list[str] = []

    # 负面信号扣分
    red_flags = 0
    if receivable_warn:
        red_flags += 1
        reasons.append("应收增速>营收增速×1.5(收入质量存疑)")
    if goodwill_warn:
        red_flags += 1
        reasons.append("商誉/净资产>30%(减值风险)")
    if debt_warn:
        red_flags += 1
        reasons.append("资产负债率>70%(高杠杆)")

    if ocf_ni is not None and ocf_ni < _OCF_NI_HEALTHY:
        red_flags += 1
        reasons.append(f"经营现金流/净利润={ocf_ni:.2f}<{_OCF_NI_HEALTHY}(利润含金量低)")

    # 正面积分
    if roic is None:
        return ("D", 30, reasons or ["ROIC数据缺失"])

    gm_trend_ok = False
    if margin.get("gross_margin_yoy") and margin.get("gross_margin"):
        gm_trend_ok = (margin["gross_margin"] - margin["gross_margin_yoy"]) >= _MARGIN_IMPROVE_PP

    ocf_ok = ocf_ni is not None and ocf_ni >= _OCF_NI_HEALTHY

    if roic >= _GRADE_A_ROIC and ocf_ok and gm_trend_ok and red_flags == 0:
        return ("A", 90, ["ROIC卓越+现金流健康+毛利率上升，全线达标"])
    elif roic >= _GRADE_BPLUS_ROIC:
        score = 78
        if ocf_ok:
            score += 5
        if gm_trend_ok:
            score += 5
        score = min(score - red_flags * 5, 85)
        return ("B+", score, reasons)
    elif roic >= _GRADE_B_ROIC:
        score = 65
        if ocf_ok:
            score += 4
        score = min(score - red_flags * 4, 75)
        return ("B", score, reasons)
    elif roic >= _GRADE_C_ROIC:
        score = 50 + (roic / (_GRADE_B_ROIC or 1)) * 10
        score = min(score - red_flags * 4, 60)
        return ("C", int(score), reasons)
    else:
        score = max(30 - red_flags * 5, 15)
        reasons.append(f"ROIC={roic:.1f}%<0(资本回报为负)")
        return ("D", int(score), reasons)
