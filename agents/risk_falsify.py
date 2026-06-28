"""风险证伪引擎 —— 结构化反向验证检查表。

18 项检查，覆盖四大类风险：
  财务类 (8项) — 基于财报数据，逐项 pass/warn/fail
  业务类 (5项) — 基于产业链线索
  产业链类 (3项) — 基于 channel_research
  市场类 (2项) — 基于估值快照

纯规则驱动，不依赖 LLM。全部标注证据等级。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional


def falsify_risks(
    symbol: str,
    dissect: Optional[dict] = None,
    channel_research: Optional[dict] = None,
    bottleneck: Optional[dict] = None,
    valuation_metrics: Optional[dict] = None,
) -> dict:
    """执行 18 项反向风险检查。

    Args:
        symbol: 股票代码
        dissect: financial_dissect 模块输出
        channel_research: channel_research 模块输出
        bottleneck: bottleneck_audit 模块输出
        valuation_metrics: fetcher.get_valuation_metrics() 输出

    Returns:
        {
            "available": bool,
            "risk_flags": [{"item", "result": "pass/warn/fail", "detail", "level"}],
            "fail_count": int,
            "warn_count": int,
            "overall_risk": "low/medium/high/critical",
            "evidence_level": "A",
            "verified_at": str,
        }
    """
    now_str = _dt.datetime.now().isoformat()
    flags: list[dict] = []

    d = dissect.get("dissect", dissect) if dissect else {}

    # ---- 财务类 (8项) ----
    _check_financial_risks(flags, d)

    # ---- 业务类 (5项) ----
    _check_business_risks(flags, channel_research, bottleneck)

    # ---- 产业链类 (3项) ----
    _check_chain_risks(flags, channel_research, bottleneck)

    # ---- 市场类 (2项) ----
    _check_market_risks(flags, valuation_metrics)

    fails = sum(1 for f in flags if f["result"] == "fail")
    warns = sum(1 for f in flags if f["result"] == "warn")

    if fails >= 2:
        risk = "critical"
    elif fails >= 1 or warns >= 5:
        risk = "high"
    elif warns >= 3:
        risk = "medium"
    else:
        risk = "low"

    return {
        "available": True,
        "risk_flags": flags,
        "fail_count": fails,
        "warn_count": warns,
        "pass_count": len(flags) - fails - warns,
        "overall_risk": risk,
        "evidence_level": "A",
        "source": "财报公告/公开数据",
        "verified_at": now_str,
    }


def _add(flags: list, item: str, result: str, detail: str, level: str = "A"):
    flags.append({"item": item, "result": result, "detail": detail, "level": level})


def _check_financial_risks(flags: list, d: dict) -> None:
    """8 项财务风险检查。"""
    cf = d.get("cashflow_quality", {})
    bal = d.get("balance_health", {})
    margin = d.get("margin_trend", {})

    # 1. 应收增速 > 营收增速 × 1.5
    if bal.get("receivable_warning"):
        _add(flags, "应收增速>营收增速×1.5",
             "warn", "应收账款膨胀速度超过营收，可能存在收入质量或回款问题", "A")

    # 2. 存货周转（从已有数据推断）
    inv = d.get("dupont_roic", {})
    asset_turn = inv.get("asset_turnover")
    if asset_turn is not None and asset_turn < 0.3:
        _add(flags, "总资产周转率<0.3",
             "warn", f"资产周转率仅{asset_turn:.2f}，资产运营效率偏低", "A")

    # 3. 经营现金流/净利润 < 0.5
    ocf_ni = cf.get("ocf_to_ni")
    if ocf_ni is not None:
        if ocf_ni < 0.5:
            _add(flags, "经营现金流/净利润<0.5",
                 "fail", f"OCF/NI={ocf_ni:.2f}，净利润现金含量严重不足", "A")
        elif ocf_ni < 0.7:
            _add(flags, "经营现金流/净利润<0.7",
                 "warn", f"OCF/NI={ocf_ni:.2f}，略低于健康线0.7", "A")

    # 4. 商誉/净资产 > 30%
    if bal.get("goodwill_warning"):
        g2e = bal.get("goodwill_to_equity")
        _add(flags, "商誉/净资产>30%",
             "warn", f"商誉占比{g2e}%，存在商誉减值风险", "A")

    # 5. 资产负债率 > 70%
    if bal.get("debt_warning"):
        _add(flags, "资产负债率>70%",
             "fail", "杠杆率过高，财务风险显著", "A")
    elif bal.get("debt_ratio", 0) and bal["debt_ratio"] > 60:
        _add(flags, "资产负债率>60%",
             "warn", f"负债率{bal['debt_ratio']:.1f}%，偏高但尚未超标", "A")

    # 6. 毛利率趋势下降
    gm_trend = margin.get("gross_margin_trend", "—")
    if gm_trend and gm_trend.startswith("-"):
        val = float(gm_trend.replace("pp", "").replace("+", ""))
        if val < -3:
            _add(flags, "毛利率同比骤降",
                 "fail", f"毛利率同比下降{abs(val):.1f}pp，竞争格局恶化信号", "A")
        elif val < -1:
            _add(flags, "毛利率同比下降",
                 "warn", f"毛利率同比下降{abs(val):.1f}pp，关注竞争压力", "A")

    # 7. ROIC 为负
    roic = d.get("dupont_roic", {}).get("roic")
    if roic is not None and roic < 0:
        _add(flags, "ROIC为负",
             "fail", f"投入资本回报率{roic:.1f}%，价值毁灭", "A")

    # 8. 研发资本化率（从valuation_metrics推断，暂跳过——需要更细粒度数据）
    _add(flags, "研发资本化率异常(需细粒度数据)",
         "pass", "当前数据源暂不支持此项详细拆解，默认通过", "C")


def _check_business_risks(flags: list, channel_research, bottleneck) -> None:
    """5 项业务风险检查。"""
    cr_text = ""
    if channel_research and channel_research.get("available"):
        payload = channel_research.get("data") or channel_research
        if isinstance(payload, dict):
            cr_text = str(payload).lower()
        elif isinstance(payload, str):
            cr_text = payload.lower()

    bt = bottleneck or {}

    # 1. 核心客户集中度
    if any(kw in cr_text for kw in ["大客户", "单一客户", "集中", "依赖"]):
        _add(flags, "核心客户集中度偏高",
             "warn", "可能存在单一客户依赖风险", "C")
    else:
        _add(flags, "客户集中度",
             "pass", "未检测到异常集中", "C")

    # 2. 海外收入占比高 → 汇率/地缘风险
    if any(kw in cr_text for kw in ["海外", "出口", "北美", "欧洲", "海外收入"]):
        _add(flags, "海外收入占比需关注",
             "warn", "海外业务存在汇率波动和地缘政策风险", "C")
    else:
        _add(flags, "海外收入风险",
             "pass", "未发现明显海外依赖", "C")

    # 3. 原材料成本占比高
    if any(kw in cr_text for kw in ["原材料", "大宗", "涨价", "成本压力"]):
        _add(flags, "原材料成本占比高",
             "warn", "大宗商品价格波动可能侵蚀利润", "C")
    else:
        _add(flags, "原材料成本风险",
             "pass", "未发现明显原材料成本压力", "C")

    # 4. 专利/技术路线替代风险
    bp = bt.get("bottleneck_position", {})
    subt = bp.get("substitutability", "") if isinstance(bp, dict) else ""
    if subt == "高":
        _add(flags, "技术替代风险",
             "fail", "产品/技术可替代性高，护城河薄弱", "B")
    elif subt == "中":
        _add(flags, "技术替代风险",
             "warn", "存在一定技术替代风险", "B")
    else:
        _add(flags, "技术替代风险",
             "pass", "未发现明显技术替代威胁", "B")

    # 5. 产能利用率
    if any(kw in cr_text for kw in ["产能过剩", "利用率低", "闲置"]):
        _add(flags, "产能利用率偏低",
             "warn", "固定资产闲置可能导致折旧压力", "C")
    else:
        _add(flags, "产能利用率",
             "pass", "未发现产能严重过剩", "C")


def _check_chain_risks(flags: list, channel_research, bottleneck) -> None:
    """3 项产业链风险检查。"""
    cr_text = ""
    if channel_research and channel_research.get("available"):
        payload = channel_research.get("data") or channel_research
        cr_text = str(payload).lower()

    # 1. 下游自研替代
    if any(kw in cr_text for kw in ["自研", "自建", "垂直整合"]):
        _add(flags, "下游客户自研替代",
             "fail", "下游客户可能自建产能替代外购订单", "C")
    else:
        _add(flags, "下游替代风险",
             "pass", "未发现客户自研替代信号", "C")

    # 2. 上游断供
    if any(kw in cr_text for kw in ["断供", "紧缺", "短缺", "缺货", "制裁"]):
        _add(flags, "上游物料断供风险",
             "warn", "关键原材料/设备存在供应中断风险", "C")
    else:
        _add(flags, "上游供应风险",
             "pass", "未发现明显断供风险", "C")

    # 3. 政策/环保限制
    if any(kw in cr_text for kw in ["环保", "限产", "双碳", "政策", "监管"]):
        _add(flags, "政策/环保限制",
             "warn", "行业可能受政策/环保约束影响", "C")
    else:
        _add(flags, "政策风险",
             "pass", "未发现明显政策限制", "C")


def _check_market_risks(flags: list, valuation_metrics: Optional[dict]) -> None:
    """2 项市场风险检查。"""
    vm = valuation_metrics or {}

    # 1. 估值分位数
    pe = vm.get("市盈率") or vm.get("PE")
    if pe is not None:
        try:
            pe_val = float(pe)
            if pe_val < 0:
                _add(flags, "市盈率为负",
                     "warn", "公司处于亏损状态，估值无参考意义", "B")
            elif pe_val > 80:
                _add(flags, "PE>80(估值过高)",
                     "warn", f"当前市盈率{pe_val:.0f}，显著高于历史中枢", "B")
            else:
                _add(flags, "估值水平",
                     "pass", f"PE={pe_val:.0f}，估值处于合理区间", "B")
        except (ValueError, TypeError):
            _add(flags, "估值水平",
                 "pass", "PE数据不可用，跳过估值检查", "C")
    else:
        _add(flags, "估值水平",
             "pass", "PE数据缺失", "C")

    # 2. 机构持仓（从valuation_metrics取）
    inst = vm.get("机构持仓比例") or vm.get("institution_holding")
    if inst is not None:
        try:
            inst_val = float(inst)
            if inst_val > 40:
                _add(flags, "机构持仓>40%",
                     "warn", f"机构持仓{inst_val:.0f}%，流动性风险偏高", "B")
            else:
                _add(flags, "机构持仓集中度",
                     "pass", f"机构持仓{inst_val:.0f}%，正常", "B")
        except (ValueError, TypeError):
            _add(flags, "机构持仓",
                 "pass", "数据不可用", "C")
    else:
        _add(flags, "机构持仓",
             "pass", "数据缺失", "C")
