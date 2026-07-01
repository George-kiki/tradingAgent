"""定时调度：每日定时执行选股推送与盘后复盘推送。

使用 APScheduler 常驻进程调度（工作日执行）。
也可单独调用各 job 函数，配合系统计划任务/cron 使用。
"""
from __future__ import annotations

import datetime as dt
import json

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

    if tailpick_mode:
        from recommend.database import RecommendDB
        from recommend.winrate import settle_all_pending
        from tailpick.tail_engine import TailEngine

        print("[尾盘荐股] 开始生成（结算→尾盘专属引擎→落库）...")
        db = RecommendDB()
        fetcher = None
        try:
            from data.fetcher import get_fetcher
            fetcher = get_fetcher()
            settle_all_pending(db, fetcher)
        except Exception:
            pass

        res = TailEngine().run(count=count, force=force)
        if res.get("error"):
            print(f"[尾盘荐股] 失败：{res['error']}")
            return ""

        base_date = res.get("as_of", "")[:10]
        picks = res.get("picks", [])
        for p in picks:
            p["entry_price"] = p.get("entry_price") or p.get("price")
            p.setdefault("factors", {})
            p["factors"].update({
                "tail_engine": res.get("engine", "tail-dedicated"),
                "market_score": p.get("market_score"),
                "fund_score": p.get("fund_score"),
                "risk_penalty": p.get("risk_penalty"),
                "risk_flags": p.get("risk_flags", []),
            })
        db.save_recommendations(base_date, picks, mode="tail")

        prev_wr = db.latest_winrate(before=base_date, mode="tail")
        title_prefix = "🕝 尾盘荐股"
        lines = [
            f"# {title_prefix}（{base_date} 14:30）",
            "> 🎯 策略：尾盘买入，次日开盘/早盘验证。专属引擎：资金流 + 尾盘技术 + 市场情绪。",
        ]
        if prev_wr:
            flag = "✅达标" if prev_wr["win_rate"] >= 0.5 else "⚠️偏低·已收紧尾盘模型"
            lines.append(f"> 近期尾盘胜率 **{prev_wr['win_rate']*100:.0f}%**（{flag}）")
        market = res.get("market") or {}
        if market.get("note"):
            lines.append(f"> 市场：{market['note']}")
        if res.get("data_source"):
            lines.append(f"> 数据源：{res['data_source']}")
        if res.get("risk_note"):
            lines.append(f"> 风控：{res['risk_note']}")
        lines.append("")
        if not picks:
            lines.append("今日尾盘没有满足风控条件的标的，宁缺毋滥。")
        for i, p in enumerate(picks, 1):
            lines.append(f"**{i}. {p.get('name','')}（{p['symbol']}）** ｜ "
                         f"入场参考 ¥{p.get('entry_price') or ''} ｜ 评分 {p.get('score','')}")
            lines.append(f"   {p.get('reason','')}")
            if p.get("risk_flags"):
                lines.append(f"   风险：{'；'.join(p['risk_flags'])}")
        md = "\n".join(lines)
        push(f"{title_prefix}·专属引擎", md)
        print(f"[尾盘荐股] 已推送 Top{len(picks)}")
        return md

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
    from review.html_report import generate_and_store

    print("[复盘] 开始生成...")
    res = generate_and_store(store=True)
    from config import get_review_config
    title = get_review_config().get("title", "每日盘后复盘")
    m = res.get("metrics") or {}
    b = m.get("breadth") or {}
    q = m.get("quality") or {}
    sectors = m.get("top_sectors") or []
    sec_txt = "、".join(
        f"{s.get('name')}({s.get('pct')}%)" for s in sectors[:5] if s.get("name")
    ) or "暂无"
    md = "\n".join([
        f"# {title}",
        f"> 数据源：{q.get('source', '未知')}｜覆盖 {q.get('rows', '-')} 只｜{q.get('note', '')}",
        f"> 涨跌家数：{b.get('up', '-')} / {b.get('down', '-')}，涨停 {b.get('limit_up', '-')}，跌停 {b.get('limit_down', '-')}",
        f"> 强势板块：{sec_txt}",
        "",
        "完整 HTML 复盘已保存到本地历史记录，可在 Web「盘后复盘」查看。",
    ])
    push(title, md)
    print("[复盘] 已推送")
    return md


def _parse_hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def job_news_driven() -> str:
    """热点驱动扫描：多渠道抓取→本地持久化→异步预分析。每12小时执行。

    流程：
      1. 多渠道抓取全球新闻（东方财富/财联社/新浪/金十/抖音/Reuters/DuckDuckGo）
      2. 写入 news_raw 表（本地持久化）
      3. 异步触发预分析（过滤→多分析师→板块映射）
      4. 预分析结果写入 news_analysis 表
      5. 每7天自动清理>4天前的过期数据
      6. 扫描前后发送邮箱提醒（需配置 MAIL_ENABLED=true）

    用户点击"分析"时直接读 news_analysis 预存结果，秒级响应。
    """
    import time as _time

    # ── 扫描前：邮箱预告提醒 ──
    scan_count = 0
    try:
        from agents.mail_notify import get_scan_count, increment_scan_count, send_scan_pre_notify, should_notify
        cur_count, _ = get_scan_count()
        scan_count = cur_count + 1
        if should_notify():
            send_scan_pre_notify(scan_count, dt.datetime.now())
    except Exception as e:
        print(f"[邮件提醒] 预告发送异常（不影响主流程）: {e}")

    t0 = _time.time()
    result = {}

    print("[热点驱动] === 开始定时扫描 ===")
    try:
        from agents.news_orchestrator import run_full_scan, should_run_cleanup, cleanup_old_data
        from agents.llm import get_llm
        llm = get_llm()

        # 执行完整扫描（含抓取+存储+预分析）
        result = run_full_scan(llm=llm)
        sectors = result.get("sectors", [])
        print(f"[热点驱动] ✅ 扫描完成: {len(sectors)}个利好板块")

        # 7日数据清理
        if should_run_cleanup():
            print("[热点驱动] 执行7日数据清理...")
            cleanup = cleanup_old_data()
            print(f"[热点驱动] 清理完成: {cleanup}")

        return json.dumps(result, ensure_ascii=False) if sectors else ""
    except Exception as e:
        print(f"[热点驱动] ❌ 失败: {e}")
        return ""
    finally:
        # ── 扫描后：邮箱完成通知 ──
        try:
            duration = round((_time.time() - t0) / 60, 1)
            from agents.mail_notify import increment_scan_count, send_scan_done_notify, should_notify
            if should_notify():
                final_count = increment_scan_count(dt.datetime.now())
                send_scan_done_notify(final_count, duration, result)
        except Exception as e:
            print(f"[邮件提醒] 完成通知异常（不影响主流程）: {e}")


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

    # 热点驱动扫描：启动后2分钟首次执行，之后每6小时
    from apscheduler.triggers.interval import IntervalTrigger as _Interval
    sched.add_job(job_news_driven, _Interval(hours=6), id="news_driven",
                  next_run_time=dt.datetime.now() + dt.timedelta(minutes=2))

    print("=" * 50)
    print("定时调度已启动（工作日）：")
    if settings.tail_recommend_enabled:
        print(f"  🕝 尾盘荐股推送：{settings.tail_recommend_push_time}（盘中推荐·尾盘买入·次日验证）")
    print(f"  - 每日荐股推送：{settings.recommend_push_time}（含反思迭代）")
    print(f"  - 每日选股推送：{settings.select_push_time}")
    print(f"  - 盘后复盘推送：{settings.review_push_time}")
    print(f"  - 🌍 热点驱动扫描：每6小时（全球新闻→多Agent→板块映射）")
    print(f"  - 推送渠道：{', '.join(settings.channels)}")
    print("按 Ctrl+C 退出")
    print("=" * 50)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n调度已停止")


if __name__ == "__main__":
    start_scheduler()
