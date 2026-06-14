"""报告生成器：将分析结果渲染为 Markdown 决策报告。"""
from __future__ import annotations

import datetime as dt
import os


def render_analysis_markdown(result: dict) -> str:
    """把 orchestrator.analyze 的结果渲染为 Markdown。"""
    name = result.get("name", "")
    symbol = result.get("symbol", "")
    snap = result.get("snapshot", {})
    decision = result.get("decision", {})
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"# 📊 {name}（{symbol}）智能分析报告",
        f"> 生成时间：{now}　|　模式：{'多智能体' if result.get('llm_enabled') else '纯量化'}",
        "",
        "## 一、决策结论",
        "",
        f"- **操作建议**：{decision.get('action', '-')}",
        f"- **信心指数**：{decision.get('confidence', '-')}",
        f"- **目标价**：{decision.get('target_price', '-')}",
        f"- **止损位**：{decision.get('stop_loss', '-')}",
        f"- **建议仓位**：{decision.get('position', '-')}",
        f"- **操作周期**：{decision.get('horizon', '-')}",
        f"- **核心结论**：{decision.get('summary', '-')}",
        "",
    ]

    reasons = decision.get("reasons") or []
    if reasons:
        lines.append("**主要理由：**")
        lines += [f"{i+1}. {r}" for i, r in enumerate(reasons)]
        lines.append("")

    # 行情快照
    lines += [
        "## 二、行情与技术快照",
        "",
        f"| 指标 | 数值 | 指标 | 数值 |",
        f"| --- | --- | --- | --- |",
        f"| 最新价 | {snap.get('close')} | 涨跌幅 | {snap.get('change_pct')}% |",
        f"| MA5 | {snap.get('ma5')} | MA20 | {snap.get('ma20')} |",
        f"| MA60 | {snap.get('ma60')} | RSI | {snap.get('rsi')} |",
        f"| MACD-DIF | {snap.get('macd_dif')} | MACD-DEA | {snap.get('macd_dea')} |",
        f"| KDJ-K | {snap.get('kdj_k')} | KDJ-D | {snap.get('kdj_d')} |",
        f"| 布林上轨 | {snap.get('boll_up')} | 布林下轨 | {snap.get('boll_low')} |",
        f"| ATR | {snap.get('atr')} | 动量 | {snap.get('momentum')}% |",
        "",
    ]

    # 量化策略信号
    quant = result.get("quant_consensus", {})
    lines += [
        "## 三、量化策略信号",
        "",
        f"综合评分：**{quant.get('avg_score')}**　|　"
        f"买入信号：{quant.get('buy_signals')}　卖出信号：{quant.get('sell_signals')}　"
        f"（共 {quant.get('total_strategies')} 个策略）",
        "",
        "| 策略 | 信号 | 评分 | 理由 |",
        "| --- | --- | --- | --- |",
    ]
    for s in result.get("strategy_signals", []):
        lines.append(f"| {s['strategy']} | {s['signal']} | {s['score']} | {s['reason']} |")
    lines.append("")

    # 多智能体内容
    if result.get("llm_enabled"):
        reports = result.get("analyst_reports", {})
        if reports:
            lines.append("## 四、分析师团队报告")
            lines.append("")
            for role, text in reports.items():
                lines += [f"### {role}", "", text, ""]

        debate = result.get("debate", {})
        if debate.get("transcript"):
            lines += ["## 五、多空辩论", ""]
            for r in debate["transcript"]:
                lines += [
                    f"**第 {r['round']} 轮**",
                    f"- 🐂 多头：{r['bull']}",
                    f"- 🐻 空头：{r['bear']}",
                    "",
                ]

        risk = result.get("risk_assessment")
        if risk:
            lines += ["## 六、风险评估", "", risk, ""]

    lines += [
        "---",
        "> ⚠️ **免责声明**：本报告由 AI 系统自动生成，仅供学习与研究使用，"
        "不构成任何投资建议。股市有风险，投资需谨慎。",
    ]
    return "\n".join(lines)


def save_report(content: str, symbol: str, out_dir: str = "reports") -> str:
    os.makedirs(out_dir, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"{symbol}_{ts}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def render_selection_markdown(picks: list[dict]) -> str:
    """渲染每日选股结果。"""
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 📈 每日 AI 量化选股",
        f"> 生成时间：{now}",
        "",
        "| 排名 | 代码 | 名称 | 现价 | 涨跌幅 | 综合评分 | 买入信号 | 触发策略 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, p in enumerate(picks, 1):
        strat = "、".join(p.get("buy_strategies", [])) or "-"
        lines.append(
            f"| {i} | {p['symbol']} | {p['name']} | {p['close']} | "
            f"{p['pct_change']}% | {p['score']} | {p['buy_signals']} | {strat} |"
        )
    lines += [
        "",
        "> ⚠️ 仅供研究参考，不构成投资建议。",
    ]
    return "\n".join(lines)
