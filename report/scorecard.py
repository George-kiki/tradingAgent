"""基本面评分卡（移植自 GitHub stock-analysis skill 的指标体系）。

为每个指标套用行业基准阈值，给出 优/良/中/差 评估，并汇总总体评级。
参考：claude-office-skills/skills/stock-analysis （MIT）。
"""
from __future__ import annotations


def _assess(value, good, mid, higher_better=True, fmt="{:.2f}"):
    """返回 (显示值, 等级, 基准说明)。等级 ∈ good/mid/bad/na。"""
    if value is None:
        return "—", "na", ""
    try:
        v = float(value)
    except Exception:
        return str(value), "na", ""
    disp = fmt.format(v)
    if higher_better:
        bench = f"≥{good} 优 / ≥{mid} 良"
        level = "good" if v >= good else "mid" if v >= mid else "bad"
    else:
        bench = f"≤{good} 优 / ≤{mid} 良"
        level = "good" if v <= good else "mid" if v <= mid else "bad"
    return disp, level, bench


def build_scorecard(m: dict) -> dict:
    """根据原始指标 dict 构建分类评分卡。"""
    if not m:
        return {"available": False, "categories": [], "rating": "数据不足", "report_date": ""}

    cats = []

    # 估值（越低越好）
    valuation = [
        ("市盈率 P/E(TTM)", *_assess(m.get("pe"), 15, 25, higher_better=False)),
        ("市净率 P/B", *_assess(m.get("pb"), 1, 3, higher_better=False)),
        ("市销率 P/S", *_assess(m.get("ps"), 3, 6, higher_better=False)),
        ("股息率 %", *_assess(m.get("dv_ratio"), 3, 1, higher_better=True)),
    ]
    cats.append({"name": "估值", "rows": valuation})

    # 盈利能力（越高越好，均为百分比）
    profit = [
        ("ROE 净资产收益率 %", *_assess(m.get("roe"), 15, 8, higher_better=True)),
        ("销售毛利率 %", *_assess(m.get("gross_margin"), 40, 20, higher_better=True)),
        ("销售净利率 %", *_assess(m.get("net_margin"), 10, 5, higher_better=True)),
    ]
    cats.append({"name": "盈利能力", "rows": profit})

    # 成长性（越高越好）
    growth = [
        ("营收同比增长 %", *_assess(m.get("revenue_growth"), 10, 0, higher_better=True)),
        ("净利润同比增长 %", *_assess(m.get("profit_growth"), 15, 0, higher_better=True)),
    ]
    cats.append({"name": "成长性", "rows": growth})

    # 财务健康（资产负债率越低越好）
    health = [
        ("资产负债率 %", *_assess(m.get("debt_ratio"), 40, 60, higher_better=False)),
    ]
    cats.append({"name": "财务健康", "rows": health})

    # 汇总评级
    levels = [r[2] for c in cats for r in c["rows"] if r[2] in ("good", "mid", "bad")]
    good_n = levels.count("good")
    bad_n = levels.count("bad")
    total = len(levels)
    if total == 0:
        rating = "数据不足"
    elif good_n >= total * 0.5:
        rating = "📈 基本面优秀"
    elif bad_n >= total * 0.5:
        rating = "📉 基本面偏弱"
    else:
        rating = "➖ 基本面中性"

    return {
        "available": total > 0,
        "categories": [
            {"name": c["name"],
             "rows": [{"metric": r[0], "value": r[1], "level": r[2], "benchmark": r[3]} for r in c["rows"]]}
            for c in cats
        ],
        "rating": rating,
        "good_count": good_n,
        "bad_count": bad_n,
        "total": total,
        "report_date": m.get("report_date", ""),
    }
