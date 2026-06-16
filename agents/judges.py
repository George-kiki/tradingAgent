"""投资大佬评审团（规则驱动，灵感参考 UZI-Skill 的多评委审判席）。

每位投资大佬有自己的量化规则集：基于个股的估值/盈利/成长/财务健康/技术面/资金面
指标，输出一盏灯（bull 看多 / bear 看空 / neutral 中性）+ 0-100 评分 + 命中规则点评。
全部规则驱动，**不依赖 LLM**，无 API key 也能产出完整评审团结果。

评委覆盖多流派：经典价值 / 成长投资 / 宏观对冲 / 技术趋势 / 中国价投 / A股游资 / 量化系统。

汇总：
- consensus 共识分 = (看多数 + 0.6×中性数) / 有效评委数 × 100
- 多空大分歧 = 最看多评委 vs 最看空评委

免责声明：所有评委评语均为算法基于公开数据的模拟输出，不代表任何真实投资者观点，
不构成投资建议。
"""
from __future__ import annotations

from typing import Optional


def _num(v) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None  # NaN 检查
    except Exception:
        return None


class JudgeContext:
    """评委评估所需的精简指标集（从 orchestrator 的数据中抽取）。"""

    def __init__(self, metrics: dict, snapshot: dict, quant: dict,
                 scorecard: dict, fund_flow: dict):
        m = metrics or {}
        s = snapshot or {}
        self.pe = _num(m.get("pe"))
        self.pb = _num(m.get("pb"))
        self.ps = _num(m.get("ps"))
        self.roe = _num(m.get("roe"))
        self.gross = _num(m.get("gross_margin"))
        self.net_margin = _num(m.get("net_margin"))
        self.debt = _num(m.get("debt_ratio"))
        self.rev_g = _num(m.get("revenue_growth"))
        self.profit_g = _num(m.get("profit_growth"))
        self.dv = _num(m.get("dv_ratio"))
        self.total_mv = _num(m.get("total_mv"))
        # 技术面
        self.close = _num(s.get("close"))
        self.ma20 = _num(s.get("ma20"))
        self.ma60 = _num(s.get("ma60"))
        self.rsi = _num(s.get("rsi"))
        self.momentum = _num(s.get("momentum"))
        self.macd_hist = _num(s.get("macd_hist"))
        self.change_pct = _num(s.get("change_pct"))
        # 量化与资金
        self.quant_score = _num((quant or {}).get("avg_score")) or 0.0
        self.buy_signals = (quant or {}).get("buy_signals", 0)
        self.sell_signals = (quant or {}).get("sell_signals", 0)
        self.sc_good = (scorecard or {}).get("good_count", 0)
        self.sc_bad = (scorecard or {}).get("bad_count", 0)
        ff = fund_flow or {}
        # 主力净流入（best-effort 从常见键提取，取不到则 None）
        self.main_inflow = None
        for k, v in ff.items():
            if "主力" in str(k) and ("净" in str(k) or "流入" in str(k)):
                self.main_inflow = str(v)
                break

    def ma_bull(self) -> bool:
        return bool(self.close and self.ma20 and self.ma60
                    and self.close > self.ma20 > self.ma60)


# ---------------- 评委规则集 ----------------
# 每个评委：name/流派/规则函数。规则函数返回 (light, score, note, hits)
#   light ∈ {"bull","bear","neutral"}；score 0-100；hits 命中规则文字列表。

def _clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def _judge_buffett(c: JudgeContext):
    """巴菲特：护城河 + 合理估值 + 高 ROE + 低负债。"""
    score, hits = 50, []
    if c.roe is not None:
        if c.roe >= 15:
            score += 18; hits.append(f"ROE {c.roe}% ≥15%（优质盈利）")
        elif c.roe < 8:
            score -= 15; hits.append(f"ROE {c.roe}% 偏低")
    if c.pe is not None and 0 < c.pe <= 25:
        score += 10; hits.append(f"PE {c.pe} 合理")
    elif c.pe is not None and c.pe > 40:
        score -= 14; hits.append(f"PE {c.pe} 偏贵，缺乏安全边际")
    if c.debt is not None and c.debt <= 45:
        score += 8; hits.append(f"负债率 {c.debt}% 健康")
    elif c.debt is not None and c.debt > 65:
        score -= 10; hits.append(f"负债率 {c.debt}% 偏高")
    return _light(score), _clamp(score), _note(score, "护城河与盈利质量"), hits


def _judge_graham(c: JudgeContext):
    """格雷厄姆：极致安全边际，低 PE/PB。"""
    score, hits = 50, []
    if c.pb is not None:
        if c.pb <= 1.5:
            score += 20; hits.append(f"PB {c.pb} ≤1.5（深度安全边际）")
        elif c.pb > 4:
            score -= 18; hits.append(f"PB {c.pb} >4，无安全边际")
    if c.pe is not None:
        if 0 < c.pe <= 15:
            score += 16; hits.append(f"PE {c.pe} ≤15")
        elif c.pe > 30 or c.pe <= 0:
            score -= 16; hits.append(f"PE {c.pe} 不符价值标准")
    return _light(score), _clamp(score), _note(score, "安全边际"), hits


def _judge_lynch(c: JudgeContext):
    """彼得·林奇：PEG 视角，成长与估值匹配。"""
    score, hits = 50, []
    if c.profit_g is not None and c.pe is not None and c.pe > 0:
        peg = c.pe / c.profit_g if c.profit_g > 0 else None
        if peg is not None and 0 < peg <= 1:
            score += 22; hits.append(f"PEG≈{round(peg,2)} ≤1（成长股甜点区）")
        elif peg is not None and peg > 2:
            score -= 16; hits.append(f"PEG≈{round(peg,2)} >2，成长配不上估值")
    if c.profit_g is not None and c.profit_g >= 25:
        score += 12; hits.append(f"净利增速 {c.profit_g}% 高成长")
    elif c.profit_g is not None and c.profit_g < 0:
        score -= 14; hits.append(f"净利增速 {c.profit_g}% 负增长")
    return _light(score), _clamp(score), _note(score, "成长与估值匹配"), hits


def _judge_wood(c: JudgeContext):
    """木头姐：高成长、不拘泥估值。"""
    score, hits = 50, []
    if c.rev_g is not None and c.rev_g >= 20:
        score += 20; hits.append(f"营收增速 {c.rev_g}% 高成长赛道")
    elif c.rev_g is not None and c.rev_g < 5:
        score -= 12; hits.append(f"营收增速 {c.rev_g}% 乏力")
    if c.gross is not None and c.gross >= 40:
        score += 12; hits.append(f"毛利率 {c.gross}% 高（科技/品牌属性）")
    if c.momentum is not None and c.momentum > 0:
        score += 6; hits.append("近期动量为正")
    return _light(score), _clamp(score), _note(score, "颠覆式成长"), hits


def _judge_soros(c: JudgeContext):
    """索罗斯：反身性 + 趋势/动量。"""
    score, hits = 50, []
    if c.momentum is not None:
        if c.momentum > 8:
            score += 16; hits.append(f"动量 {c.momentum}% 强趋势")
        elif c.momentum < -8:
            score -= 16; hits.append(f"动量 {c.momentum}% 趋势走弱")
    if c.ma_bull():
        score += 10; hits.append("多头排列（趋势确认）")
    if c.rsi is not None and c.rsi > 80:
        score -= 10; hits.append(f"RSI {c.rsi} 过热，反身性见顶风险")
    return _light(score), _clamp(score), _note(score, "趋势反身性"), hits


def _judge_livermore(c: JudgeContext):
    """利弗莫尔：只做强势趋势股。"""
    score, hits = 50, []
    if c.ma_bull():
        score += 18; hits.append("站上 20/60 日线，趋势向上")
    elif c.close and c.ma20 and c.close < c.ma20:
        score -= 14; hits.append("跌破 20 日线，趋势走坏")
    if c.macd_hist is not None and c.macd_hist > 0:
        score += 8; hits.append("MACD 红柱（多头动能）")
    if c.rsi is not None and c.rsi < 30:
        score -= 6; hits.append(f"RSI {c.rsi} 弱势，不抄底")
    return _light(score), _clamp(score), _note(score, "强势趋势"), hits


def _judge_duan(c: JudgeContext):
    """段永平：商业模式 + 长期 ROE + 合理价格。"""
    score, hits = 50, []
    if c.roe is not None and c.roe >= 15:
        score += 16; hits.append(f"ROE {c.roe}% 长期盈利能力强")
    if c.net_margin is not None and c.net_margin >= 15:
        score += 12; hits.append(f"净利率 {c.net_margin}% 高（定价权）")
    if c.pe is not None and c.pe > 35:
        score -= 12; hits.append(f"PE {c.pe} 偏贵，等回调")
    return _light(score), _clamp(score), _note(score, "好生意好价格"), hits


def _judge_zhangkun(c: JudgeContext):
    """张坤：高 ROE + 高壁垒消费/医药风格。"""
    score, hits = 50, []
    if c.roe is not None and c.roe >= 18:
        score += 18; hits.append(f"ROE {c.roe}% 极高（高壁垒）")
    if c.gross is not None and c.gross >= 50:
        score += 12; hits.append(f"毛利率 {c.gross}% 强护城河")
    if c.debt is not None and c.debt <= 40:
        score += 6; hits.append("低负债经营稳健")
    if c.pe is not None and c.pe > 45:
        score -= 10; hits.append(f"PE {c.pe} 透支未来")
    return _light(score), _clamp(score), _note(score, "高质量龙头"), hits


def _judge_zhaolaoge(c: JudgeContext):
    """赵老哥（游资）：只做最强趋势 + 资金博弈。"""
    score, hits = 50, []
    if c.momentum is not None and c.momentum > 6:
        score += 16; hits.append(f"动量 {c.momentum}% 强势")
    if c.buy_signals and c.buy_signals > c.sell_signals:
        score += 12; hits.append(f"{c.buy_signals} 个策略看多")
    if c.change_pct is not None and c.change_pct > 5:
        score += 8; hits.append(f"今日 +{c.change_pct}% 资金抢筹")
    if c.ma_bull():
        score += 6; hits.append("多头排列")
    if c.momentum is not None and c.momentum < -5:
        score -= 18; hits.append("退潮趋势，不接飞刀")
    return _light(score), _clamp(score), _note(score, "最强趋势游资"), hits


def _judge_simons(c: JudgeContext):
    """西蒙斯（量化）：综合量化信号 + 多因子。"""
    score, hits = 50, []
    qs = c.quant_score
    score += int(_clamp(qs * 60, -30, 35))
    if qs > 0.2:
        hits.append(f"多因子量化评分 {round(qs,2)} 偏多")
    elif qs < -0.2:
        hits.append(f"多因子量化评分 {round(qs,2)} 偏空")
    else:
        hits.append(f"多因子量化评分 {round(qs,2)} 中性")
    if c.buy_signals + c.sell_signals > 0:
        net = c.buy_signals - c.sell_signals
        score += net * 3
        hits.append(f"策略净信号 {net:+d}（{c.buy_signals}买/{c.sell_signals}卖）")
    return _light(score), _clamp(score), _note(score, "量化系统"), hits


def _judge_munger(c: JudgeContext):
    """芒格：避免愚蠢 + 质量优先。"""
    score, hits = 50, []
    if c.sc_good >= c.sc_bad and (c.sc_good + c.sc_bad) > 0:
        score += 14; hits.append(f"评分卡 {c.sc_good} 优 ≥ {c.sc_bad} 差")
    elif c.sc_bad > c.sc_good:
        score -= 14; hits.append(f"评分卡 {c.sc_bad} 差项偏多")
    if c.debt is not None and c.debt > 70:
        score -= 12; hits.append(f"负债率 {c.debt}% 过高（避坑）")
    if c.roe is not None and c.roe >= 15:
        score += 8; hits.append(f"ROE {c.roe}% 质量达标")
    return _light(score), _clamp(score), _note(score, "理性避坑"), hits


def _light(score: float) -> str:
    if score >= 62:
        return "bull"
    if score <= 42:
        return "bear"
    return "neutral"


def _note(score: float, theme: str) -> str:
    if score >= 75:
        return f"看多：{theme}突出，值得重点关注"
    if score >= 62:
        return f"偏多：{theme}良好，可跟踪"
    if score <= 38:
        return f"看空：{theme}存明显短板，回避"
    if score <= 42:
        return f"偏空：{theme}偏弱，谨慎"
    return f"中性：{theme}一般，观望"


# 评委注册表：(姓名, 流派, 规则函数)
JUDGES = [
    ("巴菲特", "经典价值", _judge_buffett),
    ("格雷厄姆", "经典价值", _judge_graham),
    ("芒格", "经典价值", _judge_munger),
    ("彼得·林奇", "成长投资", _judge_lynch),
    ("木头姐 Cathie Wood", "成长投资", _judge_wood),
    ("索罗斯", "宏观对冲", _judge_soros),
    ("利弗莫尔", "技术趋势", _judge_livermore),
    ("段永平", "中国价投", _judge_duan),
    ("张坤", "中国价投", _judge_zhangkun),
    ("赵老哥", "A股游资", _judge_zhaolaoge),
    ("西蒙斯", "量化系统", _judge_simons),
]


def run_jury(metrics: dict, snapshot: dict, quant: dict,
             scorecard: dict, fund_flow: dict) -> dict:
    """运行评审团，返回每位评委的灯/评分/点评 + 共识 + 多空大分歧。"""
    c = JudgeContext(metrics, snapshot, quant, scorecard, fund_flow)
    judges = []
    for name, school, fn in JUDGES:
        try:
            light, score, note, hits = fn(c)
        except Exception:
            light, score, note, hits = "neutral", 50, "数据不足，暂持中性", []
        judges.append({
            "name": name, "school": school, "light": light,
            "score": int(round(score)), "note": note,
            "hits": hits[:3],
        })

    active = len(judges)
    bull = sum(1 for j in judges if j["light"] == "bull")
    bear = sum(1 for j in judges if j["light"] == "bear")
    neutral = active - bull - bear
    consensus = round((bull + 0.6 * neutral) / active * 100, 1) if active else 50.0

    ranked = sorted(judges, key=lambda j: j["score"], reverse=True)
    most_bull = ranked[0] if ranked else None
    most_bear = ranked[-1] if ranked else None

    return {
        "judges": judges,
        "stats": {"active": active, "bull": bull, "bear": bear, "neutral": neutral},
        "consensus": consensus,
        "divide": {
            "bull": most_bull,
            "bear": most_bear,
        } if (most_bull and most_bear and most_bull["name"] != most_bear["name"]) else None,
    }
