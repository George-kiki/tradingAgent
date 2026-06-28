# 📊 AI-Agent A股智能分析系统

一个面向**中国内地股市（A股）**的多智能体 + 多策略量化分析系统。融合了
[TradingAgents](https://github.com/TauricResearch/TradingAgents) 的「多空辩论」多智能体思想，
与 daily_stock_analysis 的「多交易策略」实践，使用 **AkShare** + **通达信/腾讯直连** 双数据源
与国产 **DeepSeek V4 Pro** 大模型。

> ⚠️ **免责声明**：本项目仅供学习与研究使用，**不构成任何投资建议**，不提供实盘交易功能。股市有风险，投资需谨慎。

---

## ✨ 核心功能

| 模块 | 说明 |
|------|------|
| 🤖 **多智能体分析** | 技术面 / 基本面 / 舆情分析师 → 多空研究员多轮辩论 → 风控经理 → 首席交易决策 |
| 📈 **多交易策略** | 内置 **10 种策略**：均线金叉、MACD、KDJ、布林带、多头趋势、RSI、高点突破、放量突破、缠论分型、网格交易 |
| 🔬 **策略回测** | 向量化回测引擎，T+1 成交、含手续费/印花税，输出收益/回撤/夏普/胜率 |
| 📊 **实时分析** | MA均线破位检测 + 主力资金流 + 缩量/放量判断 + 情绪温度计六维评分 + K线迷你图 |
| 🎯 **量化选股** | 对股票池用全部策略综合打分排序，输出每日选股 |
| 📌 **每日荐股 + 反思迭代** | 基于前一日数据推荐 5 只（过滤科创板/创业板），结果落本地 SQLite；昨日胜率<70% 自动反思迭代 |
| 🕝 **尾盘荐股** | 14:30 盘中推荐，今日尾盘买入、次日早盘验证（午后动量+VWAP+抢筹+AI协同） |
| 🧠 **复盘哥选股** | 情绪周期(天时) + 题材主线(人和) + 龙头身位/K线形态(地利)，三维共振精选 |
| 🌍 **热点驱动** | 全球热点新闻多渠道抓取 → 过滤Agent → 多分析师协作 → 板块利好映射；**多日对比分析**（折线图/柱状图/热力图/LLM总结/PNG导出） |
| 💎 **价值挖掘** | 五步智能体：瓶颈拆解 → 锁定标的 → 财务穿透 → 红队证伪 → 熔断机制 |
| 📋 **盘后复盘** | 多种可自定义复盘模式（大盘/市场广度/热点板块/自选股/持仓/AI总结） |
| 📉 **情绪温度计** | 六维加权：主力资金(20%)+相对强弱(20%)+参与热度(15%)+趋势(20%)+回撤修复(15%)+下行风险(10%) |
| 📧 **邮箱提醒** | 热点捕获前后自动发送预告/完成通知邮件 |
| 🔔 **定时推送** | 多任务定时调度（选股/荐股/复盘/热点扫描），支持企业微信/飞书/邮箱/控制台 |
| 📉 **K线可视化** | Web 端 ECharts 蜡烛图 + 均线 + 成交量 + MACD 副图 |
| 🗄️ **多数据源** | A-Stock直连(通达信/腾讯/东财) → Tushare → AkShare → 新浪，自动降级容错 |
| 🌐 **Web 界面** | FastAPI + 单页前端，13 个功能面板，支持 PNG 导出 |

---

## 🏗️ 项目结构

```
tradingAgent/
├── core/              # 配置 + 技术指标库（纯 pandas 实现）
├── data/              # 数据采集（A-Stock直连 + Tushare + AkShare + 新浪）+ 缓存
│   ├── fetcher.py     # 统一入口（K线/资金流/北向/新闻/市场广度/板块）
│   ├── astock_source.py  # A-Stock 直连（通达信mootdx/腾讯/东财，不封IP）
│   ├── tushare_source.py # Tushare 增强源
│   └── cache.py       # 本地缓存（含索引管理）
├── strategies/        # 10 种交易策略（可回测+实时信号）
├── backtest/          # 向量化回测引擎与绩效评估
├── agents/            # 多智能体（DeepSeek 驱动）
│   ├── analysts.py / researchers.py / risk_manager.py / trader.py
│   ├── orchestrator.py    # 智能分析编排
│   ├── judges.py / valuation.py  # 评委/DCF估值
│   ├── financial_dissect.py     # 杜邦ROIC财务穿透
│   ├── bottleneck_audit.py      # 三问瓶颈审计
│   ├── risk_falsify.py          # 18项风险清单
│   ├── sentiment_thermometer.py # 情绪温度计六维评分
│   ├── news_fetcher.py          # 全球新闻多渠道抓取
│   ├── news_driven.py           # 过滤Agent + 多分析师
│   ├── news_orchestrator.py     # 热点驱动编排 + 多日对比
│   └── mail_notify.py           # 邮箱提醒模块
├── review/            # 盘后复盘（可插拔多模式）
├── recommend/         # 每日荐股 + 反思迭代
│   ├── database.py / winrate.py / reflection.py / engine.py
│   ├── fundamentals.py / sentiment.py / llm_pipeline.py
│   └── fugupan_stock.py   # 复盘哥三维共振选股
├── tailpick/          # 尾盘选股
│   ├── tail_engine.py      # v2: 午后动量+VWAP+抢筹
│   └── ai_tail_engine.py   # v3: LLM+ML四层AI协同
├── value/             # 价值挖掘（五步智能体）
├── config/            # 用户配置（自选股/持仓/复盘模式）
├── schedulers/        # 定时调度（多任务常驻进程）
├── notify/            # 消息推送（控制台/企业微信/飞书/邮箱）
├── web/               # FastAPI + Web 前端
│   ├── app.py         # API 路由（20+ 端点）
│   └── static/index.html  # 单页前端（13个面板 + ECharts）
├── docs/              # 技术方案文档
├── main.py            # CLI 入口
├── scheduler.py       # 定时调度器
└── requirements.txt
```

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

> 建议 Python 3.10+。首次运行会联网获取数据。

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，填入 DeepSeek API Key：

```
DEEPSEEK_API_KEY=sk-你的key
ENABLE_LLM=true
```

> 💡 **不配置 LLM 也能用**：将 `ENABLE_LLM=false`，系统自动降级为纯量化模式，不消耗任何 token。

### 3. 使用方式

**命令行：**

```bash
python main.py strategies                    # 查看所有策略
python main.py analyze 600519                # 多智能体分析
python main.py backtest 600519 --all         # 全部策略回测
python main.py recommend                     # 每日荐股 + 反思迭代
python main.py tail-recommend                # 尾盘荐股（14:30）
python main.py value 光伏胶膜                 # 价值挖掘五步分析
python main.py select --top 10               # 量化选股 Top10
python main.py review                        # 生成盘后复盘
python main.py news-driven                   # 手动触发热点扫描
python main.py web                           # 启动 Web 服务
python main.py schedule                      # 启动定时调度（常驻）
```

**Web 界面：**

```bash
python main.py web
# 浏览器打开 http://127.0.0.1:8000
# 13 个面板：智能分析 / 实时分析 / K线图表 / 策略回测 / 量化选股 /
#           自定义选股 / 价值挖掘 / 每日荐股 / 尾盘荐股 / 尾盘选股 /
#           复盘哥 / 热点驱动 / 盘后复盘
```

### 4. 定时调度

```bash
python main.py schedule
```

内置任务：

| 任务 | 时间 | 频率 |
|------|------|------|
| 每日选股推送 | 09:00 | 工作日 |
| 🕝 尾盘荐股 | 14:30 | 工作日 |
| 盘后复盘 | 15:30 | 工作日 |
| 每日荐股（含反思） | 18:30 | 工作日 |
| 🌍 热点驱动扫描 | — | 每12小时 |

推送渠道配置见 `.env`。

---

## 🌍 热点驱动模块

全球热点新闻 → 板块利好映射，每12小时自动执行。

### 新闻来源

| 渠道 | 数量 | 说明 |
|------|------|------|
| 东方财富全球要闻 | 30条 | 国内财经头条 |
| 华尔街见闻快讯 | 100条 | 实时财经快讯 |
| 澎湃新闻 | 20条 | 综合资讯 |
| 抖音热点 | 8条 | 社交媒体热度 |
| 东财搜索 | 15条 | 关键词主动搜索 |
| 财联社 / Reuters | 降级处理 | 需登录或不可达时跳过 |

### 分析流程

```
多渠道抓取 → 关键词过滤Agent → 多分析师协作 → 板块利好映射 → 持久化预存
```

### 多日对比分析

| 功能 | 说明 |
|------|------|
| 日期选择 | 多选下拉框，近三日/自定义范围 |
| 趋势图表 | ECharts 折线图(板块评分走势) + 柱状图(指标对比) + 热力图(日×板块色温矩阵) |
| 交叉分析 | 共性板块 / 特异板块 / 关键词抽取 |
| 总结 | 规则引擎 + 🧠 LLM 增强（用户主动触发） |
| 导出 | 一键导出 PNG 图片 |

### 邮箱提醒

配置 `.env` 中 `MAIL_ENABLED=true` 及 SMTP 参数后，每次扫描前后自动发送邮件通知。

---

## 📉 情绪温度计

六维加权评分（0-100），判断个股偏冷/正常/过热：

| 维度 | 权重 | 说明 |
|------|------|------|
| 主力资金 | 20% | 大单/超大单流向，持续流入加分 |
| 相对强弱 | 20% | 个股涨幅 vs 基准指数涨幅 |
| 参与热度 | 15% | 量价配合，放量上涨加分 |
| 趋势 | 20% | MA20/MA60 均线多头排列 |
| 回撤修复 | 15% | 距阶段高点距离与修复程度 |
| 下行风险 | 10% | 近20日下跌波动与负收益占比 |

状态：❄️冰冷(<30) 🧊偏冷(30-44) 🌡️正常(45-64) 🔥偏热(65-79) 🌋过热(80+)

---

## 📌 每日荐股 + 反思迭代

一个会"自我复盘、自我进化"的荐股系统。每日基于**前一日市场数据**推荐 5 只股票，结果持久化到本地 SQLite；昨日胜率低于阈值（默认 70%）时自动诊断，反思结论融入今日选股。

**两阶段评分**：阶段一技术面初筛 → 阶段二多维度富化重排（估值/业绩/北向/龙虎榜/放量/利好）

**反思机制**：6 大反思维度（策略失效/追高风险/资金流背离/板块集中/市场情绪/基本面误判），自动调权加约束，闭环迭代。

---

## 💎 价值挖掘（五步智能体）

| 步骤 | 角色 | 产出 |
|------|------|------|
| ① 逆向拆解瓶颈 | 产业研究专家 | 识别高门槛/不可替代的物理瓶颈 |
| ② 锁定非对称标的 | 隐形冠军猎手 | 筛选中小盘 A股 |
| ③ 穿透财务拐点 | 财务侦探 | 杜邦ROIC + 毛利率趋势 |
| ④ AI 红队测试 | 空头分析师 | 三维证伪报告 |
| ⑤ 指定熔断机制 | 风控官 | 可证伪里程碑 |

> 新增：`financial_dissect`(杜邦ROIC评级)、`bottleneck_audit`(三问框架)、`risk_falsify`(18项风险清单)

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
| `grid` | 网格交易 | 区间内分档低吸高抛 |

---

## 📋 盘后复盘（高度自定义）

复盘完全由 `config/review.json` 驱动，支持 6 种模式：大盘/市场广度/热点板块/自选股/持仓/AI总结，可自由增删调整。

---

## 🔧 二次开发

- **新增策略**：在 `strategies/library.py` 继承 `Strategy`，实现 `generate_positions` 与 `current_signal`
- **新增 Agent**：在 `agents/` 下继承 `Agent`，在 `orchestrator.py` 中接入流程
- **新增复盘模式**：在 `review/modes.py` 继承 `ReviewMode` 并 `@register_mode`
- **新增推送渠道**：在 `notify/channels.py` 继承 `Notifier`，加入 `ALL_CHANNELS`
- **新增数据源**：在 `data/` 下实现适配器，在 `fetcher.py` 注册优先链
- **启用 Tushare**：在 `.env` 填入 `TUSHARE_TOKEN`

---

## 📌 致谢

- 多智能体架构灵感：[TradingAgents](https://github.com/TauricResearch/TradingAgents)
- 多策略思想参考：[daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)
- 数据源：[AkShare](https://akshare.akshare.xyz/) · [mootdx](https://github.com/bopo/mootdx) · 腾讯财经 · 东方财富
- 大模型：[DeepSeek](https://www.deepseek.com/)
