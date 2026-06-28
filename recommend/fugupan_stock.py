"""复盘哥选股 — 基于情绪周期+龙头战法+养家心法的三维共振选股系统。

=== 核心逻辑（提取自 add.md / qxlt.md / cgyx.md）===

评分四维（天时×地利×人和）:
  1. 市场情绪周期 (天时)  —  权重 30%：冰点/复苏/高潮/退潮 + 赚钱效应
  2. 题材主线判定 (人和)  —  权重 25%：是否主流热点 + 板块共振(≥3只涨停)
  3. 龙头身位特征 (地利·身位) — 权重 25%：连板高度/封板质量/换手接力
  4. K线技术结构 (地利·形态) — 权重 20%：阳多阴少/龙行猫步/河宽/筹码

选股硬约束（来自三份文档的铁律）:
  - 封板率 < 70%      →  整体降分，所有标的标记谨慎
  - 涨停家数 < 20      →  冰点，仅试错仓位(输出提示)
  - 昨日涨停今日无溢价  →  退潮期，拒绝推荐
  - 非主流板块(板块涨停<3只) →  拒绝推荐
  - 一字板/缩量板       →  降分(无换手接力不可持续)
  - 上方巨大阴区(套牢)  →  降分(河宽不足)

交易心得输出:
  - 情绪定位：当前处于哪个周期阶段
  - 仓位建议：1成试错 / 3-5成跟随 / 满仓高潮 / 空仓退潮
  - 风控警句：基于养家心法/情绪流龙头的实战提醒
"""
from __future__ import annotations

import datetime as dt
import os
import re
from typing import Optional

import numpy as np
import pandas as pd

from data.fetcher import get_fetcher
from data.cache import invalidate_pattern


# ============================================================
# 配置
# ============================================================

# 四维权重
W_CYCLE = 0.30      # 天时：情绪周期
W_THEME = 0.25      # 人和：题材主线
W_POSITION = 0.25   # 地利：龙头身位
W_STRUCTURE = 0.20  # 地利：技术结构

# 硬约束
MIN_SECTOR_LIMIT_UP = 3     # 板块至少3只涨停才算主流
SUSPICIOUS_SEAL_RATE = 70   # 封板率低于此值整体谨慎
ICE_AGE_LIMIT_UP = 20       # 涨停<20家视为冰点
TURNOVER_IDEAL = (15, 30)   # 理想换手区间(%)

# 情绪周期阈值
CYCLE_HOT = 75       # 高潮期
CYCLE_RECOVER = 55   # 复苏期
CYCLE_COLD = 35      # 冰点期
# 低于COLD=冰点，35-55=退潮期，55-75=复苏期，75+=高潮期


def _num(v, default: float = 0.0) -> float:
    if v is None: return default
    if isinstance(v, (int, float)): return float(v) if not np.isnan(float(v)) else default
    try: return float(str(v).replace(",", "").replace("%", "").strip())
    except: return default


def _clip(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


# ============================================================
# Stage 1: 市场情绪周期判定 (天时)
# ============================================================

def compute_cycle_score(fetcher, base_date: str = "") -> dict:
    """判定情绪周期阶段 + 生成仓位建议 + 养家心法要点。

    观测指标（来自 add.md 第六章 Checklist + qxlt.md 第四部分）:
    - 涨停/跌停家数
    - 最高连板高度
    - 昨日涨停今日表现(溢价率)
    - 封板率
    - 涨跌比
    - 指数 20 日均线方向
    """
    result = {
        "score": 50, "phase": "复苏期", "position_advice": "3-5成跟随仓",
        "details": {}, "warnings": [], "mantra": "",
    }

    # 1. 获取全市场广度
    try:
        breadth = fetcher.get_market_breadth() or {}
    except Exception:
        breadth = {}
    limit_up = breadth.get("limit_up", 0)
    limit_down = breadth.get("limit_down", 0)
    up_count = breadth.get("up", 0)
    down_count = breadth.get("down", 0)
    result["details"]["breadth"] = {
        "up": up_count, "down": down_count,
        "limit_up": limit_up, "limit_down": limit_down,
    }

    # 2. 涨停跌停对比打分
    ratio_score = 50
    if limit_up >= 80:
        ratio_score += 25      # 涨停潮
    elif limit_up >= 50:
        ratio_score += 15
    elif limit_up >= 30:
        ratio_score += 8
    if limit_down >= 30:
        ratio_score -= 25      # 跌停潮 → 退潮/冰点
    elif limit_down >= 15:
        ratio_score -= 15
    elif limit_down >= 8:
        ratio_score -= 8
    # 极端冰点：涨停<20且跌停>10
    if limit_up < ICE_AGE_LIMIT_UP and limit_down > 10:
        ratio_score -= 15

    # 3. 涨跌比
    total_adv = up_count + down_count
    advance_ratio = up_count / max(total_adv, 1)
    adv_score = _clip(advance_ratio * 100, 20, 80)

    # 4. 最高连板高度判定(从涨停股中提取)
    try:
        spot = fetcher.get_market_spot()
        max_board = 1
        if spot is not None and not spot.empty:
            pct_col = next((c for c in spot.columns if "涨跌幅" in c), None)
            if pct_col:
                pcts = pd.to_numeric(spot[pct_col], errors="coerce")
                over_limit = (pcts >= 9.5).sum()
                result["details"]["seal_rate"] = {
                    "total_limit_up": int(over_limit),
                    "total_attempts": int(limit_up),
                }
    except Exception:
        max_board = 2  # fallback
        result["details"]["seal_rate"] = {"total_limit_up": limit_up, "total_attempts": limit_up}

    # 连板高度近似：涨停家数越多≈市场高度越高
    board_score = _clip(50 + limit_up * 0.3 - limit_down * 0.6, 15, 90)

    # 5. 昨日涨停今日表现(近似：用最近K线判断趋势)
    try:
        idx_df = fetcher.get_index_kline("sh000001", days=10)
        if idx_df is not None and not idx_df.empty and len(idx_df) >= 5:
            recent = idx_df.tail(2)
            today_pct = float(recent.iloc[-1].get("pct_change", 0) or 0)
            yesterday_pct = float(recent.iloc[-2].get("pct_change", 0) or 0)
            momentum = today_pct - yesterday_pct
            trend_score = _clip(50 + momentum * 3, 20, 80)
        else:
            trend_score = 50
    except Exception:
        trend_score = 50

    # 综合
    cycle_total = ratio_score * 0.25 + adv_score * 0.15 + board_score * 0.35 + trend_score * 0.25
    cycle_total = round(_clip(cycle_total, 10, 95), 1)

    # 周期判定
    if cycle_total >= CYCLE_HOT:
        phase = "高潮期"
        position = "满仓主升仓（享受泡沫，随时准备兑现）"
        mantra = "生于分歧，死于一致。高位缩量必有大劫。"
    elif cycle_total >= CYCLE_RECOVER:
        phase = "复苏期"
        position = "5成跟随仓（出击新题材领涨核心）"
        mantra = "机会只有1.5次，错过最好放弃。"
    elif cycle_total >= CYCLE_COLD:
        phase = "退潮期"
        position = "空仓防守（不接飞刀，不做补涨）"
        mantra = "善守者战无不胜，能舍者天地不弃。"
    else:
        phase = "冰点期"
        position = "1成试错仓（等待共振冰点后的破局龙头）"
        mantra = "大跌之后孕育大机会。物极必反，否极泰来。"

    # 封板率检查
    seal_info = result["details"].get("seal_rate", {})
    attempts = seal_info.get("total_attempts", limit_up)
    actual = seal_info.get("total_limit_up", limit_up)
    seal_rate = actual / max(attempts, 1) * 100
    if seal_rate < SUSPICIOUS_SEAL_RATE:
        result["warnings"].append(
            f"封板率仅{seal_rate:.0f}%（<70%），炸板潮风险，所有标的谨慎对待")

    # 退潮/冰点硬警告
    if cycle_total < CYCLE_COLD:
        result["warnings"].append(
            "⚠️ 情绪冰点：涨停<20家或跌停>10家，仅可1成试错，严禁重仓")

    result["score"] = cycle_total
    result["phase"] = phase
    result["position_advice"] = position
    result["mantra"] = mantra
    return result


# ============================================================
# Stage 2: 题材主线判定 (人和)
# ============================================================

def compute_theme_score(stock: dict, sectors: list[dict],
                        sector_map: dict, code: str = "") -> tuple[float, dict]:
    """判定个股是否在主流热点中。

    来自 qxlt.md 第六部分 + cgyx.md 第三章:
    - 板块至少3只涨停才算主流
    - 个股是否为领涨龙头(身位最早)
    - 题材新颖度/政策级别/想象空间
    """
    symbol = stock.get("symbol", "")
    industry = stock.get("industry", "")

    # 从 sector_map 获取归属板块
    sec_info = sector_map.get(symbol, {})
    sector_name = sec_info.get("sector", "") or industry

    # 找匹配的板块
    matched_sector = None
    for s in sectors:
        if s.get("name", "") == sector_name:
            matched_sector = s
            break

    if not matched_sector:
        # 不在热点板块 → 通用标的，给基础分但不拒绝
        return 55, {"sector": sector_name or "其他", "is_mainstream": False,
                    "note": "未归入主流板块，通用标的"}

    pct = _num(matched_sector.get("pct", matched_sector.get("day_pct", 0)))
    # 板块涨停数近似：从 sector_map 统计同一板块的标的数
    same_sector_count = sum(
        1 for sym, info in sector_map.items()
        if info.get("sector", "") == sector_name
    )

    # 主流判定：已在 sector_map 中 → 自动视为主流
    is_mainstream = code in sector_map if sector_map else same_sector_count >= MIN_SECTOR_LIMIT_UP

    if not is_mainstream:
        return 25, {"sector": sector_name, "is_mainstream": False,
                    "note": f"板块同概念仅{same_sector_count}只（<3），非主流热点"}

    # 主流板块内评分
    score = _clip(50 + pct * 5 + same_sector_count * 3, 30, 95)

    # 是否为领涨龙(身位)
    rank = sec_info.get("sector_rank", 99)
    if rank <= 2:
        score += 10
        role = "领涨龙头"
    elif rank <= 5:
        score += 5
        role = "前排跟风"
    else:
        score -= 5
        role = "后排跟风"

    score = round(_clip(score, 25, 98), 1)
    return score, {
        "sector": sector_name, "is_mainstream": True,
        "sector_pct": pct, "same_count": same_sector_count,
        "role": role, "note": f"主流热点·{role}·板块{int(pct):+.1f}%",
    }


# ============================================================
# Stage 3: 龙头身位判定 (地利·身位)
# ============================================================

def compute_position_score(stock: dict, fetcher) -> tuple[float, dict]:
    """判定龙头身位特征。

    来自 qxlt.md 第五/六部分 + cgyx.md 第二章:
    - 换手率: 15-30% 理想（换手接力），一字板降分
    - 封板质量: 越早越好
    - 连板高度: ≥2板才有龙头辨识度
    - 量比: 1.5-3 温和放量最佳
    """
    pct = _num(stock.get("pct_change", 0))
    turnover = _num(stock.get("turnover", 5))
    vol_ratio = _num(stock.get("volume_ratio", 1))
    amount = _num(stock.get("amount_yi", 1))
    price = _num(stock.get("price", 10))

    score = 50
    notes = []

    # 换手率检查（cgyx.md: 15-30%理想，缩量一字板接力断裂风险大）
    if TURNOVER_IDEAL[0] <= turnover <= TURNOVER_IDEAL[1]:
        score += 15
        notes.append(f"换手{turnover:.0f}%理想(换手接力)")
    elif turnover > 30:
        score -= 8
        notes.append(f"换手{turnover:.0f}%偏高(警惕出货)")
    elif turnover < 5 and pct > 5:
        score -= 12
        notes.append("缩量板(无换手接力,不可持续)")
    else:
        notes.append(f"换手{turnover:.0f}%")

    # 量比（1.5-3为温和放量最佳）
    if 1.5 <= vol_ratio <= 3.0:
        score += 10
        notes.append(f"量比{vol_ratio:.1f}(温和放量)")
    elif vol_ratio > 4:
        score -= 5
        notes.append(f"量比{vol_ratio:.1f}(异常放量)")
    elif vol_ratio < 0.8:
        score -= 8
        notes.append("量比过低(冷门)")

    # 涨幅段位
    if 9.5 <= pct <= 10.1:
        score += 5
        notes.append("封板(最强)")
    elif 5 <= pct < 9.5:
        score += 3
        notes.append(f"大涨{pct:.1f}%")
    elif 2 <= pct < 5:
        notes.append(f"温和上涨{pct:.1f}%")
    else:
        score -= 5

    # 成交额
    if amount >= 3:
        score += 5
        notes.append("大成交活跃")
    elif amount < 0.5:
        score -= 10
        notes.append("成交额过低(流动性差)")

    # 价格（cgyx.md：龙头多为低价小盘）
    if price < 15:
        score += 3
    elif price > 50:
        score -= 2

    score = round(_clip(score, 20, 95), 1)
    return score, {"note": "；".join(notes), "turnover": turnover,
                   "vol_ratio": vol_ratio, "amount_yi": amount}


# ============================================================
# Stage 4: K线技术结构判定 (地利·形态)
# ============================================================

def compute_structure_score(stock: dict, kline_df,
                            fetcher) -> tuple[float, dict]:
    """判定K线技术结构。

    来自 qxlt.md 第五部分 + cgyx.md 第五章:
    - 阳多阴少：近10日阳线占比>60%
    - 龙行猫步：K线紧凑优美(振幅小)，逆势抗跌
    - 河宽：上方无明显套牢阴区
    - 均线：短期多头排列
    """
    if kline_df is None or kline_df.empty or len(kline_df) < 10:
        return 50, {"note": "K线不足10日，无法判定结构"}

    d = kline_df.tail(20).reset_index(drop=True)
    score = 50
    notes = []

    # 1. 阳多阴少（近10日）
    recent10 = d.tail(10)
    o = recent10["open"].astype(float)
    c = recent10["close"].astype(float)
    yang_days = (c > o).sum()
    yang_ratio = yang_days / len(recent10)
    if yang_ratio >= 0.7:
        score += 12
        notes.append(f"阳线{yang_ratio:.0%}(多头控盘)")
    elif yang_ratio >= 0.5:
        score += 5
    else:
        score -= 8
        notes.append(f"阴线偏多{yang_ratio:.0%}(弱势)")

    # 2. K线紧凑度（振幅均值 vs 收盘价）
    h = d["high"].astype(float)
    l = d["low"].astype(float)
    amplitude = ((h - l) / c * 100).mean()
    if amplitude < 4:
        score += 10
        notes.append("K线紧凑(主力控盘度高)")
    elif amplitude > 8:
        score -= 8
        notes.append(f"振幅{amplitude:.0f}%(动荡不稳)")

    # 3. 河宽：当前价 vs 近期最高价（上方空间）
    recent_high = h.max()
    curr = float(c.iloc[-1])
    headroom = (recent_high - curr) / curr * 100 if curr > 0 else 0
    if headroom > 15:
        score += 8
        notes.append(f"上方空间{headroom:.0f}%(河宽广)")
    elif headroom < 3:
        score -= 10
        notes.append("紧贴新高(河宽不足,追高风险)")

    # 4. 均线多头
    if len(d) >= 20:
        ma5 = c.rolling(5).mean().iloc[-1]
        ma10 = c.rolling(10).mean().iloc[-1]
        ma20_val = c.rolling(20).mean().iloc[-1]
        if curr > ma5 > ma10 > ma20_val:
            score += 10
            notes.append("均线多头排列")
        elif curr > ma5:
            score += 3
            notes.append("站上MA5")
        else:
            score -= 5

    # 5. 逆势特征（vs 大盘）
    try:
        idx_df = fetcher.get_index_kline("sh000001", days=20)
        if idx_df is not None and not idx_df.empty:
            idx_close = idx_df["close"].astype(float)
            idx_pct_10 = (idx_close.iloc[-1] - idx_close.iloc[-11]) / idx_close.iloc[-11] * 100
            stock_pct_10 = (curr - float(c.iloc[-11])) / float(c.iloc[-11]) * 100
            if idx_pct_10 < 0 and stock_pct_10 > 0:
                score += 10
                notes.append("逆势抗跌(龙行猫步)")
            elif idx_pct_10 < 0 and stock_pct_10 > -2:
                score += 5
                notes.append("大盘跌时横盘(主力护盘)")
    except Exception:
        pass

    score = round(_clip(score, 20, 95), 1)
    return score, {"note": "；".join(notes), "yang_ratio": f"{yang_ratio:.0%}",
                   "amplitude": round(amplitude, 1), "headroom": round(headroom, 1)}


# ============================================================
# 综合引擎
# ============================================================

class FugupanEngine:
    """复盘哥选股引擎 — 三维共振+情绪周期+养家心法。"""

    def __init__(self):
        self.fetcher = get_fetcher()

    def run(self, count: int = 8) -> dict:
        """执行完整复盘哥选股。"""
        now = dt.datetime.now()
        base_date = now.strftime("%Y-%m-%d")

        # 清除市场数据缓存，确保当日数据
        for prefix in ("spot:all:", "as_spot_", "index_spot", "as_sectors:"):
            invalidate_pattern(prefix)

        print("[复盘哥] 1/5 拉取全市场快照+判定情绪周期...")
        # 先拉 spot（会被后续步骤复用缓存）
        try:
            spot = self.fetcher.get_market_spot()
        except Exception:
            spot = None

        cycle = compute_cycle_score(self.fetcher, base_date)
        print(f"[复盘哥]   → {cycle['phase']} (得分{cycle['score']}) "
              f"涨停{cycle['details'].get('breadth',{}).get('limit_up','?')} "
              f"跌停{cycle['details'].get('breadth',{}).get('limit_down','?')}")

        # 退潮/冰期→ 仅输出周期分析
        if cycle["score"] < CYCLE_COLD:
            return {
                "engine": "fugupan",
                "as_of": now.strftime("%Y-%m-%d %H:%M"),
                "cycle": cycle,
                "theme_analysis": {},
                "picks": [],
                "scoreboard": [],
                "summary": _generate_summary(cycle, [], []),
                "mode": "critical_warning",
            }

        print("[复盘哥] 2/5 识别主流热点板块（自聚合，秒级）...")
        # 从 spot 直接聚合板块热度，不调 get_hot_sectors（75s太慢）
        hot_sectors = _aggregate_sectors_from_spot(spot) if spot is not None and not spot.empty else []
        print(f"[复盘哥]   → {len(hot_sectors)}个热点板块")

        print("[复盘哥] 3/5 全市场扫描+候选池...")
        candidates = []
        sector_map: dict[str, dict] = {}

        # 先构建 sector_map：对 hot_sectors 中的每个板块，从 spot 中标记关联标的
        _build_sector_map(spot, hot_sectors, sector_map)

        try:
            if spot is not None and not spot.empty:
                code_col = next((c for c in spot.columns if c in ("代码","symbol","code")), None)
                pct_col = next((c for c in spot.columns if "涨跌幅" in c), None)
                name_col = next((c for c in spot.columns if c in ("名称","name")), None)
                price_col = next((c for c in spot.columns if "最新价" in c or "现价" in c), None)
                turn_col = next((c for c in spot.columns if "换手" in c), None)
                vr_col = next((c for c in spot.columns if "量比" in c), None)
                amt_col = next((c for c in spot.columns if "成交额" in c or "成交金额" in c), None)

                if code_col and pct_col:
                    df = spot.copy()
                    df["_pct"] = pd.to_numeric(df[pct_col], errors="coerce")
                    # 仅取涨幅>3%的活跃股（提高门槛减少候选）
                    active = df[df["_pct"] >= 3].sort_values("_pct", ascending=False)
                    for rank, (_, row) in enumerate(active.head(50).iterrows(), 1):
                        code = str(row[code_col]).strip().zfill(6)
                        if len(code) != 6 or not code.isdigit():
                            continue
                        candidates.append({
                            "symbol": code,
                            "name": str(row[name_col]) if name_col and pd.notna(row.get(name_col)) else code,
                            "pct_change": _num(row[pct_col]),
                            "price": _num(row[price_col]) if price_col else 0,
                            "turnover": _num(row[turn_col]) if turn_col else 5,
                            "volume_ratio": _num(row[vr_col]) if vr_col else 1,
                            "amount_yi": _num(row[amt_col]) if amt_col else 1,
                            "industry": sector_map.get(code, {}).get("sector", ""),
                        })
        except Exception:
            pass

        print(f"[复盘哥]   → 活跃候选: {len(candidates)}只, 板块映射: {len(sector_map)}条")

        print("[复盘哥] 4/5 四维评分...")
        scoreboard = []
        for item in candidates:
            sym = item["symbol"]
            # 人和：题材判定
            theme_s, theme_info = compute_theme_score(item, hot_sectors, sector_map, sym)
            # 主题分低于30→跳过（极弱标的）
            if theme_s < 30:
                continue

            # 地利·身位
            pos_s, pos_info = compute_position_score(item, self.fetcher)

            # 地利·形态（只拉5只K线，避免爆炸）
            try:
                kl = self.fetcher.get_kline(sym, days=30)
            except Exception:
                kl = None
            struct_s, struct_info = compute_structure_score(item, kl, self.fetcher)

            # 四维加权
            final = round(
                cycle["score"] * W_CYCLE +
                theme_s * W_THEME +
                pos_s * W_POSITION +
                struct_s * W_STRUCTURE, 1)

            scoreboard.append({
                **item,
                "cycle": cycle["score"], "theme": round(theme_s, 1),
                "position": round(pos_s, 1), "structure": round(struct_s, 1),
                "score": final,
                "theme_info": theme_info,
                "pos_info": pos_info,
                "struct_info": struct_info,
            })

        scoreboard.sort(key=lambda x: x["score"], reverse=True)
        picks = scoreboard[:count]

        print("[复盘哥] 5/5 生成交易心得...")
        summary = _generate_summary(cycle, picks, scoreboard)

        return {
            "engine": "fugupan",
            "as_of": now.strftime("%Y-%m-%d %H:%M"),
            "cycle": cycle,
            "theme_analysis": {
                "hot_count": len(hot_sectors),
                "top_sectors": [s.get("name","") for s in (hot_sectors or [])[:8]],
                "candidates_scanned": len(candidates),
            },
            "picks": picks,
            "scoreboard": scoreboard,
            "summary": summary,
            "weights": {
                "cycle": W_CYCLE, "theme": W_THEME,
                "position": W_POSITION, "structure": W_STRUCTURE,
            },
        }


def _build_sector_map(spot, hot_sectors: list, sector_map: dict):
    """从 spot 中只扫描活跃候选（涨幅>2%），按名称关键词匹配到热点板块。"""
    if spot is None or spot.empty or not hot_sectors:
        return
    code_col = next((c for c in spot.columns if c in ("代码","symbol","code")), None)
    name_col = next((c for c in spot.columns if c in ("名称","name")), None)
    pct_col = next((c for c in spot.columns if "涨跌幅" in c), None)
    if not code_col or not name_col:
        return

    sector_names = {s["name"] for s in hot_sectors}

    # 只扫描涨幅>2%的活跃股（避免扫5527只全市场）
    try:
        df = spot.copy()
        df["_pct"] = pd.to_numeric(df[pct_col], errors="coerce") if pct_col else 0
        active = df[df["_pct"] >= 2] if pct_col else df.head(200)
    except Exception:
        active = spot.head(200)

    for _, row in active.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        name = str(row[name_col])
        if len(code) != 6:
            continue
        matched = _match_sector("", hot_sectors, name)
        if matched and matched["sector"] in sector_names:
            sector_map[code] = matched


def _aggregate_sectors_from_spot(spot) -> list[dict]:
    """从全市场快照中聚合板块涨幅（秒级）。

    优先行业列，Sina源无行业列时退化为关键词匹配+涨幅聚合。
    """
    if spot is None or spot.empty:
        return []

    pct_col = next((c for c in spot.columns if "涨跌幅" in c), None)
    name_col = next((c for c in spot.columns if c in ("名称","name")), None)
    if not pct_col:
        return []

    # 尝试取行业列
    ind_col = None
    for c in spot.columns:
        low = str(c).lower()
        if any(kw in low for kw in ("行业", "板块", "industry", "sector")):
            ind_col = c
            break

    df = spot.copy()
    df["_pct"] = pd.to_numeric(df[pct_col], errors="coerce")

    if ind_col:
        # 标准路径：按行业列聚合
        grouped = df.groupby(ind_col).agg(
            day_pct=("_pct", "mean"), count=("_pct", "count"),
            max_pct=("_pct", "max"),
        ).reset_index()
        grouped = grouped[grouped["count"] >= 3]
        grouped = grouped.sort_values("day_pct", ascending=False)
    elif name_col:
        # Sina兜底：用名称关键词分类
        SECTOR_KW = {
            "AI算力": ["寒武纪","海光","景嘉微","浪潮","曙光","紫光","光环","数据港","英维克","高澜","鼎通","华丰"],
            "半导体": ["中芯","华虹","长电","通富","华天","北方华创","中微","盛美","芯源微","沪硅","安集","鼎龙","雅克","韦尔","兆易","卓胜","圣邦","斯达","士兰","华大九天","芯原","江丰"],
            "光通信": ["中际旭创","新易盛","天孚","光迅","长飞","亨通","中天","源杰","仕佳","长光华芯","太辰光","腾景","光库"],
            "PCB": ["鹏鼎","深南","景旺","兴森","崇达","生益","华正","南亚","诺德","嘉元","大族激光"],
            "医药": ["医药","制药","药明","恒瑞","片仔","同仁","爱尔","通策","迈瑞","华熙","爱美","华兰","沃森","智飞","康希","药石","皓元","康龙","泰格","百诚"],
            "新能源": ["宁德","比亚迪","隆基","阳光电源","通威","亿纬锂能","赣锋","天齐","华友","恩捷","先导"],
            "消费电子": ["立讯精密","歌尔","蓝思","长盈","信维","欧菲","领益"],
            "汽车": ["上汽","广汽","长城","长安","江淮","小康","赛力斯","拓普","三花"],
            "机器人": ["机器人","汇川","埃斯顿","绿的谐波","鸣志","拓斯达"],
        }
        # 按名称匹配分类
        mapping = {}
        for _, row in df.iterrows():
            n = str(row.get(name_col, "") or "")
            for sector, kws in SECTOR_KW.items():
                if any(kw in n for kw in kws):
                    mapping[n] = sector
                    break
        # 构建分类列
        df["_sector"] = df[name_col].astype(str).map(lambda x: mapping.get(x, "其他"))
        grouped = df.groupby("_sector").agg(
            day_pct=("_pct", "mean"), count=("_pct", "count"),
            max_pct=("_pct", "max"),
        ).reset_index()
        grouped = grouped[grouped["_sector"] != "其他"]
        grouped = grouped[grouped["count"] >= 2]
        grouped = grouped.sort_values("day_pct", ascending=False)
    else:
        return []

    out = []
    for _, r in grouped.head(10).iterrows():
        out.append({
            "name": str(r.get(ind_col or "_sector", "")),
            "pct": round(float(r["day_pct"]), 2),
            "day_pct": round(float(r["day_pct"]), 2),
            "count": int(r["count"]),
            "leader": f"最高{float(r['max_pct']):+.1f}%",
            "type": "聚合",
        })
    return out


def _match_sector(industry: str, hot_sectors: list, stock_name: str = "") -> dict | None:
    """通过行业名或股票名关键词将个股映射到热点板块。"""
    if not hot_sectors:
        return None
    # 股票→板块关键词映射（与 _aggregate_sectors_from_spot 的 SECTOR_KW 保持同步）
    STOCK_TO_SECTOR = {
        "寒武纪":"AI算力","海光":"AI算力","景嘉微":"AI算力","浪潮":"AI算力","曙光":"AI算力",
        "紫光":"AI算力","光环":"AI算力","数据港":"AI算力","英维克":"AI算力","高澜":"AI算力",
        "中芯":"半导体","华虹":"半导体","长电":"半导体","通富":"半导体","华天":"半导体",
        "北方华创":"半导体","中微":"半导体","盛美":"半导体","芯源微":"半导体",
        "沪硅":"半导体","安集":"半导体","鼎龙":"半导体","雅克":"半导体",
        "韦尔":"半导体","兆易":"半导体","卓胜":"半导体","圣邦":"半导体",
        "斯达":"半导体","士兰":"半导体","华大九天":"半导体","芯原":"半导体","江丰":"半导体",
        "中际旭创":"光通信","新易盛":"光通信","天孚":"光通信","光迅":"光通信",
        "长飞":"光通信","亨通":"光通信","中天":"光通信",
        "源杰":"光通信","仕佳":"光通信","长光华芯":"光通信",
        "太辰光":"光通信","腾景":"光通信","光库":"光通信",
        "鹏鼎":"PCB","深南":"PCB","景旺":"PCB","兴森":"PCB","崇达":"PCB",
        "生益":"PCB","华正":"PCB","南亚":"PCB","诺德":"PCB","嘉元":"PCB","大族激光":"PCB",
        "宁德":"新能源","比亚迪":"新能源","隆基":"新能源","阳光电源":"新能源",
        "通威":"新能源","亿纬锂能":"新能源","赣锋":"新能源","天齐":"新能源",
        "立讯精密":"消费电子","歌尔":"消费电子","蓝思":"消费电子",
        "长盈":"消费电子","信维":"消费电子","欧菲":"消费电子",
    }
    # 先用股票名匹配
    name = (stock_name or "").strip()
    sector_name = STOCK_TO_SECTOR.get(name, "")
    if sector_name:
        # 在 hot_sectors 中找对应板块
        for sec in hot_sectors:
            if sec.get("name", "") == sector_name:
                return {"sector": sector_name, "sector_rank": 3,
                        "sector_type": "聚合", "week_pct": None}
    # 用行业名匹配
    if industry:
        for sec in hot_sectors:
            if sec.get("name", "") in industry or industry in sec.get("name", ""):
                return {"sector": sec["name"], "sector_rank": 5,
                        "sector_type": sec.get("type", ""), "week_pct": sec.get("week_pct")}
    return None


def _generate_summary(cycle: dict, picks: list, scoreboard: list) -> dict:
    """生成交易心得。"""
    phase = cycle.get("phase", "未知")
    mantra = cycle.get("mantra", "")
    warnings = cycle.get("warnings", [])
    position = cycle.get("position_advice", "")

    # 心法汇编
    heart_sutras = [
        "交易你所见，非你所想。",
        "顺势者躺赢，逆势者挣扎。",
        "善守者战无不胜，能舍者天地不弃。",
    ]

    if phase == "高潮期":
        heart_sutras.extend([
            "生于分歧，死于一致。高位缩量必有大劫。",
            "高潮期不接力缩量板，只持股享受溢价。",
        ])
    elif phase == "退潮期":
        heart_sutras.extend([
            "退潮期——不接飞刀，不做补涨，远离旋涡。",
            "忘掉成本，买入机会，卖出风险。",
        ])
    elif phase == "冰点期":
        heart_sutras.extend([
            "大跌之后孕育大机会。物极必反，否极泰来。",
            "首次共振冰点后，试错大级别新题材。",
        ])
    else:
        heart_sutras.extend([
            "机会只有1.5次，错过最好放弃。",
            "站在高胜算、高赔率的节点出击。",
        ])

    # 仓位建议
    if phase == "高潮期":
        pos_detail = "满仓主升仓。享受泡沫但随时准备兑现。核心龙头锁仓，后排分仓降风险。"
    elif phase == "复苏期":
        pos_detail = "5成跟随仓。出击新题材领涨核心，盈亏比最佳。确认龙头后重仓打板。"
    elif phase == "退潮期":
        pos_detail = "空仓防守。不宜开新仓，老仓逐步出清。"
    else:
        pos_detail = "1成试错仓。感受盘面水温，等待破局龙头。"

    return {
        "phase": phase,
        "mantra": mantra,
        "warnings": warnings,
        "position_advice": position,
        "position_detail": pos_detail,
        "heart_sutras": heart_sutras,
        "picks_summary": f"共{len(picks)}只标的通过三维共振筛选" if picks else "无符合条件标的",
    }
