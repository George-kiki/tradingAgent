"""胜率计算与结算。

== 胜率计算规则 ==
1. 每个推荐以"入场参考价" = 推荐所依据的前一日(base_date)收盘价。
2. "评估日" = base_date 之后的下一交易日（eval_date）。
3. 次日涨跌幅 next_pct = (eval_close / entry_price - 1) * 100。
4. 判定"赢"：next_pct > win_pct_threshold（默认 0，即次日收红即赢；可配置为
   跑赢阈值，如 0.5 表示需涨超 0.5% 才算赢）。
5. 批次胜率 win_rate = 赢的只数 / 已评估总只数（0~1）。
6. 同时记录批次平均次日收益 avg_return、最佳/最差个股，供反思与展示。

只有当某推荐批次(base_date)的"下一交易日"数据已经存在时，该批次才可被结算。
因此结算天然滞后一个交易日，符合 A 股 T+1 与"昨日荐股今日见分晓"的逻辑。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from core.config import settings
from data.fetcher import get_fetcher
from recommend.database import RecommendDB


def _next_trading_close(df: pd.DataFrame, base_date: str) -> Optional[tuple[str, float]]:
    """在 K线中找到 base_date 的下一交易日，返回 (日期, 收盘价)。"""
    if df is None or df.empty or "date" not in df.columns:
        return None
    dates = df["date"].tolist()
    if base_date not in dates:
        # base_date 可能非交易日：取严格大于它的第一根
        after = [i for i, d in enumerate(dates) if d > base_date]
        if not after:
            return None
        idx = after[0]
        return dates[idx], float(df.iloc[idx]["close"])
    i = dates.index(base_date)
    if i + 1 >= len(dates):
        return None  # 还没有次日数据，不可结算
    return dates[i + 1], float(df.iloc[i + 1]["close"])


def settle_batch(db: RecommendDB, base_date: str, fetcher=None,
                 mode: str = "daily") -> Optional[dict]:
    """结算某个推荐批次：逐只计算次日收益、判定胜负，并汇总胜率。

    返回该批次胜率汇总 dict；若次日数据尚不可得则返回 None（未结算）。

    结算策略：
    - 优先使用 K线次日收盘价（最标准，T+1日K线）
    - K线无次日数据时（如收盘后K线尚未更新），回退到全市场快照的当日收盘价
    """
    fetcher = fetcher or get_fetcher()
    recs = db.get_recommendations(base_date, mode=mode)
    if not recs:
        return None

    import datetime as _dt
    today_str = _dt.date.today().isoformat()

    # 只有确认次日数据已存在时才结算
    # base_date 的次日 = today → 可结算（盘后快照已有收盘价）
    # base_date 的次日 > today → 不可结算（未来）
    try:
        bd = _dt.date.fromisoformat(base_date)
        eval_date_expected = (bd + _dt.timedelta(days=1)).isoformat()
    except Exception:
        eval_date_expected = today_str

    # 若预期评估日还没到，提前退出
    if eval_date_expected > today_str:
        return None

    # 预拉取全市场快照（供K线缺失时兜底），带缓存TTL避免重复拉取
    spot = None

    def _spot_close(symbol: str) -> Optional[tuple[str, float]]:
        """从快照中取 symbol 的当日收盘价作为评估价。"""
        nonlocal spot
        if spot is None:
            try:
                from data.cache import invalidate_pattern
                invalidate_pattern("spot:all:")
                spot = fetcher.get_market_spot()
            except Exception:
                return None
        if spot is None or spot.empty:
            return None
        code_col = next((c for c in spot.columns if c in ("代码", "symbol", "code")), None)
        price_col = next((c for c in spot.columns if "最新价" in c or "现价" in c), None)
        if not code_col or not price_col:
            return None
        target = str(symbol).strip().zfill(6)
        row = spot[spot[code_col].astype(str).str.strip().str.zfill(6) == target]
        if row.empty:
            return None
        return today_str, float(row.iloc[0][price_col])

    win_th = settings.win_pct_threshold
    settled = 0
    for rec in recs:
        if db.has_result(base_date, rec["symbol"], mode=mode):
            settled += 1
            continue

        sym = rec["symbol"]
        df = fetcher.get_kline(sym, days=400)
        nxt = _next_trading_close(df, base_date)

        # K线无次日数据时 → 用今日快照收盘价兜底（收盘后K线尚未更新场景）
        if nxt is None and eval_date_expected == today_str:
            nxt = _spot_close(sym)

        if nxt is None:
            continue

        eval_date, eval_close = nxt
        entry = rec.get("entry_price")
        if not entry:
            row = df[df["date"] == base_date]
            entry = float(row.iloc[0]["close"]) if not row.empty else eval_close
        next_pct = round((eval_close / entry - 1) * 100, 2)
        is_win = 1 if next_pct > win_th else 0
        note = "次日上涨" if is_win else "次日未达标"
        db.save_result(rec["id"], base_date, eval_date, rec["symbol"],
                       round(float(entry), 2), round(eval_close, 2),
                       next_pct, is_win, note, mode=mode)
        settled += 1

    results = db.get_results(base_date, mode=mode)
    if not results:
        return None
    total = len(results)
    wins = sum(r["is_win"] for r in results)
    win_rate = round(wins / total, 4) if total else 0.0
    avg_return = round(sum(r["next_pct"] for r in results) / total, 2) if total else 0.0
    best = max(results, key=lambda r: r["next_pct"])
    worst = min(results, key=lambda r: r["next_pct"])
    db.save_winrate(base_date, total, wins, win_rate, avg_return,
                    best["symbol"], worst["symbol"])
    return db.get_winrate(base_date)


def settle_all_pending(db: RecommendDB, fetcher=None) -> list[dict]:
    """结算所有可结算但尚未结算的历史批次（含 daily 和 tail 两种模式）。"""
    fetcher = fetcher or get_fetcher()
    out = []
    for mode in ("daily", "tail"):
        for base_date in db.all_recommendation_dates(mode=mode):
            wr = db.get_winrate(base_date)
            recs = db.get_recommendations(base_date, mode=mode)
            results = db.get_results(base_date, mode=mode)
            if wr and len(results) >= len(recs):
                continue
            settled = settle_batch(db, base_date, fetcher, mode=mode)
            if settled:
                out.append(settled)
    return out
