"""定时调度：每日定时执行选股推送与盘后复盘推送。

使用 APScheduler 常驻进程调度（工作日执行）。
也可单独调用各 job 函数，配合系统计划任务/cron 使用。
"""
from __future__ import annotations

import datetime as dt

from core.config import settings


def _is_trading_day(date: dt.date | None = None) -> bool:
    """简单判断是否为交易日（仅排除周末；节假日可结合 akshare 交易日历增强）。"""
    date = date or dt.date.today()
    if date.weekday() >= 5:
        return False
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        days = set(cal["trade_date"].astype(str))
        return date.strftime("%Y-%m-%d") in days
    except Exception:
        return True  # 获取失败时按工作日处理


def job_select(top_n: int = 10, force: bool = False) -> str:
    """每日选股并推送。"""
    if not force and not _is_trading_day():
        print("[选股] 非交易日，跳过")
        return ""
    from notify import push
    from report.generator import render_selection_markdown
    from report.selector import select_stocks

    print("[选股] 开始扫描...")
    picks = select_stocks(top_n=top_n)
    md = render_selection_markdown(picks)
    push("每日AI量化选股", md)
    print(f"[选股] 已推送 Top{top_n}")
    return md


def job_recommend(count: int = 5, force: bool = False, tailpick_mode: bool = False) -> str:
    """每日荐股（含反思迭代）并推送。

    tailpick_mode=True: 尾盘荐股模式，14:30运行，今日尾盘买入、次日验证。
    """
    mode_label = "尾盘荐股" if tailpick_mode else "每日荐股"
    if not force and not _is_trading_day():
        print(f"[{mode_label}] 非交易日，跳过")
        return ""
    from notify import push
    from recommend.engine import RecommendEngine

    print(f"[{mode_label}] 开始生成（结算→反思→选股）...")
    res = RecommendEngine().run(count=count)
    if res.get("error"):
        print(f"[{mode_label}] 失败：{res['error']}")
        return ""

    title_prefix = "🕝 尾盘荐股" if tailpick_mode else "📌 每日荐股"
    strategy_note = "> 🎯 策略：14:30 盘中推荐，今日尾盘买入，次日早盘验证。" if tailpick_mode else ""
    lines = [f"# {title_prefix}（基于 {res['base_date']} 数据）"]
    if strategy_note:
        lines.append(strategy_note)
    pwr = res.get("prev_winrate")
    if pwr:
        flag = "✅达标" if pwr["win_rate"] >= res["threshold"] else "⚠️未达标·已反思迭代"
        lines.append(f"> 昨日胜率 **{pwr['win_rate']*100:.0f}%**（线 {res['threshold']*100:.0f}%）{flag}")
    ref = res.get("reflection")
    if ref and ref.get("conclusion"):
        lines.append(f"> 🔁 反思：{ref['conclusion']}")
    if res.get("market_note"):
        lines.append(f"> 📉 {res['market_note']}")
    lines.append("")
    for i, p in enumerate(res["picks"], 1):
        lines.append(f"**{i}. {p.get('name','')}（{p['symbol']}）** ｜ {p.get('industry','—')} ｜ "
                     f"入场参考 ¥{p.get('entry_price','')} ｜ 评分 {p.get('score','')}")
        lines.append(f"   {p.get('reason','')}")
    md = "\n".join(lines)
    push(f"{title_prefix}·反思迭代", md)
    print(f"[{mode_label}] 已推送 Top{res['count']}")
    return md


def job_tail_recommend(count: int = 5, force: bool = False) -> str:
    """尾盘荐股：14:30 盘中推荐，当日尾盘买入、次日验证。"""
    from data.cache import invalidate_pattern
    cleared = invalidate_pattern()
    if cleared:
        print(f"[尾盘荐股] 已清除 {cleared} 个数据缓存，从 A-Stock 实时拉取...")
    return job_recommend(count=count, force=force, tailpick_mode=True)


def job_review(force: bool = False) -> str:
    """每日盘后复盘并推送。"""
    if not force and not _is_trading_day():
        print("[复盘] 非交易日，跳过")
        return ""
    from notify import push
    from review import run_review

    print("[复盘] 开始生成...")
    md = run_review()
    from config import get_review_config
    title = get_review_config().get("title", "每日盘后复盘")
    push(title, md)
    print("[复盘] 已推送")
    return md


def _parse_hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def start_scheduler():
    """启动常驻定时调度。"""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except Exception:
        print("未安装 APScheduler，请执行: pip install APScheduler")
        return

    sched = BlockingScheduler(timezone="Asia/Shanghai")

    sh, sm = _parse_hm(settings.select_push_time)
    rh, rm = _parse_hm(settings.review_push_time)
    ch, cm = _parse_hm(settings.recommend_push_time)
    th, tm = _parse_hm(settings.tail_recommend_push_time)

    sched.add_job(job_select, CronTrigger(day_of_week="mon-fri", hour=sh, minute=sm), id="select")
    sched.add_job(job_review, CronTrigger(day_of_week="mon-fri", hour=rh, minute=rm), id="review")
    sched.add_job(lambda: job_recommend(settings.recommend_count),
                  CronTrigger(day_of_week="mon-fri", hour=ch, minute=cm), id="recommend")
    if settings.tail_recommend_enabled:
        sched.add_job(lambda: job_tail_recommend(settings.recommend_count),
                      CronTrigger(day_of_week="mon-fri", hour=th, minute=tm), id="tail_recommend")

    print("=" * 50)
    print("定时调度已启动（工作日）：")
    if settings.tail_recommend_enabled:
        print(f"  🕝 尾盘荐股推送：{settings.tail_recommend_push_time}（盘中推荐·尾盘买入·次日验证）")
    print(f"  - 每日荐股推送：{settings.recommend_push_time}（含反思迭代）")
    print(f"  - 每日选股推送：{settings.select_push_time}")
    print(f"  - 盘后复盘推送：{settings.review_push_time}")
    print(f"  - 推送渠道：{', '.join(settings.channels)}")
    print("按 Ctrl+C 退出")
    print("=" * 50)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n调度已停止")


if __name__ == "__main__":
    start_scheduler()
