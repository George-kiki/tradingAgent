"""自定义条件选股引擎。

两阶段筛选（高效 + 容错）：
1. 全市场实时快照初筛（涨幅/量比/换手率/总市值/板块）——一次调用过滤掉绝大多数，O(1)。
2. 对入围股逐只用日 K线验证技术条件（均线金叉且慢线向上、尾盘创新高）
   及资金流稳定性（主力资金净流入）。

所有条件均可单独启用/禁用（区间 min/max 为 None 表示不限），缺数据的条件按容错处理。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from core.indicators import sma
from data.fetcher import get_fetcher, normalize_symbol


def _eligible_board(code: str) -> bool:
    """剔除科创板688 / 创业板300·301 / 北交所8x·4x / B股9x。"""
    s = str(code).strip()
    if len(s) != 6 or not s.isdigit():
        return False
    if s.startswith(("688", "300", "301", "8", "4", "9")):
        return False
    return s.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


def _in_range(value, rng: Optional[dict]) -> bool:
    """区间判定；rng 为空或不限则视为通过；value 无效则不通过。"""
    if not rng:
        return True
    lo, hi = rng.get("min"), rng.get("max")
    if lo is None and hi is None:
        return True
    if value is None or (isinstance(value, float) and value != value):  # NaN
        return False
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


def _num(row, col):
    if not col:
        return None
    try:
        v = float(pd.to_numeric(row[col], errors="coerce"))
        return v if v == v else None
    except Exception:
        return None


class ScreenerEngine:
    def __init__(self):
        self.fetcher = get_fetcher()

    # ---------------- 快照列定位 ----------------
    @staticmethod
    def _cols(spot: pd.DataFrame) -> dict:
        def find(*kw):
            return next((c for c in spot.columns if all(k in c for k in kw)), None)
        return {
            "code": next((c for c in spot.columns if c in ("代码", "股票代码")), None),
            "name": next((c for c in spot.columns if "名称" in c), None),
            "price": next((c for c in spot.columns if c in ("最新价", "最新")), None),
            "pct": find("涨跌幅"),
            "vol_ratio": next((c for c in spot.columns if "量比" in c), None),
            "turnover": next((c for c in spot.columns if "换手" in c), None),
            "mv": next((c for c in spot.columns if "总市值" in c), None),
        }

    # ---------------- 主流程 ----------------
    def run(self, conditions: dict, limit: int = 30, max_kline: int = 80) -> dict:
        cond = conditions or {}
        spot = self.fetcher.get_market_spot()
        if spot is None or spot.empty:
            return {"error": "无法获取全市场行情快照（主备数据源均限流），请稍后重试或配置 Tushare。"}

        c = self._cols(spot)
        if not c["code"]:
            return {"error": "行情快照字段异常"}

        exclude_boards = cond.get("exclude_boards", True)
        pct_r = cond.get("pct_change")
        vol_r = cond.get("vol_ratio")
        to_r = cond.get("turnover")
        mc_r = cond.get("market_cap")

        # 备用源（如新浪）可能缺失这些列 -> 标记为"延后到候选阶段补算/降级"
        warnings = []
        deferred = set()
        for key, col in (("vol_ratio", c["vol_ratio"]), ("turnover", c["turnover"]),
                         ("market_cap", c["mv"])):
            if cond.get(key) and not col:
                deferred.add(key)

        # ---- 阶段一：快照初筛（仅筛快照中存在的字段）----
        prelim = []
        for _, row in spot.iterrows():
            code = normalize_symbol(str(row[c["code"]]))
            if exclude_boards and not _eligible_board(code):
                continue
            pct = _num(row, c["pct"])
            vol_ratio = _num(row, c["vol_ratio"]) if c["vol_ratio"] else None
            turnover = _num(row, c["turnover"]) if c["turnover"] else None
            mv_yi = (_num(row, c["mv"]) or 0) / 1e8 if c["mv"] else None  # 元 -> 亿
            if c["pct"] and not _in_range(pct, pct_r):
                continue
            if c["vol_ratio"] and not _in_range(vol_ratio, vol_r):
                continue
            if c["turnover"] and not _in_range(turnover, to_r):
                continue
            if c["mv"] and not _in_range(mv_yi, mc_r):
                continue
            prelim.append({
                "symbol": code,
                "name": str(row[c["name"]]) if c["name"] else code,
                "price": _num(row, c["price"]),
                "pct_change": round(pct, 2) if pct is not None else None,
                "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
                "turnover": round(turnover, 2) if turnover is not None else None,
                "market_cap": round(mv_yi, 1) if mv_yi else None,
            })

        total_scanned = int(len(spot))
        passed_snapshot = len(prelim)
        prelim.sort(key=lambda x: (x["pct_change"] or 0), reverse=True)
        candidates = prelim[:max_kline]

        # ---- 阶段二：K线技术条件 + 缺失字段补算 + 资金流 ----
        gc = cond.get("ma_golden_cross")
        nh = cond.get("new_high_at_close")
        need_fund = bool(cond.get("fund_flow_stable"))
        mv_uncomputable = False
        to_uncomputable = False
        vol_from_kline = False
        to_from_kline = False

        picks = []
        for item in candidates:
            if len(picks) >= limit:
                break
            try:
                ok, extra = self._check_kline(item["symbol"], gc, nh)
            except Exception:
                ok, extra = False, {}
            if not ok:
                continue

            # 量比：快照缺失时用 K线收盘口径精确值（与官方收盘量比一致）补齐并过滤
            if item.get("vol_ratio") is None and extra.get("vol_ratio") is not None:
                item["vol_ratio"] = extra["vol_ratio"]
            if "vol_ratio" in deferred:
                vol_from_kline = True
                if item.get("vol_ratio") is not None and not _in_range(item["vol_ratio"], vol_r):
                    continue

            # 换手率：快照缺失时用 K线自带的当日真实换手率补齐并过滤
            if item.get("turnover") is None and extra.get("turnover") is not None:
                item["turnover"] = extra["turnover"]
            if "turnover" in deferred:
                if item.get("turnover") is not None:
                    to_from_kline = True
                    if not _in_range(item["turnover"], to_r):
                        continue
                else:
                    to_uncomputable = True  # K线也无换手率，本条件对该股降级

            # 市值补取（基本信息）
            if "market_cap" in deferred:
                mv = self._fetch_market_cap(item["symbol"])
                if mv is not None:
                    item["market_cap"] = mv
                    if not _in_range(mv, mc_r):
                        continue
                else:
                    mv_uncomputable = True  # 补不到，本条件降级

            # 资金流稳定性
            if need_fund:
                stable, flow_txt = self._check_fund(item["symbol"])
                if not stable:
                    continue
                item["main_net_inflow"] = flow_txt

            extra.pop("vol_ratio", None)
            extra.pop("turnover", None)
            item.update(extra)
            item["matched"] = self._matched_labels(cond, item)
            item["reason"] = self._gen_reason(cond, item)
            picks.append(item)

        if vol_from_kline:
            warnings.append("量比由K线按收盘口径精确计算（与官方收盘量比一致）")
        if to_from_kline:
            warnings.append("换手率取自K线当日真实换手率")
        if mv_uncomputable:
            warnings.append("部分个股市值数据暂缺，市值条件对其未生效")
        if to_uncomputable:
            warnings.append("部分个股换手率暂缺，换手率条件对其未生效")

        return {
            "conditions": cond,
            "count": len(picks),
            "picks": picks,
            "total_scanned": total_scanned,
            "passed_snapshot": passed_snapshot,
            "kline_checked": len(candidates),
            "warnings": warnings,
        }

    def _fetch_market_cap(self, symbol: str) -> Optional[float]:
        """补取总市值（亿元），失败返回 None。"""
        try:
            info = self.fetcher.get_basic_info(symbol)
            mv = info.get("总市值")
            return round(float(mv) / 1e8, 1) if mv else None
        except Exception:
            return None

    # ---------------- K线条件 ----------------
    def _check_kline(self, symbol: str, gc: Optional[dict], nh: Optional[dict]):
        df = self.fetcher.get_kline(symbol, days=120)
        if df is None or df.empty or len(df) < 35:
            return False, {}
        close = df["close"]
        extra = {}
        # 量比（收盘口径）：今日成交量 / 前5日日均成交量。
        # 数学上等于官方收盘量比（收盘时当日开市分钟=240，公式化简后即此式），并非粗略估算。
        try:
            if "volume" in df.columns and len(df) >= 6:
                prev5 = float(df["volume"].iloc[-6:-1].mean())
                if prev5 > 0:
                    extra["vol_ratio"] = round(float(df["volume"].iloc[-1]) / prev5, 2)
        except Exception:
            pass
        # 换手率：东财历史K线自带每日真实换手率，直接取最新一日
        try:
            if "turnover" in df.columns:
                tv = float(pd.to_numeric(df["turnover"].iloc[-1], errors="coerce"))
                if tv == tv:  # 非 NaN
                    extra["turnover"] = round(tv, 2)
        except Exception:
            pass

        # 均线金叉且慢线向上
        if gc:
            fast = int(gc.get("fast", 5))
            slow = int(gc.get("slow", 30))
            within = int(gc.get("within", 5))
            ma_f = sma(close, fast)
            ma_s = sma(close, slow)
            if ma_f.iloc[-1] <= ma_s.iloc[-1]:
                return False, {}  # 当前快线须在慢线上方
            # 近 within 日内发生金叉
            crossed = False
            for i in range(-within, 0):
                if i - 1 < -len(ma_f):
                    continue
                if ma_f.iloc[i - 1] <= ma_s.iloc[i - 1] and ma_f.iloc[i] > ma_s.iloc[i]:
                    crossed = True
                    break
            if not crossed:
                return False, {}
            if gc.get("slow_rising"):
                if len(ma_s) < 6 or not (ma_s.iloc[-1] > ma_s.iloc[-6]):
                    return False, {}
            extra["ma_cross"] = f"{fast}日金叉{slow}日" + ("·慢线向上" if gc.get("slow_rising") else "")

        # 尾盘（收盘）创 N 日新高
        if nh:
            window = int(nh.get("window", 20))
            recent_high = df["high"].iloc[-window:-1].max() if len(df) > window else df["high"].iloc[:-1].max()
            if not (close.iloc[-1] >= recent_high):
                return False, {}
            extra["new_high"] = f"收盘创{window}日新高"

        # K线迷你（前端卡片）
        tail = df.tail(60)
        extra["kline_mini"] = {
            "dates": tail["date"].tolist(),
            "candles": [[round(float(o), 2), round(float(cl), 2), round(float(lo), 2), round(float(hi), 2)]
                        for o, cl, lo, hi in zip(tail["open"], tail["close"], tail["low"], tail["high"])],
        }
        return True, extra

    # ---------------- 资金流稳定性 ----------------
    def _check_fund(self, symbol: str):
        """主力资金净流入 >= 0 视为稳定；取不到则按容错通过（不误杀）。"""
        try:
            flow = self.fetcher.get_fund_flow(symbol)
            main_net = flow.get("主力净流入-净额") or flow.get("主力净流入")
            if main_net is None:
                return True, "—"  # 数据缺失，容错通过
            v = float(main_net)
            txt = (f"+{v/1e8:.2f}亿" if v >= 1e8 else f"+{v/1e4:.0f}万") if v >= 0 else \
                  (f"{v/1e8:.2f}亿" if abs(v) >= 1e8 else f"{v/1e4:.0f}万")
            return v >= 0, txt
        except Exception:
            return True, "—"

    # ---------------- 文案 ----------------
    @staticmethod
    def _matched_labels(cond: dict, item: dict) -> list:
        labels = []
        if cond.get("pct_change"):
            labels.append(f"涨幅{item.get('pct_change')}%")
        if cond.get("vol_ratio") and item.get("vol_ratio") is not None:
            labels.append(f"量比{item.get('vol_ratio')}")
        if cond.get("turnover") and item.get("turnover") is not None:
            labels.append(f"换手{item.get('turnover')}%")
        if item.get("ma_cross"):
            labels.append(item["ma_cross"])
        if item.get("new_high"):
            labels.append(item["new_high"])
        if cond.get("fund_flow_stable"):
            labels.append("资金稳定")
        return labels

    @staticmethod
    def _gen_reason(cond: dict, item: dict) -> str:
        parts = []
        if item.get("pct_change") is not None:
            parts.append(f"今日涨{item['pct_change']}%")
        if item.get("market_cap"):
            parts.append(f"市值{item['market_cap']}亿")
        if item.get("turnover") is not None:
            parts.append(f"换手{item['turnover']}%")
        if item.get("vol_ratio") is not None:
            parts.append(f"量比{item['vol_ratio']}")
        if item.get("ma_cross"):
            parts.append(item["ma_cross"])
        if item.get("new_high"):
            parts.append(item["new_high"])
        if item.get("main_net_inflow") and item["main_net_inflow"] != "—":
            parts.append(f"主力{item['main_net_inflow']}")
        return "；".join(parts) + "。"


def run_screener(conditions: dict, limit: int = 30) -> dict:
    return ScreenerEngine().run(conditions, limit=limit)
