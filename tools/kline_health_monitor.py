"""K线数据健康监控：检测无日线数据的股票并自动分类故障原因。

用法：
  python tools/kline_health_monitor.py                    # 全市场扫描（采样200只）
  python tools/kline_health_monitor.py --full             # 全市场扫描（全部）
  python tools/kline_health_monitor.py --pool sh,sz       # 指定市场板块
  python tools/kline_health_monitor.py --sample 100       # 随机采样N只
  python tools/kline_health_monitor.py --alert-thresh 5   # 设置告警阈值（失败数>N触发）
  python tools/kline_health_monitor.py --json            # JSON 输出（供CI/自动化）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from typing import Optional

# 自动添加项目根目录到 sys.path（支持 CLI 直接运行）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ────────── 故障分类 ──────────
CATEGORY = {
    "OK":           "数据正常",
    "EMPTY":        "接口返回空数据（可能新股/退市/数据源不覆盖）",
    "SUSPENDED":    "停牌导致数据中断",
    "DELISTED":     "退市/代码已失效",
    "RATE_LIMIT":   "接口限频/请求过快",
    "FEW_ROWS":     "数据不足（新上市/停牌恢复初期）",
    "STALE_DATA":   "数据滞后（最新日线日期<今日-3天）",
    "SOURCE_FAIL":  "数据源完全不可用（新浪/东财均失败）",
    "UNKNOWN":      "未知异常",
}


def classify_failure(code: str, df, error: Optional[Exception], today: str) -> tuple[str, str]:
    """根据返回值或异常类型，分类一只票的数据健康状态。

    Returns: (category_key, detail_message)
    """
    if error is not None:
        msg = str(error).lower()
        # 按优先级匹配关键词
        if any(kw in msg for kw in ("停牌", "suspend", "suspended")):
            return ("SUSPENDED", f"停牌: {str(error)[:80]}")
        if any(kw in msg for kw in ("退市", "delist", "delisted")):
            return ("DELISTED", f"退市: {str(error)[:80]}")
        if any(kw in msg for kw in ("限频", "限流", "频繁", "rate", "too many", "throttle")):
            return ("RATE_LIMIT", f"接口限频: {str(error)[:80]}")
        if any(kw in msg for kw in ("dlsym", "mini_racer", "execjs")):
            return ("SOURCE_FAIL", f"JS引擎异常(Sina源不可用): {str(error)[:80]}")
        if any(kw in msg for kw in ("connection", "timeout", "RemoteDisconnected")):
            return ("SOURCE_FAIL", f"数据源连接失败: {str(error)[:80]}")
        if "NoneType" in str(type(error).__name__) or "none" in msg:
            return ("SOURCE_FAIL", f"数据源返回空: {str(error)[:80]}")
        return ("UNKNOWN", f"{type(error).__name__}: {str(error)[:80]}")

    # 无异常，检查 df
    if df is None or df.empty:
        # 可能是新股（无历史K线）、北交所（数据源不覆盖）等
        return ("EMPTY", "K线为空（可能新股/北交所/数据源不覆盖）")

    if "date" not in df.columns or len(df) == 0:
        return ("EMPTY", "K线无数据列或0行")

    dates = df["date"].tolist()
    latest = str(dates[-1]) if dates else "none"
    row_count = len(dates)

    # 新上市（数据<20根）
    if row_count < 20:
        return ("FEW_ROWS", f"数据不足({row_count}根，最新={latest})，可能新上市")

    # 数据滞后（最新日期 < today-3天）
    if latest < today:
        from datetime import datetime as _dt2
        try:
            gap = (_dt2.strptime(today, "%Y-%m-%d") - _dt2.strptime(latest, "%Y-%m-%d")).days
            if gap > 3:
                return ("STALE_DATA", f"数据滞后{gap}天（最新={latest}）")
        except Exception:
            pass

    return ("OK", f"正常({row_count}根，最新={latest})")


def scan_kline_health(
    pool: list[str] | None = None,
    sample_size: int = 200,
    full: bool = False,
    quiet: bool = False,
) -> dict:
    """扫描股票池的K线数据健康状态。

    Args:
        pool: 指定股票代码列表；为None则从全市场快照取。
        sample_size: 随机采样数量（full=False时）。
        full: True=扫描全部股票（慎用，最多5600只）。
        quiet: True=静默扫描（仅返回结果，不打印进度）。

    Returns:
        {
            "scanned": int,
            "ok": int,
            "failed": int,
            "failure_rate": float,
            "by_category": {category: count},
            "by_market": {market_prefix: {category: count}},
            "failures": [{code, category, detail}],
            "top_failure_codes": [...],
            "elapsed_s": float,
            "timestamp": str,
        }
    """
    from data.fetcher import get_fetcher

    today = _dt.date.today().isoformat()
    t0 = time.time()

    # ── 构建股票池 ──
    if pool is None:
        f0 = get_fetcher()
        spot = f0.get_market_spot()
        code_col = next((c for c in spot.columns if c in ("代码", "symbol", "code")), None)
        if not code_col:
            return {"error": "无法获取全市场股票列表"}
        all_codes = spot[code_col].astype(str).str.strip().str.zfill(6).unique().tolist()
    else:
        all_codes = [str(c).strip().zfill(6) for c in pool]

    total_available = len(all_codes)
    if full:
        scan_pool = all_codes
    else:
        n = min(sample_size, len(all_codes))
        scan_pool = random.sample(all_codes, n)

    # ── 扫描 ──
    f = get_fetcher()
    results: list[dict] = []
    ok_count = 0
    failed_count = 0
    by_category: dict[str, int] = defaultdict(int)
    by_market: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    total = len(scan_pool)
    for i, code in enumerate(scan_pool):
        df = None
        error = None
        try:
            df = f.get_kline(code, days=60)
        except Exception as e:
            error = e

        cat, detail = classify_failure(code, df, error, today)
        entry = {"code": code, "category": cat, "detail": detail}

        # 获取股票名称
        try:
            entry["name"] = f.get_name(code) or code
        except Exception:
            entry["name"] = code

        # 市场分类
        mkt = _market_prefix(code)

        if cat == "OK":
            ok_count += 1
        else:
            failed_count += 1
            by_category[cat] += 1
            by_market[mkt][cat] += 1
            results.append(entry)

        if not quiet and (i + 1) % 50 == 0:
            pct = (i + 1) / total * 100
            print(f"\r  进度: {i+1}/{total} ({pct:.0f}%)  "
                  f"正常={ok_count} 异常={failed_count}", end="", flush=True)

    if not quiet:
        print(f"\r  完成: {total}/{total}  正常={ok_count}  异常={failed_count}  "
              f"耗时={time.time()-t0:.0f}s" + " " * 20)

    elapsed = round(time.time() - t0, 1)
    failure_rate = round(failed_count / total * 100, 2) if total > 0 else 0

    return {
        "scanned": total,
        "total_available": total_available,
        "ok": ok_count,
        "failed": failed_count,
        "failure_rate": failure_rate,
        "by_category": dict(by_category),
        "by_market": {k: dict(v) for k, v in by_market.items()},
        "failures": sorted(results, key=lambda x: x["category"]),
        "top_failure_codes": [r["code"] for r in results[:20]],
        "elapsed_s": elapsed,
        "timestamp": _dt.datetime.now().isoformat(),
    }


def _market_prefix(code: str) -> str:
    """推断股票所属市场。"""
    code = str(code).strip().zfill(6)
    if code.startswith("6"):
        return "SH(沪主板)"
    elif code.startswith(("0", "3")):
        return "SZ(深市)"
    elif code.startswith("4") or code.startswith("8"):
        return "BJ(北交所)"
    elif code.startswith("9"):
        return "BJ(北交所)"
    return "OTHER"


def print_report(report: dict):
    """格式化输出扫描报告。"""
    print("\n" + "=" * 70)
    print("  📊 K线数据健康扫描报告")
    print("=" * 70)
    print(f"  时间: {report['timestamp']}")
    print(f"  市场总票: {report.get('total_available', '?')}  扫描: {report['scanned']}  耗时: {report['elapsed_s']}s")
    print(f"  正常: {report['ok']} ({100 - report['failure_rate']:.1f}%)  "
          f"异常: {report['failed']} ({report['failure_rate']:.1f}%)")

    by_cat = report.get("by_category", {})
    if by_cat:
        print(f"\n  ┌─ 故障分类 ─")
        for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
            desc = CATEGORY.get(cat, "—")
            print(f"  │ {cat:15s} {count:4d} 只  {desc}")

    by_mkt = report.get("by_market", {})
    if by_mkt:
        print(f"\n  ┌─ 分市场统计 ─")
        for mkt, cats in sorted(by_mkt.items()):
            total = sum(cats.values())
            print(f"  │ {mkt}: 异常{total}只", end="")
            if total > 0:
                items = [f"{c}:{n}" for c, n in sorted(cats.items(), key=lambda x: -x[1])]
                print(f" ({', '.join(items)})", end="")
            print()

    failures = report.get("failures", [])
    if failures:
        print(f"\n  ┌─ 异常明细 (Top 20) ─")
        for f in failures[:20]:
            print(f"  │ {f['code']}({f.get('name','?')}) [{f['category']}] {f['detail'][:70]}")

    print("=" * 70)

    # 告警判断
    if report["failure_rate"] > 10:
        print(f"\n  🚨 告警: 异常率 {report['failure_rate']}% 超过 10% 阈值！")
    elif report["failure_rate"] > 5:
        print(f"\n  ⚠️  注意: 异常率 {report['failure_rate']}% 超过 5% 阈值")
    else:
        print(f"\n  ✅ 数据健康度良好（异常率 {report['failure_rate']}%）")


def main():
    parser = argparse.ArgumentParser(description="K线数据健康监控")
    parser.add_argument("--full", action="store_true", help="全市场扫描（全部5600只）")
    parser.add_argument("--sample", type=int, default=200, help="采样数量（默认200）")
    parser.add_argument("--pool", type=str, default=None,
                        help="指定市场代码前缀，逗号分隔，如 sh,sz,bj")
    parser.add_argument("--json", action="store_true", help="JSON输出（供自动化）")
    parser.add_argument("--alert-thresh", type=float, default=10.0,
                        help="告警阈值%（默认10）")
    parser.add_argument("--quiet", action="store_true", help="静默模式")

    args = parser.parse_args()

    # 构建筛选池
    pool = None
    if args.pool:
        from data.fetcher import get_fetcher
        f = get_fetcher()
        spot = f.get_market_spot()
        code_col = next((c for c in spot.columns if c in ("代码", "symbol", "code")), None)
        codes = spot[code_col].astype(str).str.strip().str.zfill(6).tolist()
        prefixes = [p.strip().lower() for p in args.pool.split(",")]
        prefix_map = {"sh": "6", "sz": ("0", "3"), "bj": ("4", "8", "9")}
        allowed = set()
        for pfx in prefixes:
            for k, v in prefix_map.items():
                if pfx == k:
                    if isinstance(v, tuple):
                        for vv in v:
                            allowed.add(vv)
                    else:
                        allowed.add(v)
        if allowed:
            pool = [c for c in codes if c[0] in allowed]
            print(f"按板块筛选: {args.pool} → {len(pool)} 只")

    report = scan_kline_health(
        pool=pool,
        sample_size=args.sample,
        full=args.full,
        quiet=args.json or args.quiet,
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)

    # 告警退出码
    if report.get("failure_rate", 0) > args.alert_thresh:
        sys.exit(2)  # 非零退出码供CI检测
    elif report.get("failed", 0) > 0:
        sys.exit(1)  # 有异常但未超阈值
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
