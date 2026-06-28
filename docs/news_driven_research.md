# 热点新闻抓取与分析系统 — 开源调研报告

## 一、调研发现的3个核心开源项目

### 1. ourongxing/newsnow ⭐20.8k（数据源层）
- **定位**：全网热点聚合的数据源底座（TypeScript）
- **平台覆盖**：35+平台（微博/抖音/知乎/B站/华尔街见闻/财联社/百度/澎湃/头条等）
- **核心价值**：**统一多平台数据格式 + 反爬处理 + 自适应缓存**
- **架构**：`shared/sources`(类型定义) + `server/sources`(各平台抓取实现)
- **缓存策略**：默认30分钟，自适应2分钟~30分钟
- **反爬**：优化资源使用防止IP封禁
- **部署**：Cloudflare Pages / Vercel / Docker
- **AI集成**：内置MCP Server支持
- **协议**：MIT

### 2. 776181/trendradar ⭐200+（分析层）
- **定位**：基于newsnow数据的AI热点分析雷达（Python）
- **平台**：依赖newsnow API，默认监控11个平台
- **核心价值**：**关键词筛选引擎 + 热度权重算法 + MCP AI分析**
- **热度算法**：排名权重60% + 频次权重30% + 热度权重10%
- **筛选语法**：普通词 + 必须词(+) + 过滤词(!)
- **AI分析**：13种MCP工具（趋势追踪/情感分析/相似检索/摘要生成）
- **推送**：企业微信/飞书/钉钉/Telegram/邮件/ntfy（6渠道）
- **部署**：Docker一行命令 / GitHub Fork零服务器
- **存储**：本地JSON + HTML + TXT（按日期分区）
- **协议**：GPL-3.0

### 3. jx-177/finance-inform ⭐4（财经垂直）
- **定位**：财经RSS聚合 + DeepSeek摘要推送（Python）
- **数据源**：华尔街见闻/36氪/东方财富/WSJ/BBC（5个RSS）
- **AI**：DeepSeek摘要（无情感分析，规划中）
- **部署**：GitHub Actions零成本
- **推送**：Server酱→微信
- **协议**：MIT

---

## 二、与我们现有方案的对比

| 维度 | 我们现有方案 | newsnow | trendradar | finance-inform |
|------|------------|---------|------------|----------------|
| **平台覆盖** | 7源（东财/财联/新浪/金十/抖音/Reuters/DDG） | **35+平台** | 11平台(依赖newsnow) | 5个RSS |
| **反爬处理** | UA伪装+超时 | **自适应缓存+防封禁** | 依赖newsnow | 无 |
| **数据格式统一** | 各源独立dict | **TypeScript类型驱动** | 统一JSON | RSS标准格式 |
| **筛选引擎** | LLM过滤+规则兜底 | 无（纯数据源） | **关键词三语法(+!空)** | LLM摘要 |
| **热度算法** | LLM bullish_score | 无 | **排名60%+频次30%+热度10%** | 无 |
| **AI分析** | 7个分析师Agent | MCP Server | **13种MCP工具** | DeepSeek摘要 |
| **存储** | SQLite双表 | Cloudflare D1 | **本地JSON按日期分区** | GitHub Actions产物 |
| **数据清理** | 7日清理>4天 | 缓存TTL | 无 | 无 |
| **推送** | Web UI | Web UI | **6渠道推送** | 微信(Server酱) |
| **部署** | 本地Python | Cloudflare/Vercel/Docker | **Docker一行** | GitHub Actions |
| **语言** | Python | TypeScript | Python | Python |

---

## 三、可借鉴的核心设计

### 从 newsnow 借鉴（数据源层）
1. **统一数据模型**：定义 `NewsItem` 标准结构，所有源输出统一格式
2. **自适应缓存**：不同源设置不同TTL（财联社2min，东方财富10min，Reuters30min）
3. **分层架构**：`sources/` 目录每个源一个文件，新增源只需加一个文件

### 从 trendradar 借鉴（分析层）
4. **关键词三语法筛选**：`+必须词` `!过滤词` `普通词`，比纯LLM更快更可控
5. **热度权重算法**：`排名60% + 频次30% + 热度10%`，量化热度而非纯定性
6. **按日期分区存储**：`output/YYYY-MM-DD/news_data.json`，天然支持清理
7. **6渠道推送**：企业微信/飞书/钉钉/Telegram/邮件/ntfy

### 从 finance-inform 借鉴（部署层）
8. **GitHub Actions零成本**：cron定时触发，无需服务器常驻

---

## 四、推荐的优化方案

### 方案A：集成 newsnow 作为数据源底座（推荐）
```
newsnow(API) → 我们的news_fetcher → filter+analysts → SQLite
```
- **优势**：直接获得35+平台覆盖 + 反爬处理 + 统一格式
- **成本**：部署newsnow（Docker一行命令）或调用其公开API
- **改动量**：news_fetcher.py 重写为调用newsnow API，其余不变

### 方案B：移植 trendradar 的筛选+热度算法
```
我们的news_fetcher → trendradar筛选引擎 → 热度算法 → 我们的analysts
```
- **优势**：关键词三语法比LLM过滤快100倍，热度算法可量化
- **成本**：移植筛选引擎(~200行) + 热度算法(~100行)
- **改动量**：news_driven.py 的 filter_news 替换为关键词引擎

### 方案C：全栈参考（A+B组合）
```
newsnow(35+平台) → 关键词筛选(+!语法) → 热度权重算法 → 7分析师Agent → SQLite → Web UI + 6渠道推送
```
- **优势**：兼具广度(35+平台) + 速度(关键词筛选) + 深度(分析师Agent) + 推送(6渠道)
- **成本**：最大改动量，但每层都是最优

---

## 五、建议优先级

| 优先级 | 改进项 | 理由 |
|--------|--------|------|
| P0 | 移植trendradar关键词筛选引擎 | LLM过滤慢(30s+)且不稳定，关键词引擎<1s |
| P0 | 移植trendradar热度权重算法 | 当前bullish_score纯LLM定性，缺乏量化 |
| P1 | 集成newsnow API扩展平台 | 7源→35+平台，覆盖面质变 |
| P1 | 按日期分区存储 | 替代SQLite单表，简化清理逻辑 |
| P2 | 增加多渠道推送 | 当前仅Web UI，增加飞书/钉钉推送 |
| P2 | GitHub Actions部署选项 | 零服务器成本定时执行 |

---

请确认方向，我再开始实施。
