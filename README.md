# 📊 AI-Agent A股智能分析系统

一个面向**中国内地股市（A股）**的多智能体 + 多策略量化分析系统。融合了
[TradingAgents](https://github.com/TauricResearch/TradingAgents) 的「多空辩论」多智能体思想，
与 daily_stock_analysis 的「多交易策略」实践，使用免费的 **AkShare** 数据源 + 国产 **DeepSeek** 大模型。

> ⚠️ **免责声明**：本项目仅供学习与研究使用，**不构成任何投资建议**，不提供实盘交易功能。股市有风险，投资需谨慎。

---

## ✨ 核心功能

| 模块 | 说明 |
|------|------|
| 🤖 **多智能体分析** | 技术面 / 基本面 / 舆情分析师 → 多空研究员多轮辩论 → 风控经理 → 首席交易决策 |
| 📈 **多交易策略** | 内置 **10 种策略**：均线金叉、MACD、KDJ、布林带、多头趋势、RSI、高点突破、放量突破、**缠论分型、网格交易** |
| 🔬 **策略回测** | 向量化回测引擎，T+1 成交、含手续费/印花税，输出收益/回撤/夏普/胜率 |
| 🎯 **量化选股** | 对股票池用全部策略综合打分排序，输出每日选股 |
| 📌 **每日荐股 + 反思迭代** | 基于前一日数据推荐 5 只（**过滤科创板/创业板**），结果落本地 SQLite；**昨日胜率<70% 自动反思**（5维诊断），动态调整策略权重与约束以迭代提升胜率 |
| 📋 **盘后复盘** | **多种可自定义复盘模式**（大盘/市场广度/热点板块/自选股/持仓/AI总结），配置驱动、高度自定义 |
| 🔔 **定时推送** | 每日选股 + 盘后复盘**定时自动推送**到企业微信/飞书/邮箱 |
| 📉 **K线可视化** | Web 端 ECharts 蜡烛图 + 均线 + 成交量 + MACD 副图 |
| 🗄️ **双数据源** | 默认免费 AkShare；配置 Tushare token 后**自动切换增强数据源并降级容错** |
| 📝 **决策报告** | 自动生成 Markdown 分析/选股/复盘报告 |
| 🌐 **Web 界面** | FastAPI + 单页前端，可视化分析、K线、回测、选股、复盘 |

---

## 🏗️ 项目结构

```
ai-agent/
├── core/              # 配置 + 技术指标库（纯 pandas 实现）
├── data/              # 数据采集（AkShare + 可选 Tushare）+ 本地缓存
│   ├── fetcher.py     # 统一入口（含市场广度/热点/指数）
│   └── tushare_source.py
├── strategies/        # 10 种交易策略（可回测+实时信号）
├── backtest/          # 向量化回测引擎与绩效评估
├── agents/            # 多智能体（DeepSeek 驱动）
│   ├── analysts.py / researchers.py / risk_manager.py / trader.py
│   └── orchestrator.py
├── review/            # 盘后复盘（可插拔多模式）
│   ├── modes.py       # 6 种复盘模式，可自定义/扩展
│   └── engine.py
├── recommend/         # 每日荐股 + 反思迭代
│   ├── database.py    # 本地 SQLite（推荐/结果/胜率/反思/策略权重 5表）
│   ├── winrate.py     # 胜率计算与结算（次日收益判定）
│   ├── reflection.py  # 反思机制（6维诊断 + 权重/约束自动调整 + LLM结论）
│   ├── fundamentals.py# 多维评分（估值/业绩/北向/龙虎榜/放量/利好）
│   └── engine.py      # 荐股引擎（两阶段评分→结算→反思→落库）
├── value/             # 价值挖掘（五步智能体）
│   ├── data.py        # 财务数据层（毛利率/CapEx趋势、标的市值校验）
│   ├── engine.py      # 五步编排（瓶颈→标的→财务拐点→红队证伪→熔断）
│   └── store.py       # 分析结果持久化（本地 JSON + 历史）
├── notify/            # 消息推送（控制台/企业微信/飞书/邮箱）
├── report/            # 选股 + 报告生成
├── config/            # 用户配置（自选股/持仓/复盘模式）
│   ├── watchlist.json
│   └── review.json
├── web/               # FastAPI + 前端页面（ECharts K线）
├── scheduler.py       # 定时调度（每日选股/复盘自动推送）
├── main.py            # CLI 入口
└── requirements.txt
```

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

> 建议 Python 3.10+。首次运行 AkShare 会联网获取数据。

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，填入 DeepSeek API Key（在 https://platform.deepseek.com 申请）：

```
DEEPSEEK_API_KEY=sk-你的key
ENABLE_LLM=true
```

> 💡 **不配置 LLM 也能用**：将 `ENABLE_LLM=false` 或不填 key，系统自动降级为「纯量化模式」，
> 仅用 8 种策略综合评分给出决策，不消耗任何 token。

### 3. 使用方式

**命令行：**

```bash
python main.py strategies                 # 查看所有策略（10 种）
python main.py analyze 600519             # 多智能体分析贵州茅台
python main.py backtest 600519 --all      # 全部策略回测对比
python main.py recommend                  # 每日荐股5只（含昨日胜率结算+反思迭代）
python main.py recommend --backfill 6     # 先回填最近6个交易日构建历史/触发反思
python main.py value 光伏胶膜              # 价值挖掘五步分析（瓶颈→标的→财务→证伪→熔断）
python main.py backtest 600519 --strategy chan --days 500
python main.py select --top 10            # 量化选股 Top10
python main.py review                      # 生成盘后复盘
python main.py review --push              # 复盘并推送到配置渠道
python main.py push-select --top 10       # 选股并推送
python main.py schedule                    # 启动定时调度（常驻）
python main.py web                        # 启动 Web 服务
```

**Web 界面：**

```bash
python main.py web
# 浏览器打开 http://127.0.0.1:8000
# 含：智能分析 / K线图表 / 策略回测 / 量化选股 / 盘后复盘 / 策略库
```

### 4. 定时自动推送（可选）

1. 在 `.env` 配置推送渠道（任选）：
   ```
   PUSH_CHANNELS=wechat,email          # 启用的渠道
   WECHAT_WEBHOOK=https://qyapi.weixin.qq.com/...   # 企业微信群机器人
   SMTP_HOST=smtp.qq.com
   SMTP_USER=你的邮箱
   SMTP_PASSWORD=授权码
   SMTP_TO=收件人
   SELECT_PUSH_TIME=09:00              # 每日选股推送时间
   REVIEW_PUSH_TIME=15:30             # 盘后复盘推送时间
   ```
2. 启动常驻调度：`python main.py schedule`（工作日自动执行）
3. 或用系统计划任务/cron 调用 `python main.py push-select` 和 `python main.py review --push`

---

## 📌 每日荐股 + 反思迭代

一个会"自我复盘、自我进化"的荐股系统：每日基于**前一日市场数据**推荐 5 只股票（自动过滤
**科创板 688 / 创业板 300·301 / 北交所 / B股**），结果**持久化到本地 SQLite**；并引入**反思机制**——
当昨日荐股胜率低于阈值（默认 70%）时，自动诊断失败原因，并将反思结论作为约束融入今日选股逻辑，
持续迭代提升胜率。

### 1. 基于昨日数据的选股指标与策略（两阶段·多维度）

**阶段一 · 技术面初筛全池**（截至前一日 K线）：
- **多策略共识**：10 个交易策略的实时信号按**动态权重**加权（权重由反思迭代调整）
- **趋势**：均线多头排列(ma5>ma10>ma20) 加分
- **量能**：温和放量(1.2~3倍) 加分；异常巨量(>4倍) 减分
- **强弱**：RSI 超跌(<35) 加分；超买(>78) 减分（防追高）
- **位置**：52周价格位置过高(>90%) 减分；**动量**：近5日涨幅过大(>18%) 减分

**阶段二 · 多维度富化重排**（仅对靠前候选，控制网络开销）：

| 维度 | 数据 | 评分倾向 |
|------|------|---------|
| 💰 **估值** | PE / PB | 合理低估加分，亏损/高估减分 |
| 📈 **业绩** | ROE / 净利同比 / 营收同比 | 高盈利高成长加分 |
| 🌐 **北向资金** | 陆股通持股占比及5日增减持 | 增持加分 |
| 🐲 **龙虎榜** | 近一月上榜次数 / 净买额 | 净买入加分 |
| 📊 **放量** | 量比 | 温和放量加分 |
| 📰 **近期利好** | 新闻标题关键词情感 | 利好加分、利空减分 |

> **综合分 = 技术面分 + 多维加分（上限 ±0.3）**，重排后取 Top N。各维度 best-effort，
> 取不到的维度按中性处理、不影响其余维度。

- 叠加**约束(filters)** 硬过滤：RSI上限 / 位置上限 / 最低分 / 要求资金净流入 / **要求基本面为正** / 单板块上限 / 大盘择时

### 2. 本地数据库表结构（SQLite，`data_store/recommend.db`）

| 表 | 作用 | 关键字段 |
|----|------|---------|
| `recommendations` | 每日推荐明细（含选股因子快照，供反思） | base_date, symbol, rank, score, entry_price, factors(JSON), kline_mini |
| `recommendation_results` | 推荐结果回评 | base_date, eval_date, entry_price, eval_price, next_pct, is_win |
| `daily_winrate` | 批次胜率汇总 | base_date, total, wins, **win_rate**, avg_return, best/worst |
| `reflections` | 反思记录 | based_on_date, prev_win_rate, dimensions(JSON), conclusion, adjustments(JSON) |
| `strategy_weights` | 动态权重与约束（迭代载体） | effective_date, weights(JSON), filters(JSON), source |

### 3. 胜率计算规则

1. 入场参考价 = 推荐所依据的**前一日(base_date)收盘价**
2. 评估日 = base_date 的**下一交易日**；次日涨跌幅 `next_pct = (评估日收盘/入场价 - 1)×100`
3. 判定**赢**：`next_pct > win_pct_threshold`（默认 0，即次日收红即赢；可配 0.5 表示需涨超 0.5%）
4. 批次胜率 = 赢的只数 / 已评估总只数；同时记录平均次日收益、最佳/最差个股
5. 结算天然滞后一个交易日（昨日荐股，今日见分晓），符合 A股 T+1

### 4. 胜率不达标时的反思维度与策略自动调整

胜率 < 70% 时触发，**5 大反思维度**逐一诊断 + 自动调整：

| 反思维度 | 诊断 | 自动调整 |
|---------|------|---------|
| **选股指标/策略失效** | 统计各触发策略对应个股次日收益，定位拖后腿的策略 | 按表现自适应**调权**：`new_w = clip(old_w×(1+clip(avg_pct/3,-0.5,0.5)),0.2,2)` |
| **追高风险** | 亏损股是否普遍 RSI 偏高 / 处 52周高位 | 加约束 `max_rsi=70`、`max_pos_52w=85` |
| **资金流背离** | 亏损股是否主力净流出却被选中 | 加约束 `require_fund_inflow=true` |
| **板块集中度** | 是否过度集中于某走弱板块 | 加约束 `max_per_sector=2` |
| **市场情绪误判** | 对比次日大盘，区分**系统性下跌(择时问题)** vs **个股选择问题** | 系统性下跌则加约束 `market_timing=true`（弱市抬高门槛） |
| **基本面/估值误判** | 亏损股是否普遍高估/业绩下滑 | 加约束 `require_positive_fundamental=true`（要求基本面综合加分为正） |

诊断汇总成**反思结论**；若配置 DeepSeek，则由 LLM 进一步凝练为自然语言"约束指令"。
所有调整落库到 `strategy_weights`，**下次选股自动生效**，形成闭环迭代。

> Web 端「📌 每日荐股」Tab：卡片式展示推荐（名称/代码/板块/入场价/迷你K线/资金/选中原因），
> 顶部胜率看板 + 反思看板（5维诊断 + 调整后约束一目了然）。

---

## 💎 价值挖掘（五步智能体）

针对**指定行业或标的**，与 Agent 进行五步交互，自上而下挖掘卡住产业瓶颈的「隐形冠军」，
并配套可证伪的熔断机制。**需配置 DeepSeek**（纯 LLM 驱动的智能体工作流）。

| 步骤 | 角色 | 产出 |
|------|------|------|
| **① 逆向拆解瓶颈** | 产业研究专家 | 逆向工程拆解 BOM，识别**扩产周期长/技术门槛高/不可替代**的物理级瓶颈环节 |
| **② 锁定非对称标的** | 隐形冠军猎手 | 基于瓶颈，筛选**市值30-150亿、机构覆盖低**、低成本占比+高失效风险的中小盘 A股 |
| **③ 穿透财务拐点** | 财务侦探 | 注入**真实毛利率/CapEx 趋势**，验证近两季毛利率是否现拐点、资本开支是否秘密爬坡 |
| **④ AI 红队测试** | 空头分析师 | 从**技术路径替代/大客户自研/供应链断裂**三维度撰写证伪报告，穷尽"死路" |
| **⑤ 指定熔断机制** | 风控官 | 拟定未来 6 个月**可证伪里程碑**（订单/良率/专利…），未达成即判定逻辑失效强制清仓 |

- 每步前一结论作为后一步**约束**，串行迭代；步骤 3 注入真实财务数据（来自财务摘要+现金流量表）。
- 结果**持久化到本地 JSON**（`data_store/value_analyses.json`），支持历史回看。
- Web 端「💎 价值挖掘」Tab：五步分步卡片 + 毛利率/CapEx 迷你走势图 + 红队三维证伪 + 熔断里程碑表格。

```bash
python main.py value 光伏胶膜                 # 行业入手，自动挖掘隐形冠军
python main.py value 固态电池 --symbol 600xxx # 指定行业并聚焦某标的做财务穿透
```

---

## 🧠 多智能体工作流

```
         ┌─────────────────────────────────────────┐
         │  数据采集 (AkShare): K线/资金流/新闻/财务  │
         └─────────────────────┬───────────────────┘
                               ▼
         ┌─────────────────────────────────────────┐
         │  8 大量化策略 → 信号 + 综合评分            │
         └─────────────────────┬───────────────────┘
                               ▼
   ┌───────────────┬───────────────┬───────────────┐
   │ 技术分析师     │ 基本面分析师   │ 舆情分析师     │  (DeepSeek)
   └───────┬───────┴───────┬───────┴───────┬───────┘
           └───────────────┼───────────────┘
                           ▼
              🐂 多头研究员 ⇄ 🐻 空头研究员 (多轮辩论)
                           ▼
                    🛡️ 风险经理 (仓位/止损)
                           ▼
                 🎯 首席交易决策 (JSON 决策)
```

---

## 📊 内置策略一览

| 代号 | 名称 | 逻辑 |
|------|------|------|
| `ma_cross` | 均线金叉 | MA5 上穿/下穿 MA20 |
| `macd` | MACD | DIF 与 DEA 金叉死叉 |
| `kdj` | KDJ超买超卖 | 低位金叉买入，高位死叉卖出 |
| `boll` | 布林带回归 | 触下轨买，触上轨卖 |
| `trend` | 多头趋势 | 均线多头排列持有 |
| `rsi` | RSI反转 | 超卖买入，超买卖出 |
| `breakout` | 高点突破 | 唐奇安通道突破 |
| `vol_break` | 放量突破 | 放量上穿 MA20 |
| `chan` | 缠论分型 | 顶/底分型构成的笔买卖点 |
| `grid` | 网格交易 | 区间内分档低吸高抛（连续仓位） |

---

## 📋 盘后复盘（高度自定义）

复盘完全由 `config/review.json` 驱动，可自由增删模式、调整顺序、配置参数：

```json
{
  "title": "每日盘后复盘",
  "enabled_modes": ["market", "breadth", "hotspot", "watchlist", "holdings", "ai_summary"],
  "use_llm_for": ["market", "ai_summary"],
  "mode_options": {
    "hotspot": { "top_n": 8 },
    "ai_summary": { "custom_prompt": "请作为首席策略师总结今日盘面与明日策略..." }
  }
}
```

| 模式 | 说明 |
|------|------|
| `market` | 大盘指数涨跌（可选 AI 点评）|
| `breadth` | 市场广度：涨跌家数、涨停跌停、情绪 |
| `hotspot` | 热点板块涨幅榜 |
| `watchlist` | 自选股表现 + 综合信号 |
| `holdings` | 持仓盈亏 |
| `ai_summary` | AI 综合总结（自定义 prompt）|

- **自选股/持仓**：编辑 `config/watchlist.json`
- **新增复盘模式**：在 `review/modes.py` 继承 `ReviewMode` 并 `@register_mode`，即可在配置中按 name 启用

---

## 🔧 二次开发

- **新增策略**：在 `strategies/library.py` 继承 `Strategy`，实现 `generate_positions` 与 `current_signal`，
  加入 `_STRATEGY_CLASSES` 即可自动注册到回测、选股、Web。
- **新增 Agent**：在 `agents/` 下继承 `Agent`，在 `orchestrator.py` 中接入流程。
- **新增复盘模式**：在 `review/modes.py` 继承 `ReviewMode` 并 `@register_mode`。
- **新增推送渠道**：在 `notify/channels.py` 继承 `Notifier`，加入 `ALL_CHANNELS`。
- **启用 Tushare**：在 `.env` 填入 `TUSHARE_TOKEN`，系统自动优先使用并降级容错。
- **自定义选股池**：修改 `report/selector.py` 的 `DEFAULT_POOL`。

---

## 📌 致谢

- 多智能体架构灵感：[TradingAgents](https://github.com/TauricResearch/TradingAgents)、[TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN)
- 多策略思想参考：[daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)
- 数据源：[AkShare](https://akshare.akshare.xyz/)
- 大模型：[DeepSeek](https://www.deepseek.com/)
