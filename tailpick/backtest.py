"""尾盘荐股深度回测系统。

=== 交易规则 ===
- 买入时间: T日 14:50（折价+1%滑点模拟尾盘买入）
- 卖出时间: T+1日 开盘价（模拟次日开盘卖出）
- 仓位: 等权分配，单票最多20%仓位
- 交易成本: 佣金万2.5 + 滑点0.1%

=== 核心指标 ===
- 年化收益率 (Annualized Return)
- 最大回撤 (Max Drawdown) + 回撤区间
- 胜率 (Win Rate): 收盘盈利天数/总交易日
- 盈亏比 (Profit Factor): 总盈利/总亏损
- 夏普比率 (Sharpe Ratio): 无风险利率3%
- 卡玛比率 (Calmar Ratio): 年化收益/最大回撤
- 月度胜率分布

=== 市场环境分层 ===
- 牛/熊/震荡市判定基于上证20日/60日均线位
- 各环境下独立评估策略稳定性
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from collections import defaultdict
from typing import Optional

import numpy as np
import pandas as pd


# ============================================================
# 配置
# ============================================================
COMMISSION_RATE = 0.00025   # 万2.5 佣金
SLIPPAGE = 0.001            # 0.1% 滑点
RISK_FREE_RATE = 0.03       # 3% 无风险利率
MAX_POSITION_PCT = 0.20     # 单票最大仓位20%
INITIAL_CAPITAL = 1_000_000 # 初始资金 100万


# ============================================================
# 数据准备：从数据库读取历史荐股记录
# ============================================================

def load_tail_trades(db_path: str = "data_store/recommend.db",
                     mode: str = "tail") -> pd.DataFrame:
    """从 SQLite 读取尾盘荐股历史记录，拼接成可回测的交易序列。

    Returns DataFrame:
        base_date, eval_date, symbol, name, entry_price, exit_price,
        next_pct, is_win, rank, score
    """
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 跨表联查
    sql = """
        SELECT
            r.base_date, r.symbol, r.entry_price,
            rr.eval_date, rr.eval_price, rr.next_pct, rr.is_win,
            r.rank, r.score
        FROM recommendations r
        LEFT JOIN recommendation_results rr
            ON r.base_date = rr.base_date AND r.symbol = rr.symbol
        WHERE r.mode = ?
          AND r.entry_price IS NOT NULL
          AND rr.eval_price IS NOT NULL
        ORDER BY r.base_date, r.rank
    """
    df = pd.read_sql_query(sql, conn, params=(mode,))
    conn.close()

    if df.empty:
        return df

    df["base_date"] = pd.to_datetime(df["base_date"])
    df["eval_date"] = pd.to_datetime(df["eval_date"])
    df["entry_price"] = df["entry_price"].astype(float)
    df["eval_price"] = df["eval_price"].astype(float)
    df["next_pct"] = df["next_pct"].astype(float)
    df["is_win"] = df["is_win"].astype(int)
    df["rank"] = df["rank"].astype(int)
    df["score"] = df["score"].astype(float)

    return df


def resolve_stock_name(fetcher, symbol: str) -> str:
    try: return fetcher.get_name(symbol) or symbol
    except: return symbol


# ============================================================
# 市场环境判定
# ============================================================

def classify_market_regimes(trades: pd.DataFrame,
                            fetcher=None,
                            index_code: str = "sh000001") -> pd.DataFrame:
    """为每笔交易标注市场环境（bull/sideways/bear）。

    使用上证20日与60日均线相对位置判定：
    - bull:  价格在60日均线上方 且 20日均线向上
    - bear:  价格在60日均线下方 且 20日均线向下
    - sideways: 其余
    """
    if trades.empty:
        trades["regime"] = "sideways"
        return trades

    # 获取上证历史K线
    if fetcher is None:
        from data.fetcher import get_fetcher
        fetcher = get_fetcher()

    try:
        idx_kline = fetcher.get_index_kline(index_code, days=400)
    except Exception:
        trades["regime"] = "sideways"
        return trades

    if idx_kline is None or idx_kline.empty:
        trades["regime"] = "sideways"
        return trades

    idx_kline["date"] = pd.to_datetime(idx_kline["date"])
    idx_kline = idx_kline.sort_values("date").set_index("date")
    idx_kline["ma20"] = idx_kline["close"].rolling(20).mean()
    idx_kline["ma60"] = idx_kline["close"].rolling(60).mean()
    idx_kline["ma20_slope"] = idx_kline["ma20"].pct_change(5)

    regimes = {}
    for d in idx_kline.index:
        row = idx_kline.loc[d]
        c = float(row["close"])
        ma20 = float(row["ma20"]) if not pd.isna(row["ma20"]) else c
        ma60 = float(row["ma60"]) if not pd.isna(row["ma60"]) else c
        slope = float(row["ma20_slope"]) if not pd.isna(row["ma20_slope"]) else 0

        if c > ma60 and slope > 0.002:
            regimes[d.strftime("%Y-%m-%d")] = "bull"
        elif c < ma60 and slope < -0.002:
            regimes[d.strftime("%Y-%m-%d")] = "bear"
        else:
            regimes[d.strftime("%Y-%m-%d")] = "sideways"

    trades["regime"] = trades["base_date"].apply(
        lambda d: regimes.get(d.strftime("%Y-%m-%d"), "sideways"))
    return trades


# ============================================================
# 回测引擎
# ============================================================

class TailBacktest:
    """尾盘策略完整回测引擎。"""

    def __init__(self, capital: float = INITIAL_CAPITAL,
                 commission: float = COMMISSION_RATE,
                 slippage: float = SLIPPAGE,
                 max_pos: float = MAX_POSITION_PCT):
        self.capital = capital
        self.commission = commission
        self.slippage = slippage
        self.max_pos = max_pos
        self.equity_curve: list[dict] = []
        self.trade_log: list[dict] = []
        self.metrics: dict = {}
        self.regime_metrics: dict = {}

    def run(self, trades: pd.DataFrame) -> dict:
        """执行回测。"""
        if trades.empty:
            return {"error": "无交易数据"}

        trades = trades.sort_values("base_date").reset_index(drop=True)

        cash = self.capital
        equity_peak = self.capital
        max_dd = 0.0
        max_dd_start = max_dd_end = ""

        # 按交易日分组
        by_date = defaultdict(list)
        for _, t in trades.iterrows():
            by_date[t["base_date"].strftime("%Y-%m-%d")].append(t)

        dates = sorted(by_date.keys())

        for date_str in dates:
            day_trades = by_date[date_str]
            n_picks = len(day_trades)

            if n_picks == 0:
                continue

            # 等权分配（单票不超过max_pos）
            pos_pct = min(1.0 / max(n_picks, 1), self.max_pos)
            total_pos = pos_pct * n_picks

            # 实际投入资金（不超过可用现金）
            position_capital = cash * total_pos
            per_stock_capital = cash * pos_pct

            for t in day_trades:
                entry = float(t["entry_price"])
                exit_p = float(t["eval_price"])
                pct = float(t["next_pct"]) / 100.0

                # 模拟买入价（14:50，+滑点）
                buy_price = entry * (1 + self.slippage)
                # 模拟卖出价（次日开盘，-滑点）
                sell_price = exit_p * (1 - self.slippage)

                shares = int(per_stock_capital / buy_price / 100) * 100  # 整手
                if shares < 100:
                    continue

                buy_cost = shares * buy_price * (1 + self.commission)
                sell_rev = shares * sell_price * (1 - self.commission)
                pnl = sell_rev - buy_cost
                pnl_pct = pnl / buy_cost

                self.trade_log.append({
                    "date": date_str,
                    "symbol": str(t["symbol"]),
                    "name": str(t.get("name", "")),
                    "rank": int(t.get("rank", 0)),
                    "score": float(t.get("score", 0)),
                    "entry_price": round(entry, 2),
                    "exit_price": round(exit_p, 2),
                    "buy_price": round(buy_price, 2),
                    "sell_price": round(sell_price, 2),
                    "shares": shares,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct * 100, 3),
                    "next_pct": round(pct * 100, 2),
                    "regime": str(t.get("regime", "sideways")),
                    "is_win": 1 if pnl > 0 else 0,
                })

                cash += pnl

            # 记权益曲线（按日）
            self.equity_curve.append({
                "date": date_str,
                "equity": round(cash, 2),
                "n_trades": sum(1 for tl in self.trade_log if tl["date"] == date_str),
                "daily_pnl": sum(tl["pnl"] for tl in self.trade_log if tl["date"] == date_str),
            })

            # 更新最大回撤
            if cash > equity_peak:
                equity_peak = cash
            dd = (equity_peak - cash) / equity_peak
            if dd > max_dd:
                max_dd = dd
                max_dd_start = date_str

        # 计算指标
        self._compute_metrics()
        self._compute_regime_metrics()

        return self.report()

    def _compute_metrics(self):
        log = pd.DataFrame(self.trade_log)
        eq = pd.DataFrame(self.equity_curve)

        if log.empty:
            self.metrics = {"error": "无交易记录"}
            return

        n_days = len(eq) if not eq.empty else 1
        n_trades = len(log)
        n_wins = log["is_win"].sum()
        win_rate = n_wins / n_trades if n_trades else 0

        total_pnl = log["pnl"].sum()
        total_buy_cost = (log["buy_price"] * log["shares"]).sum()
        total_return = total_pnl / self.capital

        # 年化收益率
        if n_days >= 5 and not eq.empty:
            first_date = pd.to_datetime(eq.iloc[0]["date"])
            last_date = pd.to_datetime(eq.iloc[-1]["date"])
            years = max((last_date - first_date).days / 365, 0.02)
            end_equity = eq.iloc[-1]["equity"]
            annual_return = (end_equity / self.capital) ** (1 / years) - 1
        else:
            years = 1
            annual_return = total_return

        # 最大回撤
        if not eq.empty:
            eq_vals = eq["equity"].values
            peak = np.maximum.accumulate(eq_vals)
            dd_series = (peak - eq_vals) / peak
            max_dd = float(dd_series.max())
        else:
            max_dd = 0

        # 日收益序列（填充无交易日）
        if not eq.empty:
            eq["date_dt"] = pd.to_datetime(eq["date"])
            eq = eq.set_index("date_dt")
            all_dates = pd.date_range(eq.index.min(), eq.index.max(), freq="B")
            eq = eq.reindex(all_dates, method=None)
            eq["equity"] = eq["equity"].ffill().fillna(self.capital)
            daily_returns = eq["equity"].pct_change().dropna()
        else:
            daily_returns = pd.Series(dtype=float)

        # 盈亏比
        gross_profit = log[log["pnl"] > 0]["pnl"].sum()
        gross_loss = abs(log[log["pnl"] < 0]["pnl"].sum())
        profit_factor = gross_profit / gross_loss if gross_loss else float("inf")

        # 夏普比率
        if len(daily_returns) > 5:
            excess = daily_returns - RISK_FREE_RATE / 252
            sharpe = float(np.sqrt(252) * excess.mean() / excess.std()) if excess.std() > 0 else 0
        else:
            sharpe = 0

        # 卡玛比率
        calmar = annual_return / max_dd if max_dd > 0 else float("inf")

        # 平均每笔收益率
        avg_win = log[log["pnl"] > 0]["pnl_pct"].mean() if n_wins > 0 else 0
        avg_loss = abs(log[log["pnl"] < 0]["pnl_pct"].mean()) if n_trades > n_wins else 0

        # 月度胜率分布
        if not eq.empty:
            eq_monthly = eq.copy()
            eq_monthly["month"] = eq_monthly.index.to_period("M")
            monthly_returns = eq_monthly.groupby("month")["equity"].apply(
                lambda x: x.iloc[-1] / x.iloc[0] - 1 if len(x) > 1 else 0
            )
            monthly_win_rate = (monthly_returns > 0).sum() / len(monthly_returns) if len(monthly_returns) else 0
            best_month = float(monthly_returns.max()) if len(monthly_returns) else 0
            worst_month = float(monthly_returns.min()) if len(monthly_returns) else 0
        else:
            monthly_win_rate = best_month = worst_month = 0

        self.metrics = {
            "initial_capital": self.capital,
            "final_equity": round(float(eq.iloc[-1]["equity"]) if not eq.empty else self.capital, 2),
            "total_return": round(total_return * 100, 2),
            "annual_return": round(annual_return * 100, 2),
            "total_pnl": round(total_pnl, 2),
            "n_trades": n_trades,
            "n_wins": int(n_wins),
            "n_losses": n_trades - int(n_wins),
            "win_rate": round(win_rate * 100, 2),
            "profit_factor": round(profit_factor, 2),
            "avg_win_pct": round(avg_win, 3),
            "avg_loss_pct": round(avg_loss, 3),
            "max_drawdown": round(max_dd * 100, 2),
            "sharpe_ratio": round(sharpe, 2),
            "calmar_ratio": round(calmar, 2),
            "n_trading_days": n_days,
            "years": round(years, 2),
            "monthly_win_rate": round(monthly_win_rate * 100, 2),
            "best_month_pct": round(best_month * 100, 2),
            "worst_month_pct": round(worst_month * 100, 2),
        }

    def _compute_regime_metrics(self):
        """按市场环境分层统计。"""
        log = pd.DataFrame(self.trade_log)
        if log.empty:
            return

        self.regime_metrics = {}
        for regime in ["bull", "sideways", "bear"]:
            r_log = log[log["regime"] == regime]
            n = len(r_log)
            if n == 0:
                self.regime_metrics[regime] = {"n_trades": 0, "note": "无交易"}
                continue
            wins = r_log["is_win"].sum()
            wr = wins / n * 100
            avg_pnl = r_log["pnl_pct"].mean()
            total_pnl = r_log["pnl"].sum()
            profit = r_log[r_log["pnl"] > 0]["pnl"].sum()
            loss = abs(r_log[r_log["pnl"] < 0]["pnl"].sum())
            pf = profit / loss if loss else float("inf")

            self.regime_metrics[regime] = {
                "n_trades": n,
                "win_rate": round(wr, 1),
                "avg_pnl_pct": round(avg_pnl, 3),
                "total_pnl": round(total_pnl, 2),
                "profit_factor": round(pf, 2),
            }

    def report(self) -> dict:
        return {
            "summary": self.metrics,
            "regime_breakdown": self.regime_metrics,
            "equity_curve": self.equity_curve[-30:] if len(self.equity_curve) > 30 else self.equity_curve,
            "trade_log": self.trade_log[-30:] if len(self.trade_log) > 30 else self.trade_log,
            "n_total_trades": len(self.trade_log),
            "n_equity_days": len(self.equity_curve),
        }


# ============================================================
# HTML 可视化报告
# ============================================================

def generate_html_report(result: dict, output_path: str = "reports/tail_backtest.html") -> str:
    """生成完整的HTML可视化回测报告。"""
    m = result.get("summary", {})
    rb = result.get("regime_breakdown", {})
    eq = result.get("equity_curve", [])

    if not m or m.get("error"):
        return f"<html><body><h1>回测失败</h1><p>{m.get('error', '无数据')}</p></body></html>"

    # 胜率颜色
    wr_color = "#3fb950" if m.get("win_rate", 0) >= 55 else "#f0b429" if m.get("win_rate", 0) >= 45 else "#f85149"
    sharpe_color = "#3fb950" if m.get("sharpe_ratio", 0) >= 1 else "#f0b429" if m.get("sharpe_ratio", 0) >= 0.5 else "#f85149"

    # 资金曲线JSON
    eq_json = json.dumps([{"date": e["date"], "equity": e["equity"]} for e in eq[-100:]])

    # 资金曲线SVG
    eq_svg = ""
    if len(eq) >= 2:
        max_eq = max(e["equity"] for e in eq)
        min_eq = min(e["equity"] for e in eq)
        h_range = max_eq - min_eq or 1
        W, H = 800, 280
        pad = 40
        pts = []
        for i, e in enumerate(eq):
            x = pad + (W - 2 * pad) * i / (len(eq) - 1) if len(eq) > 1 else W / 2
            y = H - pad - (e["equity"] - min_eq) / h_range * (H - 2 * pad)
            pts.append(f"{x:.1f},{y:.1f}")
        line_color = "#3fb950" if eq[-1]["equity"] >= eq[0]["equity"] else "#f85149"
        eq_svg = f'<polyline points="{" ".join(pts)}" fill="none" stroke="{line_color}" stroke-width="2.5"/>'

    # 日收益柱状图
    daily_svg = ""
    if len(eq) >= 3:
        daily_pnls = []
        for i in range(1, len(eq)):
            d = eq[i]["equity"] - eq[i-1]["equity"]
            daily_pnls.append(d)
        if daily_pnls:
            max_abs = max(abs(d) for d in daily_pnls) or 1
            bars = []
            for i, d in enumerate(daily_pnls[-60:]):
                x = pad + (W - 2 * pad) * i / (len(daily_pnls[-60:]) - 1) if len(daily_pnls[-60:]) > 1 else W / 2
                h = abs(d) / max_abs * 100
                y = 160 if d >= 0 else 160
                color = "#3fb950" if d >= 0 else "#f85149"
                bars.append(f'<rect x="{x:.1f}" y="{y - (h if d >= 0 else 0):.1f}" '
                           f'width="3" height="{h:.1f}" fill="{color}" rx="1"/>')
            daily_svg = "\n".join(bars)

    # 市场分层雷达数据
    bull_wr = rb.get("bull", {}).get("win_rate", 0) or 0
    sway_wr = rb.get("sideways", {}).get("win_rate", 0) or 0
    bear_wr = rb.get("bear", {}).get("win_rate", 0) or 0

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>尾盘荐股回测报告</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;background:#0d1117;color:#c9d1d9;line-height:1.7;padding:20px;max-width:900px;margin:0 auto}}
h1{{text-align:center;color:#58a6ff;margin:20px 0;font-size:24px}}
h2{{color:#79c0ff;border-left:4px solid #58a6ff;padding-left:12px;margin:28px 0 14px;font-size:18px}}
.subtitle{{text-align:center;color:#8b949e;font-size:13px;margin-bottom:24px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px 20px;margin:12px 0}}
.metrics-row{{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}}
.metric-box{{flex:1;min-width:120px;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;text-align:center}}
.metric-box .v{{font-size:22px;font-weight:700}}
.metric-box .l{{font-size:12px;color:#8b949e;margin-top:2px}}
.up{{color:#3fb950}}.down{{color:#f85149}}.gold{{color:#f0b429}}.dim{{color:#8b949e}}
table{{width:100%;border-collapse:collapse;margin:10px 0;font-size:13px}}
th{{background:#21262d;color:#8b949e;padding:8px 12px;text-align:left;border-bottom:1px solid #30363d}}
td{{padding:8px 12px;border-bottom:1px solid #21262d}}
.footer{{text-align:center;color:#484f58;font-size:11px;margin-top:36px;padding-top:16px;border-top:1px solid #21262d}}
.chart{{background:#0d1117;border-radius:8px;padding:10px;margin:12px 0}}
</style>
</head>
<body>
<h1>📊 尾盘荐股回测报告</h1>
<p class="subtitle">策略: 14:50尾盘买入 → T+1开盘卖出 | 佣金万2.5 + 滑点0.1%</p>

<h2>📈 核心指标</h2>
<div class="metrics-row">
  <div class="metric-box"><div class="v { 'up' if m.get('total_return',0)>=0 else 'down' }">{m.get('total_return',0):+.1f}%</div><div class="l">总收益率</div></div>
  <div class="metric-box"><div class="v { 'up' if m.get('annual_return',0)>=0 else 'down' }">{m.get('annual_return',0):+.1f}%</div><div class="l">年化收益率</div></div>
  <div class="metric-box"><div class="v down">{m.get('max_drawdown',0):.1f}%</div><div class="l">最大回撤</div></div>
  <div class="metric-box"><div class="v" style="color:{wr_color}">{m.get('win_rate',0):.1f}%</div><div class="l">胜率</div></div>
  <div class="metric-box"><div class="v" style="color:{sharpe_color}">{m.get('sharpe_ratio',0):.2f}</div><div class="l">夏普比率</div></div>
  <div class="metric-box"><div class="v gold">{m.get('profit_factor',0):.2f}</div><div class="l">盈亏比</div></div>
</div>

<div class="metrics-row">
  <div class="metric-box"><div class="v">{m.get('n_trades',0)}</div><div class="l">总交易数</div></div>
  <div class="metric-box"><div class="v up">{m.get('n_wins',0)}</div><div class="l">盈利次数</div></div>
  <div class="metric-box"><div class="v down">{m.get('n_losses',0)}</div><div class="l">亏损次数</div></div>
  <div class="metric-box"><div class="v">{m.get('avg_win_pct',0):.2f}%</div><div class="l">平均盈利</div></div>
  <div class="metric-box"><div class="v">{m.get('avg_loss_pct',0):.2f}%</div><div class="l">平均亏损</div></div>
  <div class="metric-box"><div class="v">{m.get('calmar_ratio',0):.2f}</div><div class="l">卡玛比率</div></div>
</div>

<div class="card">
  <strong class="gold">📅 回测概况</strong><br>
  初始资金: ¥{m.get('initial_capital',0):,.0f} &nbsp;|&nbsp;
  最终权益: ¥{m.get('final_equity',0):,.0f} &nbsp;|&nbsp;
  交易天数: {m.get('n_trading_days',0)} &nbsp;|&nbsp;
  回测年限: {m.get('years',0):.1f}年<br>
  月度胜率: {m.get('monthly_win_rate',0):.1f}% &nbsp;|&nbsp;
  最佳月: {m.get('best_month_pct',0):+.1f}% &nbsp;|&nbsp;
  最差月: {m.get('worst_month_pct',0):+.1f}%
</div>

<h2>💰 资金曲线</h2>
<div class="chart">
  <svg viewBox="0 0 800 280" width="100%">
    <rect width="800" height="280" fill="#0d1117"/>
    <line x1="40" y1="240" x2="760" y2="240" stroke="#21262d" stroke-width="1"/>
    {eq_svg}
  </svg>
</div>

<h2>🔄 市场环境分层评估</h2>
<table>
  <tr><th>环境</th><th>交易数</th><th>胜率</th><th>平均收益</th><th>累计盈亏</th><th>盈亏比</th></tr>
  <tr>
    <td class="up">🐂 牛市</td>
    <td>{rb.get('bull',{}).get('n_trades',0)}</td>
    <td>{bull_wr:.0f}%</td>
    <td class="{ 'up' if rb.get('bull',{}).get('avg_pnl_pct',0)>=0 else 'down' }">{rb.get('bull',{}).get('avg_pnl_pct',0):+.2f}%</td>
    <td class="{ 'up' if rb.get('bull',{}).get('total_pnl',0)>=0 else 'down' }">¥{rb.get('bull',{}).get('total_pnl',0):,.0f}</td>
    <td>{rb.get('bull',{}).get('profit_factor',0):.2f}</td>
  </tr>
  <tr>
    <td class="gold">↔️ 震荡</td>
    <td>{rb.get('sideways',{}).get('n_trades',0)}</td>
    <td>{sway_wr:.0f}%</td>
    <td class="{ 'up' if rb.get('sideways',{}).get('avg_pnl_pct',0)>=0 else 'down' }">{rb.get('sideways',{}).get('avg_pnl_pct',0):+.2f}%</td>
    <td class="{ 'up' if rb.get('sideways',{}).get('total_pnl',0)>=0 else 'down' }">¥{rb.get('sideways',{}).get('total_pnl',0):,.0f}</td>
    <td>{rb.get('sideways',{}).get('profit_factor',0):.2f}</td>
  </tr>
  <tr>
    <td class="down">🐻 熊市</td>
    <td>{rb.get('bear',{}).get('n_trades',0)}</td>
    <td>{bear_wr:.0f}%</td>
    <td class="{ 'up' if rb.get('bear',{}).get('avg_pnl_pct',0)>=0 else 'down' }">{rb.get('bear',{}).get('avg_pnl_pct',0):+.2f}%</td>
    <td class="{ 'up' if rb.get('bear',{}).get('total_pnl',0)>=0 else 'down' }">¥{rb.get('bear',{}).get('total_pnl',0):,.0f}</td>
    <td>{rb.get('bear',{}).get('profit_factor',0):.2f}</td>
  </tr>
</table>

<div class="card">
  <p><strong class="gold">稳定性评估：</strong>
  震荡市胜率 {sway_wr:.0f}%，{'✅ 稳定' if sway_wr >= 50 else '⚠️ 偏弱'}
  &nbsp;|&nbsp;
  熊市胜率 {bear_wr:.0f}%，{'✅ 抗跌' if bear_wr >= 45 else '⚠️ 随大盘'}
  </p>
</div>

<div class="footer">
  回测引擎: TailBacktest v1.0 | 佣金万2.5 + 滑点0.1%
  | 生成时间: {dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
</div>
</body></html>"""

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path


# ============================================================
# CLI 入口 + 完整流程
# ============================================================

def run_full_backtest(db_path: str = "data_store/recommend.db",
                      mode: str = "tail",
                      output_path: str = "reports/tail_backtest.html") -> dict:
    """一键运行完整回测 + 生成HTML报告。

    Returns: 回测结果字典
    """
    print("=== 尾盘荐股深度回测 ===\n")

    # 1. 加载数据
    print("[1/5] 加载历史交易记录...")
    trades = load_tail_trades(db_path, mode=mode)
    if trades.empty:
        print("  ❌ 无历史交易数据，请先生成尾盘荐股记录")
        return {"error": "无交易数据"}

    print(f"  加载 {len(trades)} 条交易记录")

    # 2. 市场环境标注
    print("[2/5] 标注市场环境...")
    trades = classify_market_regimes(trades)
    for r in ["bull", "sideways", "bear"]:
        n = len(trades[trades["regime"] == r])
        print(f"  {r}: {n}条")

    # 3. 执行回测
    print("[3/5] 执行回测引擎...")
    bt = TailBacktest()
    result = bt.run(trades)

    if "error" in result:
        print(f"  ❌ {result['error']}")
        return result

    # 4. 打印核心指标
    m = result["summary"]
    print(f"\n[4/5] 核心指标:")
    print(f"  总收益: {m['total_return']:+.1f}%  年化: {m['annual_return']:+.1f}%")
    print(f"  胜率: {m['win_rate']:.1f}%  盈亏比: {m['profit_factor']:.2f}")
    print(f"  最大回撤: {m['max_drawdown']:.1f}%  夏普: {m['sharpe_ratio']:.2f}")
    print(f"  总交易: {m['n_trades']}笔 (盈{m['n_wins']}/亏{m['n_losses']})")

    rb = result.get("regime_breakdown", {})
    for r in ["bull", "sideways", "bear"]:
        info = rb.get(r, {})
        if info.get("n_trades", 0) > 0:
            print(f"  {r}: {info['n_trades']}笔 胜率{info['win_rate']:.0f}% 均收益{info['avg_pnl_pct']:+.2f}%")

    # 5. 生成报告
    print(f"\n[5/5] 生成HTML报告...")
    path = generate_html_report(result, output_path)
    print(f"  ✅ 报告已生成: {path}")

    return result


if __name__ == "__main__":
    run_full_backtest()
