"""AI-Agent A股分析系统 - 命令行入口。

用法示例：
    python main.py analyze 600519              # 多智能体分析单只股票
    python main.py backtest 600519 --strategy ma_cross   # 回测
    python main.py backtest 600519 --all       # 全部策略回测对比
    python main.py select --top 10             # 每日量化选股
    python main.py strategies                  # 列出所有策略
    python main.py web                         # 启动 Web 服务
"""
from __future__ import annotations

import argparse
import sys

# Windows 控制台默认 GBK，打印 emoji 会触发 UnicodeEncodeError，统一切到 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from rich.console import Console
from rich.table import Table

console = Console()


def cmd_analyze(args):
    from agents.orchestrator import AgentOrchestrator
    from report.generator import render_analysis_markdown, save_report

    console.print(f"[bold cyan]开始分析 {args.symbol} ...[/bold cyan]")
    result = AgentOrchestrator().analyze(args.symbol)

    d = result["decision"]
    console.print(f"\n[bold]{result['name']}（{result['symbol']}）[/bold]")
    console.print(f"模式：{'多智能体' if result['llm_enabled'] else '纯量化(未配置LLM)'}")
    console.print(f"[bold green]决策：{d.get('action')}  信心：{d.get('confidence')}[/bold green]")
    console.print(f"结论：{d.get('summary')}")

    md = render_analysis_markdown(result)
    path = save_report(md, args.symbol)
    console.print(f"\n[dim]完整报告已保存：{path}[/dim]")


def cmd_backtest(args):
    from backtest.engine import run_backtest
    from data.fetcher import get_fetcher
    from strategies.library import STRATEGY_REGISTRY

    df = get_fetcher().get_kline(args.symbol, days=args.days)
    if df.empty:
        console.print("[red]无法获取行情数据[/red]")
        return

    names = list(STRATEGY_REGISTRY) if args.all else [args.strategy]
    table = Table(title=f"{args.symbol} 回测结果（近 {args.days} 日）")
    for col in ["策略", "累计收益%", "基准%", "超额%", "年化%", "最大回撤%", "夏普", "胜率%", "交易数"]:
        table.add_column(col, justify="right")

    for name in names:
        try:
            r = run_backtest(df, name).to_dict()
            table.add_row(
                r["strategy"], f"{r['total_return']}", f"{r['benchmark_return']}",
                f"{r['excess_return']}", f"{r['annual_return']}", f"{r['max_drawdown']}",
                f"{r['sharpe']}", f"{r['win_rate']}", f"{r['trade_count']}",
            )
        except Exception as e:
            console.print(f"[red]{name} 回测失败: {e}[/red]")
    console.print(table)


def cmd_select(args):
    from report.generator import render_selection_markdown, save_report
    from report.selector import select_stocks

    console.print("[bold cyan]正在扫描股票池进行量化选股 ...[/bold cyan]")
    picks = select_stocks(top_n=args.top)
    table = Table(title=f"AI 量化选股 Top {args.top}")
    for col in ["排名", "代码", "名称", "现价", "涨跌%", "评分", "买入信号"]:
        table.add_column(col)
    for i, p in enumerate(picks, 1):
        table.add_row(str(i), p["symbol"], p["name"], str(p["close"]),
                      str(p["pct_change"]), str(p["score"]), str(p["buy_signals"]))
    console.print(table)

    md = render_selection_markdown(picks)
    path = save_report(md, "selection")
    console.print(f"[dim]选股报告已保存：{path}[/dim]")


def cmd_strategies(args):
    from strategies.library import list_strategies
    table = Table(title="可用交易策略")
    table.add_column("代号", style="cyan")
    table.add_column("名称")
    table.add_column("说明")
    for s in list_strategies():
        table.add_row(s["name"], s["cn_name"], s["description"])
    console.print(table)


def cmd_review(args):
    from review import run_review
    from report.generator import save_report
    console.print("[bold cyan]正在生成盘后复盘 ...[/bold cyan]")
    md = run_review()
    console.print(md)
    if args.push:
        from notify import push
        from config import get_review_config
        title = get_review_config().get("title", "每日盘后复盘")
        res = push(title, md)
        console.print(f"[dim]推送结果：{res}[/dim]")
    path = save_report(md, "review")
    console.print(f"[dim]复盘报告(Markdown)已保存：{path}[/dim]")

    if not args.no_html:
        from review.html_report import generate_html_review, save_html_review
        console.print("[bold cyan]正在生成 HTML 复盘报告（模板同款）...[/bold cyan]")
        try:
            html_path = save_html_review(generate_html_review())
            console.print(f"[bold green]复盘报告(HTML)已保存：{html_path}[/bold green]")
        except Exception as e:
            console.print(f"[red]HTML 复盘生成失败：{e}[/red]")


def cmd_push_select(args):
    from scheduler import job_select
    console.print("[bold cyan]生成选股并推送 ...[/bold cyan]")
    job_select(top_n=args.top, force=args.force)


def cmd_recommend(args):
    from recommend.engine import RecommendEngine

    eng = RecommendEngine()
    if args.backfill:
        console.print(f"[bold cyan]回填最近 {args.backfill} 个交易日的荐股并迭代反思 ...[/bold cyan]")
        rows = eng.backfill(n_days=args.backfill, count=args.count)
        table = Table(title="历史回填胜率")
        for col in ["日期", "推荐数", "最新批次胜率%"]:
            table.add_column(col)
        for r in rows:
            wr = r.get("winrate") or {}
            table.add_row(r["base_date"], str(r["picks"]),
                          f"{(wr.get('win_rate') or 0)*100:.0f}")
        console.print(table)

    console.print("[bold cyan]生成今日荐股 ...[/bold cyan]")
    res = eng.run(as_of=args.date or None, count=args.count)
    if res.get("error"):
        console.print(f"[red]{res['error']}[/red]")
        return

    pwr = res.get("prev_winrate")
    if pwr:
        flag = "达标" if pwr["win_rate"] >= res["threshold"] else "未达标→已反思"
        console.print(f"昨日({pwr['base_date']})胜率：[bold]{pwr['win_rate']*100:.0f}%[/bold]"
                      f"（线 {res['threshold']*100:.0f}%，{flag}）")
    ref = res.get("reflection")
    if ref:
        console.print(f"[yellow]反思结论：{ref.get('conclusion', '')}[/yellow]")
    if res.get("market_note"):
        console.print(f"[magenta]{res['market_note']}[/magenta]")

    table = Table(title=f"每日荐股（基于 {res['base_date']} 数据）Top {res['count']}")
    for col in ["排名", "代码", "名称", "板块", "入场价", "评分", "RSI", "选中原因"]:
        table.add_column(col)
    for i, p in enumerate(res["picks"], 1):
        table.add_row(str(i), p["symbol"], p.get("name", ""), p.get("industry", "—"),
                      str(p.get("entry_price", "")), str(p.get("score", "")),
                      f"{p['factors'].get('rsi', 0):.0f}", (p.get("reason", "") or "")[:40] + "...")
    console.print(table)
    console.print("[dim]结果已持久化到本地数据库。打开 Web「每日荐股」可看卡片式展示。[/dim]")


def cmd_tail_recommend(args):
    """尾盘荐股：14:30 盘中推荐，今日尾盘买入、次日验证。"""
    from scheduler import job_tail_recommend

    console.print("[bold cyan]🕝 生成尾盘荐股（盘中推荐·尾盘买入·次日验证）...[/bold cyan]")
    job_tail_recommend(count=args.count, force=args.force)


def cmd_value(args):
    from value.engine import ValueEngine

    console.print(f"[bold cyan]价值挖掘五步分析：{args.target} ...[/bold cyan]")
    res = ValueEngine().run(args.target, focus_symbol=args.symbol)
    if res.get("error"):
        console.print(f"[red]{res['error']}[/red]")
        return

    s1 = res.get("bottlenecks", {})
    console.print(f"\n[bold]① 物理级瓶颈[/bold]（{s1.get('industry','')}）：{s1.get('summary','')}")
    bt = Table(title="瓶颈环节")
    for col in ["环节", "壁垒类型", "扩产周期", "严重度"]:
        bt.add_column(col)
    for b in s1.get("bottlenecks", [])[:5]:
        bt.add_row(b.get("link", ""), "/".join(b.get("barrier_type", []) if isinstance(b.get("barrier_type"), list) else [str(b.get("barrier_type", ""))]),
                   b.get("expansion_cycle", ""), b.get("severity", ""))
    console.print(bt)

    console.print("\n[bold]② 非对称标的[/bold]")
    ct = Table()
    for col in ["名称", "代码", "市值(亿)", "区间", "卡位环节", "覆盖度"]:
        ct.add_column(col)
    for t in res.get("candidates", {}).get("targets", [])[:5]:
        ct.add_row(t.get("name", ""), str(t.get("symbol", "")),
                   str(t.get("market_cap_yi", "—")),
                   "✓" if t.get("in_cap_range") else ("✗" if t.get("in_cap_range") is False else "?"),
                   t.get("bottleneck", ""), t.get("coverage", ""))
    console.print(ct)

    pr = res.get("primary", {})
    s3 = res.get("financial", {})
    console.print(f"\n[bold]③ 财务拐点[/bold]（{pr.get('name','')} {pr.get('symbol','')}）：")
    console.print(f"  毛利率拐点：{s3.get('gross_margin_inflection', {}).get('verdict','')}；"
                  f"CapEx 爬坡：{s3.get('capex_ramp', {}).get('verdict','')}；确认度 {s3.get('score','-')}")

    s4 = res.get("redteam", {})
    console.print(f"\n[bold]④ 红队证伪[/bold]：[yellow]{s4.get('verdict','')}[/yellow] — {s4.get('fatal_risk','')}")

    s5 = res.get("circuit", {})
    console.print("\n[bold]⑤ 熔断里程碑[/bold]")
    mt = Table()
    for col in ["时间窗", "类型", "里程碑", "达成标准", "未达成", "优先级"]:
        mt.add_column(col)
    for m in s5.get("milestones", [])[:8]:
        mt.add_row(m.get("window", ""), m.get("event_type", ""), (m.get("milestone", "") or "")[:24],
                   (m.get("success_criteria", "") or "")[:20], m.get("fail_action", ""), m.get("priority", ""))
    console.print(mt)
    console.print(f"[red]熔断规则：{s5.get('circuit_breaker','')}[/red]")
    console.print("[dim]结果已持久化。打开 Web「💎 价值挖掘」可看卡片式分步展示。[/dim]")


def cmd_schedule(args):
    from scheduler import start_scheduler
    start_scheduler()


def cmd_web(args):
    import uvicorn
    from core.config import settings
    console.print(f"[bold green]启动 Web 服务：http://{settings.web_host}:{settings.web_port}[/bold green]")
    uvicorn.run("web.app:app", host=settings.web_host, port=settings.web_port, reload=False)


def cmd_kline_health(args):
    from tools.kline_health_monitor import scan_kline_health, print_report
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
                    allowed.update(v if isinstance(v, tuple) else (v,))
        if allowed:
            pool = [c for c in codes if c[0] in allowed]
    report = scan_kline_health(pool=pool, sample_size=args.sample, full=args.full)
    if args.json:
        import json
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)


def cmd_news_driven(args):
    """热点驱动扫描CLI入口。"""
    from agents.news_orchestrator import run_full_scan
    from agents.llm import get_llm
    console.print("[bold cyan]🌍 热点驱动扫描启动...[/bold cyan]")
    result = run_full_scan(llm=get_llm())
    sectors = result.get("sectors", [])
    console.print(f"\n[bold green]✅ 扫描完成[/bold green]")
    console.print(f"  抓取新闻: {result.get('news_count', 0)}")
    console.print(f"  过滤保留: {result.get('filtered_count', 0)}")
    console.print(f"  利好板块: {len(sectors)}")
    for i, s in enumerate(sectors[:5], 1):
        console.print(f"  {i}. {s.get('sector','?')} (评分{s.get('bullish_score',0)}) - {s.get('catalyst','')[:60]}")
        for st in (s.get("stocks") or [])[:3]:
            console.print(f"     → {st.get('name')}({st.get('code')}) {st.get('score')}分")


def build_parser():
    p = argparse.ArgumentParser(description="AI-Agent A股智能分析系统")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze", help="多智能体分析单只股票")
    a.add_argument("symbol", help="股票代码，如 600519")
    a.set_defaults(func=cmd_analyze)

    b = sub.add_parser("backtest", help="策略回测")
    b.add_argument("symbol", help="股票代码")
    b.add_argument("--strategy", default="ma_cross", help="策略代号")
    b.add_argument("--all", action="store_true", help="对比全部策略")
    b.add_argument("--days", type=int, default=250, help="回测天数")
    b.set_defaults(func=cmd_backtest)

    s = sub.add_parser("select", help="量化选股")
    s.add_argument("--top", type=int, default=10, help="选取数量")
    s.set_defaults(func=cmd_select)

    st = sub.add_parser("strategies", help="列出所有策略")
    st.set_defaults(func=cmd_strategies)

    r = sub.add_parser("review", help="生成盘后复盘")
    r.add_argument("--push", action="store_true", help="生成后推送到配置的渠道")
    r.add_argument("--no-html", action="store_true", help="不生成 HTML 复盘报告")
    r.set_defaults(func=cmd_review)

    ps = sub.add_parser("push-select", help="生成选股并推送")
    ps.add_argument("--top", type=int, default=10)
    ps.add_argument("--force", action="store_true", help="非交易日也执行")
    ps.set_defaults(func=cmd_push_select)

    rc = sub.add_parser("recommend", help="每日荐股 + 反思迭代")
    rc.add_argument("--count", type=int, default=5, help="推荐数量")
    rc.add_argument("--date", default="", help="基准数据日期 YYYY-MM-DD（默认最新交易日）")
    rc.add_argument("--backfill", type=int, default=0, help="先回填最近 N 个交易日以构建历史/触发反思")
    rc.set_defaults(func=cmd_recommend)

    trc = sub.add_parser("tail-recommend", help="尾盘荐股：14:30 盘中推荐，尾盘买入·次日验证")
    trc.add_argument("--count", type=int, default=5, help="推荐数量")
    trc.add_argument("--force", action="store_true", help="非交易日也执行")
    trc.set_defaults(func=cmd_tail_recommend)

    v = sub.add_parser("value", help="价值挖掘五步分析（瓶颈→标的→财务→证伪→熔断）")
    v.add_argument("target", help="行业或标的，如 “光伏胶膜” 或 600519")
    v.add_argument("--symbol", default="", help="聚焦的A股代码（用于财务穿透），可选")
    v.set_defaults(func=cmd_value)

    sc = sub.add_parser("schedule", help="启动定时调度（常驻）")
    sc.set_defaults(func=cmd_schedule)

    w = sub.add_parser("web", help="启动 Web 服务")
    w.set_defaults(func=cmd_web)

    kh = sub.add_parser("kline-health", help="K线数据健康扫描（采样200只，分类故障原因）")
    kh.add_argument("--sample", type=int, default=200)
    kh.add_argument("--full", action="store_true")
    kh.add_argument("--pool", type=str, default=None, help="sh,sz,bj")
    kh.add_argument("--json", action="store_true")
    kh.add_argument("--alert-thresh", type=float, default=10.0)
    kh.set_defaults(func=cmd_kline_health)

    nd = sub.add_parser("news-driven", help="热点驱动扫描：全球新闻→多Agent→板块映射")
    nd.set_defaults(func=cmd_news_driven)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
