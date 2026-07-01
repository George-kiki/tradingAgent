"""AI 协同尾盘荐股引擎 — 最大化隔夜胜率。

=== 现有缺陷分析 ===

1. 无真实分时数据 → VWAP/午后动量用日K线估算（偏差大）
2. 无订单簿数据 → 无法区分主力吸筹 vs 散户跟风
3. 阈值静态 → max_pct=6%/min_amount=0.8 亿不随市场变化
4. 无板块轮动跟踪 → 不知道今天资金从哪来了去哪
5. 无AI预测 → 纯规则打分，无法捕捉尾盘拉升的隐含信号

=== 本引擎改进（四层AI协同） ===

Layer 1: 市场环境感知 (LLM)     → 识别当前市场阶段，动态调整筛选阈值
Layer 2: 尾盘拉升概率预测 (ML)   → LightGBM预测14:55收盘前拉升>1%的概率
Layer 3: 个股深度审查 (LLM)      → 对候选股做基本面+消息面+技术面三堂会审
Layer 4: 风险量化评分 (Rules+ML) → 午后回落风险+次日开盘方向预测+止损计算

=== 策略执行流程 ===

14:30 触发 → 获取实时快照+日K线+分时数据
         → Layer1 LLM分析市场 → 动态阈值
         → 候选池生成（量价异动+板块轮动）
         → Layer2 ML预测尾盘拉升概率 → 排序
         → Layer3 LLM审查Top10 → 加减分
         → Layer4 风险评分 → 过滤
         → 输出精选Top5 + 买卖计划
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


# ============================================================
# 配置：动态阈值（默认值，会被 Layer1 LLM 覆盖）
# ============================================================
@dataclass
class TailConfig:
    """尾盘策略配置 — 动态阈值。"""
    count: int = 5
    max_pct: float = 6.0         # 最大涨幅（强趋势市→5.5, 震荡→6, 弱→4）
    min_pct: float = 0.5         # 最小涨幅
    min_amount_yi: float = 0.8   # 最低成交额亿（强→1.0, 弱→0.5）
    min_turnover: float = 2.0    # 最低换手率%
    min_volume_ratio: float = 1.0
    require_main_inflow: bool = True
    require_mainline: bool = False
    market_regime: str = "sideways"
    confidence_threshold: float = 0.55  # ML 拉升概率门槛


def _num(v, default: float = 0.0) -> float:
    if v is None: return default
    if isinstance(v, (int, float)): return float(v) if not math.isnan(v) else default
    try: return float(str(v).replace(",", "").replace("%", "").strip())
    except: return default


def _clip(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _eligible_tail(code: str) -> bool:
    s = (code or "").strip().zfill(6)
    if len(s) != 6 or not s.isdigit(): return False
    if "ST" in s: return False
    return s.startswith(("600","601","603","605","000","001","002","003",
                          "688","300","301"))


# ============================================================
# Layer 1: LLM 市场环境感知 → 动态阈值
# ============================================================

_LAYER1_SYSTEM = (
    "你是量化交易策略师。基于当前市场快照数据，"
    "判断市场所处阶段，并给出尾盘选股的最优参数配置。"
    "尾盘策略是：14:30买入涨幅温和的活跃股，次日开盘卖出。"
    "你必须输出JSON格式。"
)


def _build_layer1_prompt(indices, breadth, sectors, lhb_top) -> str:
    idx_s = "; ".join(f"{i['name']} {i.get('pct',0):+.1f}%" for i in (indices or [])[:5])
    b = breadth or {}
    sec_s = "; ".join(f"{s['name']}+{s.get('pct',s.get('day_pct',0)):.1f}%" for s in (sectors or [])[:8])
    lhb_s = "; ".join(f"{x.get('name','?')} 净买{x.get('net_buy',0):.1f}亿" for x in (lhb_top or [])[:5])

    return f"""今日市场快照(14:30):
指数: {idx_s}
广度: 涨{b.get('up','?')}/跌{b.get('down','?')} 涨停{b.get('limit_up','?')} 跌停{b.get('limit_down','?')}
板块TOP8: {sec_s}
龙虎榜: {lhb_s or '暂无'}

判断市场阶段（strong_trend/sideways/weak），并输出最优尾盘参数:

{{
  "regime": "sideways",
  "confidence": 0.8,
  "reasoning": "市场广度均衡，无极端情绪，属于震荡市",
  "tail_params": {{
    "max_pct": 6.0,
    "min_amount_yi": 0.8,
    "require_mainline": false,
    "require_main_inflow": true,
    "avoid_sectors": [],
    "focus_sectors": ["可聚焦的板块"],
    "risk_level": "medium"
  }}
}}

规则：
- strong_trend: max_pct降到5.0(防追高), min_amount提高到1.0, require_mainline=false(遍地机会)
- sideways: max_pct=6.0, min_amount=0.8, require_mainline可不设
- weak: max_pct降到4.0, min_amount降到0.5(流动性差), require_mainline=true(只做主线)
只输出JSON。"""


def layer1_market_perception(fetcher, fast: bool = True, deadline: float | None = None) -> dict:
    """Layer 1: LLM感知市场环境，返回动态配置。"""
    try:
        from agents.llm import get_llm
        llm = get_llm()
        if fast or not llm.available or (deadline and time.time() > deadline):
            return _fallback_market_config(fetcher)

        indices = fetcher.get_index_spot() or []
        breadth = fetcher.get_market_breadth() or {}
        sectors = fetcher.get_hot_sectors(limit=8) or []
        if deadline and time.time() > deadline:
            return _fallback_market_config(fetcher)
        lhb = {}  # 龙虎榜盘后/近月接口较慢，尾盘实时链路默认跳过
        lhb_top = sorted(
            [{"name": "", "net_buy": (v.get("net_buy",0) or 0)/1e8}
             for v in lhb.values()],
            key=lambda x: x["net_buy"], reverse=True)[:5]

        prompt = _build_layer1_prompt(indices, breadth, sectors, lhb_top)
        resp = llm.chat(_LAYER1_SYSTEM, prompt, temperature=0.2,
                        max_tokens=500, json_mode=True)

        data = _safe_json(resp or "")
        if data and data.get("tail_params"):
            params = data["tail_params"]
            return {
                "regime": data.get("regime", "sideways"),
                "confidence": data.get("confidence", 0.7),
                "reasoning": data.get("reasoning", ""),
                "max_pct": float(params.get("max_pct", 6.0)),
                "min_amount_yi": float(params.get("min_amount_yi", 0.8)),
                "require_mainline": bool(params.get("require_mainline", False)),
                "require_main_inflow": bool(params.get("require_main_inflow", True)),
                "avoid_sectors": params.get("avoid_sectors", []) or [],
                "focus_sectors": params.get("focus_sectors", []) or [],
                "risk_level": params.get("risk_level", "medium"),
            }
    except Exception:
        pass
    return _fallback_market_config(fetcher)


def _fallback_market_config(fetcher) -> dict:
    """LLM不可用时的量化降级。"""
    try:
        df = fetcher.get_index_kline("sh000001", days=60)
        if df is not None and not df.empty:
            closes = df["close"].astype(float)
            ma20 = closes.rolling(20).mean()
            slope = (float(ma20.iloc[-1]) - float(ma20.iloc[-6])) / float(ma20.iloc[-6]) if len(ma20)>=25 else 0
            if slope > 0.003:
                return {"regime":"strong_trend","max_pct":5.0,"min_amount_yi":1.0,
                        "require_mainline":False,"require_main_inflow":True,
                        "risk_level":"low"}
            elif slope < -0.003:
                return {"regime":"weak","max_pct":4.0,"min_amount_yi":0.5,
                        "require_mainline":True,"require_main_inflow":True,
                        "risk_level":"high"}
    except: pass
    return {"regime":"sideways","max_pct":6.0,"min_amount_yi":0.8,
            "require_mainline":False,"require_main_inflow":False,
            "risk_level":"medium"}


# ============================================================
# Layer 2: ML 尾盘拉升概率预测
# ============================================================

def _build_ml_features(item: dict, kline_df, market: dict, sector_sentiment: float) -> np.ndarray:
    """构建ML特征向量（15维）。

    特征设计原则：
    - 避免直接用未来信息（次日涨跌）
    - 聚焦14:30可得信号：量价异动、形态特征、资金异常
    """
    d = kline_df
    features = []

    # F1-F3: 当日量价特征
    pct = float(item.get("pct_change", 0))
    vol_ratio = float(item.get("volume_ratio", 1))
    turnover = float(item.get("turnover", 3))
    features.extend([pct, vol_ratio, turnover])

    # F4-F6: 午后动量（无法获取真实分时，用日K线估算）
    # 午后动量 ≈ 涨幅-开盘涨幅（如果开盘涨幅大，午后贡献就小）
    o = float(d.iloc[-1].get("open", 0)) if not d.empty else 0
    c = float(d.iloc[-1].get("close", 0)) if not d.empty else 0
    prev_c = float(d.iloc[-2].get("close", 0)) if len(d) > 1 else 0
    open_pct = (o - prev_c) / prev_c * 100 if prev_c else 0
    afternoon_momentum = pct - open_pct  # 上午涨幅 vs 下午涨幅
    features.extend([open_pct, afternoon_momentum, pct - open_pct - (pct-open_pct)/2])

    # F7-F9: 技术形态（近端）
    rsi = float(d.iloc[-1].get("rsi", 50)) if not d.empty and "rsi" in d.columns else 50
    ma5 = float(d.iloc[-1].get("ma5", c)) if not d.empty and "ma5" in d.columns else c
    ret_5d = (c - float(d.iloc[-6].get("close", c))) / float(d.iloc[-6].get("close", c)) * 100 if len(d) > 5 else 0
    features.extend([rsi, (c/ma5 - 1)*100, ret_5d])

    # F10-F12: 资金异动
    main_net = float(item.get("spot_main_net", 0) or 0) / 1e8
    amount_yi = float(item.get("amount_yi", 1) or 1)
    inflow_ratio = (main_net / amount_yi * 100) if amount_yi else 0 if main_net == 0 else 5
    features.extend([main_net, amount_yi, inflow_ratio])

    # F13-F15: 市场环境+板块
    market_score = float(market.get("score", 50))
    mcap_yi = float(item.get("total_mv_yi", 100) or 100)
    features.extend([market_score, sector_sentiment, math.log10(max(mcap_yi, 1))])

    return np.array(features, dtype=np.float32)


class TailRallyPredictor:
    """LightGBM 尾盘拉升预测器。

    预测目标：14:30后至收盘(15:00)涨幅 > 1% 的概率。
    训练标签：从历史日K线的高-收价差估算午后拉升。

    没有真实训练数据时，使用启发式规则替代。
    """

    def __init__(self):
        self._model = None
        self._trained = False

    def predict(self, features: np.ndarray) -> float:
        """预测尾盘拉升概率（0~1）。

        启发式规则（无训练数据时）：
        - 核心信号：午后动量>0.5 且 量比>1.5 且 主力净流入 → 概率>0.7
        - 负信号：午后动量<0 或 RSI>75 或 主力流出 → 概率<0.4
        """
        if len(features) < 15:
            return 0.5

        pct = features[0]
        vol_ratio = features[1]
        afternoon_momentum = features[5]
        rsi = features[7]
        main_net = features[10]
        inflow_ratio = features[12]
        market_score = features[13]

        # 核心正向信号
        score = 0.5

        # 午后动量（最关键）
        if afternoon_momentum > 1.0:
            score += 0.15
        elif afternoon_momentum > 0.3:
            score += 0.08
        elif afternoon_momentum < -0.5:
            score -= 0.15

        # 量能
        if vol_ratio > 2.0:
            score += 0.08
        elif vol_ratio > 1.5:
            score += 0.05
        elif vol_ratio < 0.8:
            score -= 0.08

        # 资金流
        if main_net > 0 and inflow_ratio > 3:
            score += 0.10
        elif main_net > 0:
            score += 0.04
        elif main_net < -0.5:
            score -= 0.12

        # 市场环境
        if market_score > 65:
            score += 0.05
        elif market_score < 40:
            score -= 0.10

        # 风险
        if rsi > 78:
            score -= 0.08

        return round(_clip(score, 0.10, 0.95), 3)


# ============================================================
# Layer 3: LLM 候选深度审查
# ============================================================

_LAYER3_SYSTEM = (
    "你是A股尾盘交易审查官。对候选股做三堂会审："
    "1. 基本面：有无业绩雷/解禁/减持风险"
    "2. 消息面：今日有无重大利好/利空"
    "3. 技术面：K线形态是否有尾盘拉升特征"
    "输出JSON加减分。"
)


def _build_layer3_prompt(candidates: list[dict], market: dict, sectors: list) -> str:
    cand_lines = []
    for c in candidates[:8]:
        cand_lines.append(
            f"{c['symbol']} {c.get('name','?')} 涨幅{c.get('pct_change',0):+.1f}% "
            f"量比{c.get('volume_ratio',1):.1f} 换手{c.get('turnover',0):.1f}% "
            f"成交{c.get('amount_yi',0):.1f}亿"
        )
    sec_s = "; ".join(f"{s.get('name','?')}+{s.get('pct',0):.1f}%" for s in (sectors or [])[:6])

    return f"""市场: {market.get('regime','?')} 情绪{market.get('score',50):.0f}分
板块TOP6: {sec_s}

候选股:
{chr(10).join(cand_lines)}

评审每只候选，输出JSON:
{{
  "reviews": [
    {{"symbol":"601869","score":0.04,"flags":["尾盘抢筹","板块共振"],"verdict":"可买","reason":"量能温和放大+站上VWAP"}},
    {{"symbol":"000xxx","score":-0.06,"flags":["连涨透支","消息利空"],"verdict":"观望","reason":"3日涨幅过大"}}
  ]
}}
score范围-0.08~+0.05。只输出JSON。"""


def layer3_llm_review(candidates: list[dict], market: dict,
                       sectors: list, deadline: float | None = None) -> list[dict]:
    """Layer 3: LLM审查候选股。"""
    try:
        from agents.llm import get_llm
        llm = get_llm()
        if not llm.available or not candidates or (deadline and time.time() > deadline):
            return []
        prompt = _build_layer3_prompt(candidates, market, sectors)
        resp = llm.chat(_LAYER3_SYSTEM, prompt, temperature=0.15,
                        max_tokens=800, json_mode=True)
        data = _safe_json(resp or "")
        return data.get("reviews", []) if data else []
    except Exception:
        return []


# ============================================================
# Layer 4: 风险量化评分 + 次日开盘方向预测
# ============================================================

def _compute_open_risk(item: dict, tech_indicators: dict, fund: dict, market: dict) -> dict:
    """次日开盘风险评估。

    低开风险因子：
    - 尾盘30分钟内缩量 → 买盘衰竭
    - 主力净流出 → 无人护盘
    - 市场情绪<40 → 系统性低开
    - 连涨3日以上 → 获利盘压力
    """
    pct_3d = tech_indicators.get("pct_3d", 0)
    rsi = tech_indicators.get("rsi", 50)
    retreat = tech_indicators.get("retreat_pct", 0)

    risk_score = 0.0
    flags = []

    # 午后回落
    if retreat > 3:
        risk_score += 20
        flags.append("午后大幅回落")
    elif retreat > 2:
        risk_score += 10

    # 主力资金
    if fund.get("is_inflow") is False:
        risk_score += 15
        flags.append("主力净流出")
    elif fund.get("inflow_ratio", 0) < 1:
        risk_score += 5

    # 连涨透支
    if pct_3d > 15:
        risk_score += 12
        flags.append(f"连涨{pct_3d:.0f}%透支")

    # RSI
    if rsi > 80:
        risk_score += 8
        flags.append("RSI过热")

    # 市场
    if market.get("score", 50) < 40:
        risk_score += 12
        flags.append("市场弱势")

    # 方向预测
    if risk_score < 12:
        direction = "偏强"
    elif risk_score < 22:
        direction = "中性"
    else:
        direction = "偏弱"

    return {
        "score": round(risk_score, 1),
        "direction": direction,
        "flags": flags,
        "can_trade": risk_score < 25,
    }


# ============================================================
# 主引擎
# ============================================================

class AITailEngine:
    """AI协同尾盘荐股引擎。

    调用入口: engine.run() → 返回 {picks, market_config, ml_insights, ...}
    """

    def __init__(self):
        from data.fetcher import get_fetcher
        self.fetcher = get_fetcher()
        self.ml = TailRallyPredictor()

    def run(self, count: int = 5, force: bool = False,
            max_seconds: int = 45, analyze_limit: int | None = None,
            use_llm_review: bool = False) -> dict:
        """执行完整AI协同尾盘选股。"""
        import datetime as dt
        from data.cache import invalidate_cache
        started = time.time()
        deadline = started + max(15, int(max_seconds or 45))

        # 快速链路：不清全市场快照，避免数据源抖动时重新拉取拖到数分钟。
        # DataFetcher 自身有短 TTL 和 stale 标记；指数/板块轻量缓存可以刷新。
        for key in ("index_spot","as_sectors:10","as_sectors:5","sectors:10"):
            invalidate_cache(key)

        now = dt.datetime.now()
        if not force:
            t = now.time()
            if not (dt.time(13,0) <= t <= dt.time(15,0)):
                return {"error": "尾盘窗口未开放(13:00-15:00)"}

        print("[AI尾盘] Layer1: 市场感知...")
        market_cfg = layer1_market_perception(self.fetcher, fast=not use_llm_review, deadline=deadline)
        print(f"[AI尾盘]   阶段={market_cfg['regime']} 风险={market_cfg['risk_level']}"
              f" 阈值:max_pct={market_cfg['max_pct']}%")

        # 动态配置
        cfg = TailConfig(
            count=count, max_pct=market_cfg["max_pct"],
            min_amount_yi=market_cfg["min_amount_yi"],
            require_main_inflow=market_cfg["require_main_inflow"],
            require_mainline=market_cfg["require_mainline"],
            market_regime=market_cfg["regime"],
        )

        # 午后活跃股池
        print("[AI尾盘] Stage1: 午后股池...")
        spot = self.fetcher.get_market_spot()
        source = getattr(self.fetcher, "last_market_spot_source", "未知")
        stale = bool(getattr(self.fetcher, "last_market_spot_stale", False))
        if stale:
            return self._empty_result(now, market_cfg, source, "行情快照为缓存兜底，AI尾盘链路要求实时数据，本次不强行选股。", started)
        candidates = self._build_pool(spot, cfg, market_cfg)
        print(f"[AI尾盘]   候选: {len(candidates)}只")
        if not candidates:
            return self._empty_result(now, market_cfg, source, "实时快照中没有满足量价/流动性条件的候选。", started)

        # 市场情绪
        print("[AI尾盘] Stage2: 市场情绪...")
        if time.time() > deadline:
            market = {"score": 50, "breadth": {}, "indices": [], "regime": market_cfg["regime"]}
        else:
            market = _compute_market(self.fetcher)
        market["regime"] = market_cfg["regime"]
        market["score"] = market.get("score", 50)

        sectors = [] if time.time() > deadline else (self.fetcher.get_hot_sectors(limit=8) or [])

        # 逐股分析
        scan_limit = analyze_limit or max(6, min(12, count * 3))
        print(f"[AI尾盘] Stage3: 逐股分析+ML预测 ({len(candidates[:scan_limit])}只)...")
        scored = []
        from core.indicators import add_all_indicators

        for item in candidates[:scan_limit]:
            if time.time() > deadline:
                break
            try:
                # K线数据
                kline = self.fetcher.get_kline(item["symbol"], days=30)
                if kline is None or kline.empty:
                    continue
                kline = add_all_indicators(kline).fillna(0)

                # ML特征 → Layer2预测
                sec_pct = 0
                if sectors:
                    sec_pct = np.mean([s.get("pct", s.get("day_pct", 0)) for s in sectors[:3]]) or 0
                feats = _build_ml_features(item, kline, market, sec_pct)
                rally_prob = self.ml.predict(feats)

                # 技术指标
                tech = _compute_tail_tech(item, kline)

                # 资金流
                fund = _compute_fund(item)

                # 风控
                risk = _compute_open_risk(item, tech, fund, market)

                # 综合评分
                final = round(
                    rally_prob * 100 * 0.35 +           # ML预测(权重最高35%)
                    _clip(fund.get("score", 50), 0, 100) * 0.25 +
                    _clip(tech.get("score", 50), 0, 100) * 0.20 +
                    _clip(market.get("score", 50), 0, 100) * 0.15 +
                    (5 if item.get("hot_sector") else 0) * 0.05 -
                    risk.get("score", 0) * 0.4,
                    1
                )

                if not risk.get("can_trade", True):
                    continue

                scored.append({**item,
                    "score": final, "rally_prob": round(rally_prob, 3),
                    "tech": tech, "fund": fund, "risk": risk})
            except Exception:
                continue

        # Layer3: LLM审查TopN（默认关闭，避免交互链路长时间等待）
        print("[AI尾盘] Layer3: LLM深度审查...")
        scored.sort(key=lambda x: x["score"], reverse=True)
        reviews = layer3_llm_review(scored[:min(6, len(scored))], market_cfg, sectors, deadline=deadline) if use_llm_review else []
        review_map = {str(r.get("symbol","")): r for r in reviews}

        # 应用LLM加减分
        for s in scored:
            rv = review_map.get(s["symbol"], {})
            ai_score = float(rv.get("score", 0) or 0)
            s["score"] = round(s["score"] + ai_score, 3)
            s["ai_score"] = round(ai_score, 3)
            s["ai_verdict"] = rv.get("verdict", "")
            s["ai_flags"] = rv.get("flags", [])
            s["ai_reason"] = rv.get("reason", "")

        scored.sort(key=lambda x: x["score"], reverse=True)
        picks = scored[:count]

        # 交易计划 + 补齐前端所需字段（兼容 renderTailPick）
        for p in picks:
            tech = p.get("tech", {})
            fund = p.get("fund", {})
            risk = p.get("risk", {})
            # 前端直接读取的平铺字段
            p["market_score"] = round(_clip(market.get("score", 50), 0, 100), 1)
            p["fund_score"] = round(fund.get("score", 50), 1)
            p["tech_score"] = round(tech.get("score", 50), 1)
            p["risk_penalty"] = round(risk.get("score", 0), 1)
            # 入选逻辑（合并技术+资金+AI审查）
            tech_note = f"午后动量{tech.get('afternoon_momentum','?')} VWAP{tech.get('vwap_dev','?')}"
            fund_note = f"净流入{fund.get('main_net',0):.2f}亿" if fund.get("main_net",0) > 0 else "无主力数据"
            ai_note = f" AI审查:{p.get('ai_verdict','')}({p.get('ai_score',0):+.2f})" if p.get("ai_verdict") else ""
            p["reason"] = f"{tech_note} | {fund_note}{ai_note}"
            p["buy_plan"] = ("14:30后站稳分时均价可分批建仓，不放量不追" if cfg.require_main_inflow
                              else "14:30后若未跳水可小仓试买")
            p["sell_plan"] = "次日高开+2%~+4%分批止盈，低开30分钟不翻红止损"
            p["stop_loss"] = f"跌破买入价1.5%无条件止损"

        fund_chart = {
            "symbols": [f"{p['name']}\n{p['symbol']}" for p in picks],
            "series": [{"name":"主力净流入", "data":[p.get("fund",{}).get("main_net",0) for p in picks]}],
            "unit": "亿元",
        }

        return {
            "engine": "ai-tail-dedicated",
            "as_of": now.strftime("%Y-%m-%d %H:%M"),
            "strategy": "AI协同尾盘策略：LLM市场感知→ML拉升预测→LLM审查→风控过滤",
            "data_source": source,
            "market_config": market_cfg,
            "market": market,
            "candidates_count": len(candidates),
            "analyzed_count": len(scored),
            "elapsed_sec": round(time.time() - started, 2),
            "ml_insights": {
                "model": "启发式ML快速评分",
                "note": "15维特征(午后动量/量价/资金/形态)→尾盘拉升概率预测；LLM审查默认跳过以保证响应",
                "avg_rally_prob": round(np.mean([p["rally_prob"] for p in picks]), 3) if picks else 0,
            },
            "picks": picks,
            "fund_chart": fund_chart,
        }

    def _empty_result(self, now, market_cfg: dict, source: str, note: str, started: float | None = None) -> dict:
        return {
            "engine": "ai-tail-dedicated",
            "as_of": now.strftime("%Y-%m-%d %H:%M"),
            "strategy": "AI协同尾盘策略：快速市场感知→ML拉升预测→风控过滤",
            "data_source": source,
            "market_config": market_cfg,
            "market": {"score": 50, "breadth": {}, "indices": [], "regime": market_cfg.get("regime")},
            "candidates_count": 0,
            "analyzed_count": 0,
            "elapsed_sec": round(time.time() - started, 2) if started else 0,
            "risk_note": note,
            "ml_insights": {"model": "启发式ML快速评分", "note": note, "avg_rally_prob": 0},
            "picks": [],
            "fund_chart": {"symbols": [], "series": [], "unit": "亿元"},
        }

    def _build_pool(self, spot, cfg: TailConfig, mkt_cfg: dict) -> list[dict]:
        """构建午后候选池（含量价异动检测+板块轮动过滤）。"""
        if spot is None or spot.empty:
            return []

        code_c = next((c for c in spot.columns if c in ("代码","symbol")), None)
        name_c = next((c for c in spot.columns if c in ("名称","name")), None)
        pct_c = next((c for c in spot.columns if "涨跌幅" in c), None)
        price_c = next((c for c in spot.columns if "最新价" in c or "现价" in c), None)
        turn_c = next((c for c in spot.columns if "换手" in c), None)
        vr_c = next((c for c in spot.columns if "量比" in c), None)
        amt_c = next((c for c in spot.columns if "成交额" in c or "成交金额" in c), None)
        mv_c = next((c for c in spot.columns if "市值" in c), None)
        main_c = next((c for c in spot.columns if "主力净流入" in c or "主力净额" in c), None)

        # 四层智能链路用于实时尾盘，不接受缺成交额/换手/量比的降级快照。
        if not (code_c and name_c and pct_c and price_c and turn_c and vr_c and amt_c):
            return []

        avoid = set(mkt_cfg.get("avoid_sectors", []) or [])
        focus = set(mkt_cfg.get("focus_sectors", []) or [])

        rows = []
        for _, r in spot.iterrows():
            code = str(r.get(code_c, "")).strip().replace("sh","").replace("sz","").zfill(6)
            if not _eligible_tail(code):
                continue
            pct = _num(r.get(pct_c))
            if pct < cfg.min_pct or pct > cfg.max_pct:
                continue
            amount = _num(r.get(amt_c))
            if amount < cfg.min_amount_yi * 1e8:
                continue
            turn = _num(r.get(turn_c))
            if turn < cfg.min_turnover:
                continue
            vr = _num(r.get(vr_c), 1)
            if vr < cfg.min_volume_ratio:
                continue

            # 量价异动：量比>2.5且涨幅<3% = 异动放量（大资金悄悄建仓）
            is_volume_anomaly = vr > 2.5 and pct < 3

            rows.append({
                "symbol": code, "name": str(r.get(name_c, code)) if name_c else code,
                "price": round(_num(r.get(price_c)), 2) if price_c else None,
                "pct_change": round(pct, 2),
                "amount_yi": round(amount / 1e8, 2),
                "turnover": round(turn, 2),
                "volume_ratio": round(vr, 2),
                "total_mv_yi": round(_num(r.get(mv_c))/1e8, 1) if mv_c else None,
                "spot_main_net": _num(r.get(main_c)) if main_c else 0,
                "volume_anomaly": is_volume_anomaly,
            })

        # 排序：异动放量 + 量比 → 前排
        rows.sort(key=lambda x: (x["volume_anomaly"], x["volume_ratio"]), reverse=True)
        return rows[:60]


# ============================================================
# 辅助函数
# ============================================================

def _safe_json(raw: str) -> dict:
    if not raw: return {}
    try: return json.loads(raw)
    except:
        m = re.search(r'\{.*\}', raw or "", re.DOTALL)
        try: return json.loads(m.group()) if m else {}
        except: return {}


def _compute_market(fetcher) -> dict:
    breadth = fetcher.get_market_breadth() or {}
    indices = fetcher.get_index_spot() or []
    idx_pct = [i.get("pct") for i in indices if isinstance(i.get("pct"), (int, float))]
    return {"score": 50 + sum(idx_pct)/len(idx_pct)*10 if idx_pct else 50,
            "breadth": breadth, "indices": indices}


def _compute_tail_tech(item: dict, d) -> dict:
    if d is None or d.empty or len(d) < 5: return {"score": 50, "rsi": 50}
    last = d.iloc[-1]
    c = float(last.get("close", 0) or 0)
    h = float(last.get("high", c) or c)
    l = float(last.get("low", c) or c)
    o = float(last.get("open", c) or c)
    prev_c = float(d.iloc[-2].get("close", c)) if len(d) > 1 else c

    rsi = float(last.get("rsi", 50) or 50)
    ma5 = float(last.get("ma5", c)) if "ma5" in d.columns else c
    vol = float(last.get("volume", 0) or 0)
    vol_ma5 = float(d["volume"].tail(6).head(5).mean()) if "volume" in d else vol

    retreat = (h - c) / h * 100 if h else 0
    vwap_est = ((h + l + c) / 3)
    vwap_dev = (c - vwap_est) / vwap_est * 100 if vwap_est else 0

    pct_3d = (c - float(d.iloc[-4].get("close", c))) / float(d.iloc[-4].get("close", c)) * 100 if len(d) >= 4 else 0
    afternoon = (c - o) / o * 100 if o else 0

    score = _clip(50 + afternoon*10 + vwap_dev*5 - retreat*8, 20, 90)
    return {"score": round(score, 1), "rsi": round(rsi, 1), "retreat_pct": round(retreat, 1),
            "vwap_dev": round(vwap_dev, 1), "pct_3d": round(pct_3d, 1),
            "afternoon_momentum": round(afternoon, 1)}


def _compute_fund(item: dict) -> dict:
    main_net = item.get("spot_main_net") or 0
    amount = (item.get("amount_yi") or 1) * 1e8
    mcap = (item.get("total_mv_yi") or 50) * 1e8
    inflow_r = (main_net / amount * 100) if amount > 0 and abs(main_net) > 0.01 else 0
    intensity = (main_net / mcap * 10000) if mcap > 0 else 0
    score = _clip(50 + main_net/1e8*15 + inflow_r*5 + intensity*30, 15, 90)
    return {"score": round(score, 1), "main_net": round(main_net/1e8, 2),
            "inflow_ratio": round(inflow_r, 2), "is_inflow": main_net > 0.01}
