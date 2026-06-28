#!/usr/bin/env python3
"""尾盘荐股策略回测 — 独立脚本。

=== 买入规则 ===
- 信号日收盘价买入（模拟14:45尾盘介入）
- 佣金万2.5 + 滑点0.1%（买入上浮）

=== 三种卖出场景 ===
Scenario A: 次日开盘卖出 (T+1 open)
Scenario B: 次日最高价卖出 (T+1 high，理想情况)
Scenario C: 持有N日后收盘卖出 (默认N=3)

=== 输入格式 ===
CSV文件两列:
  date,symbol
  2026-01-15,600519
  2026-01-15,000858

=== 使用方式 ===
  python backtest_tail_signals.py signals.csv kline_data.csv

也可指定持仓天数:
  python backtest_tail_signals.py signals.csv -n 5
"""

import argparse
import os
import sys
from collections import defaultdict

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    _HAS_MATPLOTLIB = True
except ImportError:
    _HAS_MATPLOTLIB = False

import numpy as np
import pandas as pd

# ============================================================
# 配置
# ============================================================
COMMISSION = 0.00025      # 万2.5
BUY_SLIPPAGE = 0.001      # 买入滑点0.1%
SELL_SLIPPAGE = 0.001     # 卖出滑点0.1%
INITIAL_CAPITAL = 100000  # 初始资金10万
MAX_POSITION = 0.20       # 单票最大仓位20%
RISK_FREE = 0.03          # 无风险利率3%

if _HAS_MATPLOTLIB:
    plt.rcParams.update({
        "figure.facecolor": "#0d1117",
        "axes.facecolor": "#0d1117",
        "axes.edgecolor": "#30363d",
        "axes.labelcolor": "#c9d1d9",
        "text.color": "#c9d1d9",
        "xtick.color": "#8b949e",
        "ytick.color": "#8b949e",
        "grid.color": "#21262d",
        "legend.facecolor": "#161b22",
        "legend.edgecolor": "#30363d",
    })


# ============================================================
# 数据加载
# ============================================================

def load_kline_csv(path: str) -> pd.DataFrame:
    """加载K线CSV。期望列: date, symbol, open, high, low, close, volume。

    也兼容中文列名：日期, 代码, 开盘, 最高, 最低, 收盘, 成交量。
    """
    df = pd.read_csv(path, dtype={"symbol": str})
    # 中文列名映射
    rename = {
        "日期": "date", "代码": "symbol", "开盘": "open",
        "最高": "high", "最低": "low", "收盘": "close", "成交量": "volume",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    for col in ["date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col])
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 去重
    if "date" in df.columns and "symbol" in df.columns:
        df = df.drop_duplicates(subset=["date", "symbol"])

    return df.sort_values(["symbol", "date"]).reset_index(drop=True)


def load_kline_db(db_path: str, table: str = "kline") -> pd.DataFrame:
    """从SQLite加载K线数据。"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query(f"SELECT date, symbol, open, high, low, close, volume FROM {table}", conn)
    conn.close()
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        for c in ["open", "high", "low", "close", "volume"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(["symbol", "date"]).reset_index(drop=True)


def load_signals_csv(path: str) -> list[tuple[pd.Timestamp, str]]:
    """加载荐股信号CSV。列: date, symbol。"""
    df = pd.read_csv(path, dtype={"symbol": str})
    if "日期" in df.columns:
        df = df.rename(columns={"日期": "date", "代码": "symbol"})
    df["date"] = pd.to_datetime(df["date"])
    return [(row["date"], row["symbol"].strip().zfill(6)) for _, row in df.iterrows()]


# ============================================================
# K线数据查询
# ============================================================

def find_kline(kline: pd.DataFrame, symbol: str, target_date: pd.Timestamp,
               days_after: int = 10) -> dict:
    """在K线DataFrame中查找某只股票指定日期及之后的K线。

    Returns:
        {signal_close, next_open, next_high, next_low, next_close,
         hold_close(N天后), prices_after[N天收盘价列表], valid: bool}
    """
    stock = kline[kline["symbol"] == symbol].copy()
    if stock.empty:
        return {"valid": False}

    stock = stock.sort_values("date").reset_index(drop=True)
    # 找信号日
    idx = stock[stock["date"] == target_date].index
    if len(idx) == 0:
        # 找不到精确日期，取最近交易日
        stock["_diff"] = (stock["date"] - target_date).abs()
        idx = [stock["_diff"].idxmin()]
        if stock.iloc[idx[0]]["_diff"].days > 3:
            return {"valid": False}

    i = idx[0]
    row = stock.iloc[i]

    result = {
        "signal_close": float(row["close"]),
        "signal_date": row["date"],
        "valid": True,
    }

    # 次日数据
    if i + 1 < len(stock):
        nxt = stock.iloc[i + 1]
        result["next_open"] = float(nxt["open"])
        result["next_high"] = float(nxt["high"])
        result["next_low"] = float(nxt["low"])
        result["next_close"] = float(nxt["close"])
    else:
        result["valid"] = False
        return result

    # 持有N日数据
    result["prices_after"] = []
    for j in range(1, min(days_after + 1, len(stock) - i)):
        result["prices_after"].append(float(stock.iloc[i + j]["close"]))

    return result


# ============================================================
# 回测引擎
# ============================================================

class TailSignalBacktest:
    """尾盘信号回测引擎。"""

    def __init__(self, signals: list, kline: pd.DataFrame,
                 hold_days: int = 3, capital: float = INITIAL_CAPITAL):
        self.signals = signals      # [(date, symbol), ...]
        self.kline = kline
        self.hold_days = hold_days
        self.capital = capital

        # 三种场景记录
        self.trades_a: list[dict] = []  # 次日开盘卖
        self.trades_b: list[dict] = []  # 次日最高卖
        self.trades_c: list[dict] = []  # 持有N日卖

        # 权益曲线
        self.equity_a: list[dict] = []
        self.equity_b: list[dict] = []
        self.equity_c: list[dict] = []

    def run(self) -> dict:
        cash_a = cash_b = cash_c = self.capital
        peak_a = peak_b = peak_c = self.capital
        max_dd_a = max_dd_b = max_dd_c = 0.0

        by_date = defaultdict(list)
        for d, s in self.signals:
            by_date[d.strftime("%Y-%m-%d")].append(s)

        for date_str in sorted(by_date.keys()):
            symbols = by_date[date_str]
            n = len(symbols)
            if n == 0:
                continue

            pos_pct = min(1.0 / n, MAX_POSITION)
            per_stock = cash_a * pos_pct

            daily_pnl_a = daily_pnl_b = daily_pnl_c = 0.0

            for sym in symbols:
                dt_obj = pd.Timestamp(date_str)
                k = find_kline(self.kline, sym, dt_obj, self.hold_days)
                if not k["valid"]:
                    continue

                buy = k["signal_close"] * (1 + BUY_SLIPPAGE)
                shares = int(per_stock / buy / 100) * 100
                if shares < 100:
                    continue

                buy_cost = shares * buy * (1 + COMMISSION)

                # Scenario A: 次日开盘
                sell_a = k["next_open"] * (1 - SELL_SLIPPAGE)
                rev_a = shares * sell_a * (1 - COMMISSION)
                pnl_a = rev_a - buy_cost
                daily_pnl_a += pnl_a
                self.trades_a.append({
                    "date": date_str, "symbol": sym,
                    "buy": round(buy, 2), "sell": round(sell_a, 2),
                    "pnl": round(pnl_a, 2), "pnl_pct": round(pnl_a / buy_cost * 100, 3),
                    "shares": shares,
                })

                # Scenario B: 次日最高
                sell_b = k["next_high"] * (1 - SELL_SLIPPAGE)
                rev_b = shares * sell_b * (1 - COMMISSION)
                pnl_b = rev_b - buy_cost
                daily_pnl_b += pnl_b
                self.trades_b.append({
                    "date": date_str, "symbol": sym,
                    "buy": round(buy, 2), "sell": round(sell_b, 2),
                    "pnl": round(pnl_b, 2), "pnl_pct": round(pnl_b / buy_cost * 100, 3),
                    "shares": shares,
                })

                # Scenario C: 持有N日
                if len(k["prices_after"]) >= self.hold_days:
                    sell_c = k["prices_after"][self.hold_days - 1] * (1 - SELL_SLIPPAGE)
                    rev_c = shares * sell_c * (1 - COMMISSION)
                    pnl_c = rev_c - buy_cost
                else:
                    pnl_c = 0
                daily_pnl_c += pnl_c
                self.trades_c.append({
                    "date": date_str, "symbol": sym,
                    "buy": round(buy, 2), "sell": round(sell_c, 2) if 'sell_c' in dir() else None,
                    "pnl": round(pnl_c, 2), "pnl_pct": round(pnl_c / buy_cost * 100, 3) if buy_cost else 0,
                    "shares": shares,
                })

            cash_a += daily_pnl_a
            cash_b += daily_pnl_b
            cash_c += daily_pnl_c

            # 更新权益+回撤
            for cash, peak, eq_list, label in [
                (cash_a, peak_a, self.equity_a, "a"),
                (cash_b, peak_b, self.equity_b, "b"),
                (cash_c, peak_c, self.equity_c, "c"),
            ]:
                eq_list.append({"date": date_str, "equity": round(cash, 2)})
                if cash > peak:
                    peak = cash
                dd = (peak - cash) / peak if peak > 0 else 0
                if label == "a":
                    peak_a, max_dd_a = peak, max(max_dd_a, dd)
                elif label == "b":
                    peak_b, max_dd_b = peak, max(max_dd_b, dd)
                else:
                    peak_c, max_dd_c = peak, max(max_dd_c, dd)

        # 计算指标
        return {
            "scenario_a": self._metrics(self.trades_a, cash_a, max_dd_a, "次日开盘卖出"),
            "scenario_b": self._metrics(self.trades_b, cash_b, max_dd_b, "次日最高卖出"),
            "scenario_c": self._metrics(self.trades_c, cash_c, max_dd_c,
                                        f"持有{self.hold_days}日卖出"),
            "equity_a": self.equity_a,
            "equity_b": self.equity_b,
            "equity_c": self.equity_c,
            "trades_a": self.trades_a,
            "trades_b": self.trades_b,
            "trades_c": self.trades_c,
        }

    def _metrics(self, trades: list, final_cash: float, max_dd: float, label: str) -> dict:
        n = len(trades)
        if n == 0:
            return {"label": label, "n_trades": 0, "error": "无交易"}

        wins = [t for t in trades if t["pnl"] > 0]
        n_wins = len(wins)
        win_rate = n_wins / n * 100
        total_pnl = sum(t["pnl"] for t in trades)
        total_return = (final_cash - self.capital) / self.capital * 100

        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        profit_factor = gross_profit / gross_loss if gross_loss else float("inf")

        avg_win = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
        losses = [t for t in trades if t["pnl"] < 0]
        avg_loss = abs(np.mean([t["pnl_pct"] for t in losses])) if losses else 0

        max_consec_wins = max_consec_losses = 0
        streak = 0
        for t in sorted(trades, key=lambda x: x["date"]):
            if t["pnl"] > 0:
                streak = streak + 1 if streak >= 0 else 1
            else:
                streak = streak - 1 if streak <= 0 else -1
            max_consec_wins = max(max_consec_wins, streak)
            max_consec_losses = min(max_consec_losses, streak)
        max_consec_losses = abs(max_consec_losses)

        # 年化
        if len(trades) >= 5:
            dates = sorted(set(t["date"] for t in trades))
            years = max((pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days / 365, 0.05)
            ann_ret = ((final_cash / self.capital) ** (1 / years) - 1) * 100
        else:
            years, ann_ret = 1, total_return

        # 日收益
        eq_df = pd.DataFrame([
            {"date": e["date"], "equity": e["equity"]}
            for e in (
                self.equity_a if label.startswith("次日开盘") else
                self.equity_b if label.startswith("次日最高") else
                self.equity_c
            )
        ])
        if not eq_df.empty and len(eq_df) >= 5:
            eq_df["date"] = pd.to_datetime(eq_df["date"])
            eq_df = eq_df.set_index("date").sort_index()
            all_dates = pd.date_range(eq_df.index.min(), eq_df.index.max(), freq="B")
            eq_df = eq_df.reindex(all_dates).ffill().fillna(self.capital)
            returns = eq_df["equity"].pct_change().dropna()
            if returns.std() > 0:
                sharpe = float(np.sqrt(252) * (returns.mean() - RISK_FREE / 252) / returns.std())
            else:
                sharpe = 0
        else:
            sharpe = 0

        calmar = ann_ret / max_dd * 100 if max_dd > 0 else float("inf")

        return {
            "label": label,
            "n_trades": n,
            "n_wins": n_wins,
            "n_losses": n - n_wins,
            "win_rate": round(win_rate, 1),
            "total_return": round(total_return, 2),
            "annual_return": round(ann_ret, 2),
            "total_pnl": round(total_pnl, 2),
            "profit_factor": round(profit_factor, 2),
            "avg_win_pct": round(avg_win, 3),
            "avg_loss_pct": round(avg_loss, 3),
            "max_drawdown": round(max_dd * 100, 2),
            "sharpe": round(sharpe, 2),
            "calmar": round(calmar, 2),
            "max_consec_wins": max_consec_wins,
            "max_consec_losses": max_consec_losses,
            "years": round(years, 2),
        }


# ============================================================
# 可视化
# ============================================================

def plot_report(result: dict, output: str = "reports/tail_signal_backtest.png"):
    """生成三场景对比图表。matplotlib未安装时静默跳过。"""
    if not _HAS_MATPLOTLIB:
        print("  ⚠️ matplotlib未安装，跳过图表生成 (pip install matplotlib)")
        return ""
    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    fig.suptitle("尾盘荐股策略回测 — 三场景对比", fontsize=16,
                 color="#58a6ff", fontweight="bold")

    # Panel 1: 权益曲线
    ax1 = axes[0, 0]
    for eq_list, label, color in [
        (result["equity_a"], "次日开盘卖", "#58a6ff"),
        (result["equity_b"], "次日最高卖", "#3fb950"),
        (result["equity_c"], f"持有{result.get('hold_days',3)}日卖", "#f0b429"),
    ]:
        if not eq_list:
            continue
        dates = [pd.Timestamp(e["date"]) for e in eq_list]
        vals = [e["equity"] for e in eq_list]
        ax1.plot(dates, vals, color=color, linewidth=1.5, label=label, alpha=0.85)
    ax1.axhline(y=INITIAL_CAPITAL, color="#30363d", linestyle="--", linewidth=0.8)
    ax1.set_title("资金权益曲线", fontsize=13, color="#79c0ff")
    ax1.set_ylabel("权益 (¥)")
    ax1.legend(loc="upper left", fontsize=9)
    ax1.grid(True, alpha=0.3)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))

    # Panel 2: 核心指标对比（柱状图）
    ax2 = axes[0, 1]
    scenarios = [result["scenario_a"], result["scenario_b"], result["scenario_c"]]
    labels = [s["label"] for s in scenarios]
    x = np.arange(len(labels))
    width = 0.2

    metrics_plot = [
        ("胜率%", "win_rate", "#58a6ff"),
        ("年化%", "annual_return", "#3fb950"),
        ("盈亏比", "profit_factor", "#f0b429"),
    ]
    for j, (name, key, color) in enumerate(metrics_plot):
        vals = [s.get(key, 0) for s in scenarios]
        bars = ax2.bar(x + j * width, vals, width, label=name, color=color, alpha=0.85)
        for bar, val in zip(bars, vals):
            if val != 0:
                ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                         f"{val:.1f}", ha="center", va="bottom", fontsize=7, color="#c9d1d9")

    ax2.set_xticks(x + width)
    ax2.set_xticklabels([l[:4] for l in labels], fontsize=9)
    ax2.set_title("核心指标对比", fontsize=13, color="#79c0ff")
    ax2.legend(fontsize=8)
    ax2.grid(True, alpha=0.3, axis="y")

    # Panel 3: 胜率随时间变化
    ax3 = axes[1, 0]
    for eq_list, label, color in [
        (result.get("trades_a", []), "次日开盘", "#58a6ff"),
        (result.get("trades_b", []), "次日最高", "#3fb950"),
    ]:
        if not eq_list:
            continue
        df = pd.DataFrame(eq_list).sort_values("date")
        df["win"] = (df["pnl"] > 0).astype(int)
        df["rolling_wr"] = df["win"].rolling(10, min_periods=3).mean() * 100
        dates = [pd.Timestamp(d) for d in df["date"]]
        ax3.plot(dates, df["rolling_wr"], color=color, linewidth=1.5, label=label)
    ax3.axhline(y=50, color="#30363d", linestyle="--", linewidth=0.8, alpha=0.5)
    ax3.set_title("滚动胜率 (窗口10笔)", fontsize=13, color="#79c0ff")
    ax3.set_ylabel("胜率 %")
    ax3.legend(fontsize=9)
    ax3.grid(True, alpha=0.3)
    ax3.set_ylim(0, 100)

    # Panel 4: 回撤曲线
    ax4 = axes[1, 1]
    for eq_list, label, color in [
        (result["equity_a"], "次日开盘卖", "#58a6ff"),
        (result["equity_b"], "次日最高卖", "#3fb950"),
        (result["equity_c"], f"持有{result.get('hold_days',3)}日卖", "#f0b429"),
    ]:
        if len(eq_list) < 2:
            continue
        vals = np.array([e["equity"] for e in eq_list])
        peak = np.maximum.accumulate(vals)
        dd = (peak - vals) / peak * 100
        dates = [pd.Timestamp(e["date"]) for e in eq_list]
        ax4.fill_between(dates, 0, dd, color=color, alpha=0.15)
        ax4.plot(dates, dd, color=color, linewidth=1, label=label)

    ax4.set_title("回撤曲线", fontsize=13, color="#79c0ff")
    ax4.set_ylabel("回撤 %")
    ax4.legend(fontsize=9)
    ax4.grid(True, alpha=0.3)
    ax4.invert_yaxis()

    plt.tight_layout()
    os.makedirs(os.path.dirname(output) if os.path.dirname(output) else ".", exist_ok=True)
    plt.savefig(output, dpi=150, bbox_inches="tight", facecolor="#0d1117")
    plt.close()
    return output


def print_table(results: dict, hold_days: int):
    """打印结构化统计表。"""
    print(f"\n{'='*70}")
    print(f"  尾盘荐股策略回测报告 (持{hold_days}日)")
    print(f"{'='*70}")

    for key in ["scenario_a", "scenario_b", "scenario_c"]:
        s = results[key]
        if s.get("error"):
            print(f"\n  {s['label']}: {s['error']}")
            continue

        print(f"""
  ┌─ {s['label']} ─────────────────────────────────┐
  │ 交易次数: {s['n_trades']:>6}  盈利: {s['n_wins']:>5}  亏损: {s['n_losses']:>5}
  │ 胜率:     {s['win_rate']:>6.1f}%   年化收益: {s['annual_return']:>+7.1f}%
  │ 总收益:   {s['total_return']:>+7.2f}%   最大回撤: {s['max_drawdown']:>7.1f}%
  │ 盈亏比:   {s['profit_factor']:>8.2f}   夏普:     {s['sharpe']:>8.2f}
  │ 平均盈利: {s['avg_win_pct']:>+7.3f}%    平均亏损: {s['avg_loss_pct']:>-7.3f}%
  │ 最大连胜: {s['max_consec_wins']:>5}       最大连亏: {s['max_consec_losses']:>5}
  │ 卡玛比率: {s['calmar']:>8.1f}
  └──────────────────────────────────────────────┘""")

    print(f"\n  佣金: 万2.5 | 滑点: 0.1% | 初始资金: ¥{INITIAL_CAPITAL:,.0f}")
    print(f"{'='*70}\n")


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="尾盘荐股策略回测")
    parser.add_argument("signals", help="荐股信号CSV (date,symbol)")
    parser.add_argument("kline", nargs="?", default="", help="K线CSV (date,symbol,open,high,low,close)")
    parser.add_argument("-n", "--hold-days", type=int, default=3, help="持有天数 (默认3)")
    parser.add_argument("-c", "--capital", type=float, default=INITIAL_CAPITAL, help="初始资金")
    parser.add_argument("-o", "--output", default="reports/tail_signal_backtest.png", help="图表输出路径")
    parser.add_argument("--db", action="store_true", help="K线来源为SQLite数据库")
    parser.add_argument("--db-table", default="kline", help="数据库表名")
    args = parser.parse_args()

    # 加载信号
    print(f"[1/5] 加载荐股信号: {args.signals}")
    signals = load_signals_csv(args.signals)
    print(f"  信号数: {len(signals)}条")

    if not signals:
        print("  ❌ 无信号数据")
        sys.exit(1)

    # 加载K线
    if args.db:
        print(f"[2/5] 加载数据库K线: {args.kline}")
        kline = load_kline_db(args.kline, args.db_table)
    elif args.kline:
        print(f"[2/5] 加载CSV K线: {args.kline}")
        kline = load_kline_csv(args.kline)
    else:
        print("[2/5] 从项目数据库加载K线...")
        signals_dates = sorted(set(d.strftime("%Y-%m-%d") for d, _ in signals))
        signals_symbols = list(set(s for _, s in signals))
        print(f"  日期范围: {signals_dates[0]}~{signals_dates[-1]}, {len(signals_symbols)}只股票")

        # 通过项目 fetcher 获取K线
        try:
            from data.fetcher import get_fetcher
            fetcher = get_fetcher()
            rows = []
            for sym in signals_symbols[:20]:  # 限制数量
                df = fetcher.get_kline(sym, days=400)
                if df is not None and not df.empty:
                    df["symbol"] = sym.zfill(6)
                    rows.append(df[["date", "symbol", "open", "high", "low", "close", "volume"]])
            if rows:
                kline = pd.concat(rows, ignore_index=True)
                kline["date"] = pd.to_datetime(kline["date"])
            else:
                kline = pd.DataFrame()
        except Exception as e:
            print(f"  ❌ 项目数据源不可用: {e}")
            sys.exit(1)

    print(f"  K线记录: {len(kline)}条, {kline['symbol'].nunique()}只股票")

    if kline.empty:
        print("  ❌ 无K线数据")
        sys.exit(1)

    # 回测
    print(f"[3/5] 执行回测 (持{args.hold_days}日)...")
    bt = TailSignalBacktest(signals, kline, hold_days=args.hold_days, capital=args.capital)
    result = bt.run()

    # 打印
    print(f"[4/5] 统计结果:")
    print_table(result, args.hold_days)

    # 图表
    print(f"[5/5] 生成图表...")
    path = plot_report(result, args.output)
    print(f"  ✅ 图表已保存: {path}")


if __name__ == "__main__":
    main()
