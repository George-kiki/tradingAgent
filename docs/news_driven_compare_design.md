# 热点驱动模块 —— 日期筛选与多日组合分析 技术方案

## 1. 需求概述

在现有热点驱动模块（每12小时自动扫描）基础上，新增**多日数据比对分析**能力：

### 1.1 核心功能

| 功能 | 说明 |
|------|------|
| 日期筛选 | 日期选择器，默认近三日，支持自定义范围 |
| 多日勾选 | 复选框选择 2~N 天数据 |
| 趋势对比 | 板块热度走势、评分变化 |
| 差异分析 | 共性板块 vs 特异板块 |
| 可视化 | 折线图 + 柱状图 + 热力图 |
| 多日总结 | 规则引擎兜底 + LLM 增强 |
| 导出 PNG | 对比分析结果一键导出 |
| 自定义范围 | 突破 3 天限制，可选历史任意时段 |

### 1.2 功能分层

```
Layer 1 (基础):  日期筛选 + 多日勾选 + 结果展示          ← 必需
Layer 2 (可视化): 折线图 + 柱状图 + 热力图               ← 必需
Layer 3 (总结):   规则引擎 + LLM 增强                    ← 必需
Layer 4 (导出):   对比分析 PNG 导出                      ← 必需
Layer 5 (范围):   自定义日期范围（历史全量）              ← 必需
```

---

## 2. 现有架构回顾

```
数据存储: SQLite news_analysis 表
├── id, scan_time, news_count, filtered_count
├── sectors_json  → [{sector, bullish_score, catalyst, stocks, ...}]
├── filtered_news_json → [{title, source, value, related_sectors, ...}]
└── 每12h一次扫描，7天自动清理

已有API:
├── /api/news-driven          → 最新分析 (秒级)
├── /api/news-driven/history  → 历史摘要列表 [{id, scan_time, ...}]
├── /api/news-driven/get?id=  → 单条详情
└── /api/news-driven/cleanup  → 手动清理

前端现状: 仅"读取最新"/"强制扫描"两个按钮，无历史浏览UI
```

---

## 3. 方案设计

### 3.1 整体架构

```
┌──────────────────────────────────────────────────────────────┐
│                          前端 UI                              │
│  ┌─ 模式切换 ──┬── 默认(近3日) ──┬── 自定义范围 ─────────┐   │
│  │  [📅 近三日]  [📅 自定义范围]  [📥 导出PNG]            │   │
│  ├──────────────────────────────────────────────────────────┤   │
│  │  日期勾选区 ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐        │   │
│  │            │☑ 6/28│ │☑ 6/27│ │☑ 6/26│ │☐ 6/25│ ...     │   │
│  │            └──────┘ └──────┘ └──────┘ └──────┘        │   │
│  │                     [📊 开始对比分析]                    │   │
│  ├──────────────────────────────────────────────────────────┤   │
│  │  📊 板块热度走势(折线)    │  🔥 板块热度热力图          │   │
│  │  ┌──────────────────┐    │  ┌──────────────────────┐    │   │
│  │  │ ECharts 折线图   │    │  │ ECharts 热力图       │    │   │
│  │  └──────────────────┘    │  │ (日×板块 色温矩阵)   │    │   │
│  │                          │  └──────────────────────┘    │   │
│  │  📊 核心指标对比(柱状)    │                              │   │
│  │  ┌──────────────────┐    │  ┌──────────┬───────────┐    │   │
│  │  │ ECharts 柱状图   │    │  │🌐共性板块│⚡特异板块 │    │   │
│  │  └──────────────────┘    │  └──────────┴───────────┘    │   │
│  │                          │                              │   │
│  │  📝 多日总结 (规则引擎 + 🧠 LLM增强)                    │   │
│  │  ┌──────────────────────────────────────────────────┐    │   │
│  │  │ 趋势演变 + 核心洞察 + 风险提示                    │    │   │
│  │  └──────────────────────────────────────────────────┘    │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────┬───────────────────────────────────┘
                           │ GET /api/news-driven/dates?range=all
                           │ GET /api/news-driven/compare?ids=...
                           │ GET /api/news-driven/compare-llm?ids=...
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                        后端 API                               │
│  /api/news-driven/dates         → 可用日期列表 (支持 range)  │
│  /api/news-driven/compare       → 交叉分析 (纯规则)          │
│  /api/news-driven/compare-llm   → 交叉分析 (LLM 增强总结)    │
│  /api/news-driven/compare-png   → 导出对比分析 PNG            │
└──────────────────────────────────────────────────────────────┘
```

### 3.2 日期范围策略

```
┌─────────────────────────────────────────────────────┐
│  默认模式 (近3日)                                    │
│  ├── 硬性限定最近 3 个自然日                         │
│  ├── 无数据的日期灰色禁用 🔒                         │
│  └── 最少勾选 2 天                                   │
├─────────────────────────────────────────────────────┤
│  自定义模式 (历史全量)                               │
│  ├── 调用 /api/news-driven/dates?range=all           │
│  ├── 返回所有历史可用日期（最多 7 天，受清理限制）    │
│  ├── 至少勾选 2 天、最多 7 天                        │
│  └── 显示"数据保留期 7 天，超出将自动清理"提示        │
└─────────────────────────────────────────────────────┘
```

---

## 4. 后端设计

### 4.1 API: `GET /api/news-driven/dates?range=3d|all`

```python
@app.get("/api/news-driven/dates")
def api_news_driven_dates(range: str = Query("3d", description="日期范围: 3d=近三日, all=全部历史")):
    """返回可用日期列表。range=3d 只取近3日；range=all 返回全部历史（受7天清理限制）。"""
```

**输出**:
```json
{
  "range": "3d",
  "max_retention_days": 7,
  "dates": [
    {"id": 5, "date": "06-28", "scan_time": "2026-06-28 14:31", "news_count": 173, "sector_count": 8},
    {"id": 3, "date": "06-27", "scan_time": "2026-06-27 08:05", "news_count": 152, "sector_count": 7},
    {"id": 1, "date": "06-26", "scan_time": "2026-06-26 20:01", "news_count": 168, "sector_count": 6}
  ]
}
```

### 4.2 API: `GET /api/news-driven/compare?ids=1,3,5`

核心多日对比分析接口（纯规则引擎，秒级）。

```python
@app.get("/api/news-driven/compare")
def api_news_driven_compare(ids: str = Query(..., description="逗号分隔ID，2-7个")):
```

**输出结构**:

```json
{
  "days_analyzed": 3,
  "id_range": [1, 3, 5],
  "days": [
    {
      "id": 5, "scan_time": "06-28 14:31",
      "news_count": 173, "filtered_count": 45, "sector_count": 8,
      "top_sectors": [
        {"name": "光伏储能", "score": 92, "type": "行业"},
        {"name": "半导体",   "score": 88, "type": "行业"}
      ]
    }
  ],

  "trends": {
    "sectors": [
      {"name": "光伏储能",   "scores": [85, 90, 92], "dates": ["06-26","06-27","06-28"]},
      {"name": "半导体",     "scores": [92, 85, 88], "dates": ["06-26","06-27","06-28"]},
      {"name": "新能源汽车",  "scores": [78, 82, 85], "dates": ["06-26","06-27","06-28"]},
      {"name": "人工智能",   "scores": [75, 88, 80], "dates": ["06-26","06-27","06-28"]}
    ],
    "metrics": {
      "total_news":    [168, 152, 173],
      "sector_counts": [6,   7,   8],
      "avg_score":     [76,  82,  85]
    }
  },

  "heatmap": {
    "dates": ["06-26", "06-27", "06-28"],
    "sectors": ["光伏储能","半导体","新能源车","AI","军工","医药"],
    "data": [
      [85, 92, 78, 75,  0, 70],    // 06-26 各板块评分 (0=不存在)
      [90, 85, 82, 88, 85,  0],
      [92, 88, 85, 80,  0, 72]
    ]
  },

  "comparison": {
    "common_sectors": [
      {"name": "半导体", "appearances": 3, "avg_score": 88.3, "trend": "震荡走强", "scores": [92,85,88]}
    ],
    "hot_common": [
      {"name": "光伏储能", "scores": [85, 90, 92], "trend": "↑连续升温"}
    ],
    "cooling_common": [
      {"name": "人工智能", "scores": [75, 88, 80], "trend": "↓先扬后抑"}
    ],
    "unique_per_day": {
      "06-28": [{"name": "新型储能", "score": 85}],
      "06-27": [{"name": "军工电子", "score": 82}],
      "06-26": [{"name": "消费电子", "score": 78}]
    },
    "common_keywords": ["清洁能源", "AI算力", "半导体"],
    "divergence_keywords": ["军工", "新型储能"]
  },

  "summary": {
    "text": "近三日热点呈现「新能源为主线、半导体反复活跃」格局。光伏储能连续3日评分攀升(85→90→92)...",
    "highlights": [
      "光伏储能连续3日评分上升，政策催化明确",
      "半导体板块虽偶有波动但始终在TOP3之内"
    ],
    "risk_notes": ["军工电子(06-27)为单日特异板块，持续性存疑"],
    "generated_by": "rule_engine"
  }
}
```

### 4.3 API: `GET /api/news-driven/compare-llm?ids=1,3,5`

LLM 增强版总结。复用 `/compare` 的全部数据，仅将总结部分替换为 LLM 生成。

```python
@app.get("/api/news-driven/compare-llm")
def api_news_driven_compare_llm(ids: str = Query(...)):
    """多日对比分析 + LLM 增强总结。适用于自定义范围（>3天）或需要深度洞察的场景。"""
    # 1. 先走纯规则拿到 trends + comparison
    base = compare_analyses(ids)
    # 2. LLM 重写总结
    llm_summary = _llm_enhance_summary(base["trends"], base["comparison"])
    base["summary"] = llm_summary
    base["summary"]["generated_by"] = "llm"
    return base
```

**LLM Prompt 设计**（轻量、结构化）:

```
你是一位资深宏观策略分析师。请基于以下多日热点扫描数据，生成一段200字以内的总结。

要求：
1. 概括主线演变趋势（哪条主线在强化/弱化）
2. 指出关键变化（新出现的板块、退出的板块）
3. 风险提示（单日特异板块、评分骤降板块）
4. 输出格式：纯文本（不要markdown），分3段

数据：
{序列化的 trends + comparison}
```

**LLM 调用策略**:
- 仅在用户主动点击"🧠 AI深度总结"时触发（避免自动消耗 token）
- 超时 30 秒，失败时降级到规则引擎结果
- 结果缓存 1 小时（同组 ids 不重复调用）

### 4.4 API: `POST /api/news-driven/compare-png`

导出对比分析结果为 PNG 图片。

```python
@app.post("/api/news-driven/compare-png")
def api_news_driven_compare_png(payload: dict = Body(...)):
    """将对比分析数据渲染为 HTML 卡片 → 截图 → 返回 PNG。
    
    Body: { ids, days, trends, heatmap, comparison, summary }
    前端传入已渲染的数据，后端负责 HTML→PNG 转换。
    """
    html = _build_compare_html(payload)  # 构建精美卡片 HTML
    png_path = _html_to_image(html)      # 复用已有的 playwright 截图
    return Response(content=open(png_path,'rb').read(), media_type="image/png",
                    headers={"Content-Disposition": "attachment; filename=hot-compare.png"})
```

**PNG 卡片布局**（单页纵向，适合分享）:

```
┌─────────────────────────────┐
│  🌍 热点驱动 · 多日对比分析  │
│  2026-06-26 → 06-28 (3天)   │
├─────────────────────────────┤
│  日概览                      │
│  06/26  168条·6板块·均分76   │
│  06/27  152条·7板块·均分82   │
│  06/28  173条·8板块·均分85   │
├─────────────────────────────┤
│  🔥 板块热度走势 (折线图)     │
│  [ASCII 表格近似·实际为截图]  │
├─────────────────────────────┤
│  🌐 共性板块  │ ⚡ 特异板块  │
│  光伏 89   │ 06-26 消费电子 │
│  半导体 88  │ 06-27 军工     │
│  AI 81      │ 06-28 新型储能 │
├─────────────────────────────┤
│  📝 总结                     │
│  近三日热点呈现「新能源为...  │
├─────────────────────────────┤
│  AI-Agent · 自动生成         │
└─────────────────────────────┘
```

### 4.5 交叉分析核心算法

在 `agents/news_orchestrator.py` 中新增：

```python
def get_recent_dates(days: int = 3) -> list[dict]:
    """获取近 N 日有预分析数据的日期列表。
    days=3: 近3日；days=0: 全部历史（受清理限制）。"""
    ...

def compare_analyses(ids: list[int]) -> dict:
    """多批预分析结果交叉比对。纯 Python，不调用 LLM。"""
    ...

def _generate_comparison_summary(trends, comparison, analyses) -> dict:
    """规则引擎自动生成总结。"""
    ...

def _llm_enhance_summary(trends, comparison) -> dict:
    """LLM 增强总结。需要 ENABLE_LLM=true。"""
    ...

def _build_heatmap_matrix(analyses: list) -> dict:
    """构建热力图数据矩阵。在 trends 中内嵌 heatmap 字段。"""
    ...
```

**热力图数据生成**:

```python
def _build_heatmap_matrix(analyses: list) -> dict:
    all_sectors = set()
    for a in analyses:
        for s in a.get("sectors", []):
            all_sectors.add(s["sector"])

    sectors = sorted(all_sectors, key=lambda n: max(
        s.get("bullish_score", 0) for a in analyses
        for s in a.get("sectors", []) if s["sector"] == n
    ), reverse=True)[:12]  # 最多12个板块

    dates = [a["scan_time"][:10] for a in analyses]
    data = []
    for a in analyses:
        row = []
        score_map = {s["sector"]: s.get("bullish_score", 0)
                      for s in a.get("sectors", [])}
        for sec in sectors:
            row.append(score_map.get(sec, 0))  # 0 = 不存在
        data.append(row)
    return {"dates": dates, "sectors": sectors, "data": data}
```

---

## 5. 前端设计

### 5.1 UI 布局

```
┌──────────────────────────────────────────────────────────┐
│  🌍 热点驱动                                              │
│  ┌──────────────────────────────────────────────────────┐ │
│  │ 现有：[读取最新分析] [强制重新扫描]                ← [保留]
│  ├──────────────────────────────────────────────────────┤ │
│  │ 📅 多日对比分析                               [新模块] │
│  │                                                        │
│  │ ┌─ 范围切换 ──────────────────────────────────────┐   │
│  │ │ ● 近三日(默认)   ○ 自定义范围                    │   │
│  │ └──────────────────────────────────────────────────┘   │
│  │                                                        │
│  │ ┌─ 日期勾选 ──────────────────────────────────────┐   │
│  │ │ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐   │   │
│  │ │ │☑ 6/28│ │☑ 6/27│ │☑ 6/26│ │☐ 6/25│ │🔒6/24│   │   │
│  │ │ │173条 │ │152条 │ │168条 │ │ 98条 │ │无数据│   │   │
│  │ │ └──────┘ └──────┘ └──────┘ └──────┘ └──────┘   │   │
│  │ └──────────────────────────────────────────────────┘   │
│  │                                                        │
│  │ [📊 规则对比]  [🧠 AI深度总结]  [📥 导出PNG]          │
│  │                                                        │
│  ├──────────────────────────────────────────────────────┤   │
│  │                                                        │
│  │  📊 板块热度走势(折线图)     🔥 板块热度热力图        │
│  │  ┌──────────────────┐      ┌──────────────────────┐   │
│  │  │  多板块评分趋势    │      │  日期 × 板块 色温矩阵  │   │
│  │  └──────────────────┘      └──────────────────────┘   │
│  │                                                        │
│  │  📊 核心指标对比(柱状图)                                │
│  │  ┌──────────────────────────────────────────────────┐   │
│  │  │  新闻总量 / 板块数 / 均分 分组柱状图               │   │
│  │  └──────────────────────────────────────────────────┘   │
│  │                                                        │
│  │  ┌──────────────┐  ┌──────────────┐  ┌────────────┐   │
│  │  │🌐 共性板块   │  │⚡ 特异板块   │  │🔑 关键词   │   │
│  │  │ 光伏储能 89  │  │6/28 新型储能 │  │共性: 清洁  │   │
│  │  │ 半导体 88.3  │  │6/27 军工电子 │  │能源·AI·   │   │
│  │  │ AI 81.0      │  │6/26 消费电子 │  │半导体      │   │
│  │  └──────────────┘  └──────────────┘  │分歧: 军工   │   │
│  │                                      └────────────┘   │
│  │  📝 总结 (规则引擎 / 🧠 LLM增强)                      │
│  │  ┌──────────────────────────────────────────────────┐   │
│  │  │ 近3日热点呈现「新能源为主线」格局。光伏储能连续    │   │
│  │  │ 3日评分攀升(85→90→92)，政策催化明确；半导体虽     │   │
│  │  │ 偶有波动但始终在TOP3之内...                       │   │
│  │  │ 生成方式: 规则引擎  [🧠 用AI重新生成]             │   │
│  │  └──────────────────────────────────────────────────┘   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

### 5.2 日期选择器组件

```html
<div class="date-picker-row" id="nd-date-picker">
  <!-- 动态渲染日期卡片 -->
</div>
```

**CSS 卡片样式**:
```css
.date-card { width: 80px; padding: 8px; border-radius: 8px;
             border: 2px solid #30363d; cursor: pointer; text-align: center; }
.date-card.checked { border-color: #58a6ff; background: rgba(88,166,255,.1); }
.date-card.disabled { opacity: 0.4; cursor: not-allowed; }
.date-card .check-icon { display: none; }
.date-card.checked .check-icon { display: block; color: #58a6ff; }
```

**交互规则**:
- 有数据日期：可勾选/取消，最少勾选 2 天
- 无数据日期：灰色禁用 + 🔒
- 近三日模式：仅展示最近 3 个自然日
- 自定义模式：展示所有历史可用日期（最多 7 天）
- 仅勾选 1 天时：分析按钮禁用 + tooltip"至少 2 天"

### 5.3 图表方案

| 图表 | 类型 | ECharts 配置 | 数据来源 |
|------|------|-------------|----------|
| 板块热度走势 | 多折线图 | `type: 'line'`, `smooth: true`, legend 交互筛选 | `trends.sectors[]` |
| 核心指标对比 | 分组柱状图 | `type: 'bar'`, 3 组(新闻/板块/均分) | `trends.metrics` |
| 热度热力图 | 热力图 | `type: 'heatmap'`, xAxis=日期 yAxis=板块 | `heatmap` |
| 板块分布 | 水平条形图 | `type: 'bar'`, `xAxis: {type: 'category'}` | `comparison.common_sectors` |

**热力图参数**:
```javascript
{
  tooltip: { formatter: p => `${p.data[1]} · ${p.data[0]}<br/>评分: ${p.data[2]}` },
  visualMap: {
    min: 0, max: 100,
    inRange: { color: ['#161b22','#0e4429','#006d32','#26a641','#39d353'] }
    //                     0分灰      低分绿      高分亮绿
  },
  xAxis: { data: dates, type: 'category' },
  yAxis: { data: sectors, type: 'category' },
  series: [{ type: 'heatmap', data: heatData }]
}
```

### 5.4 JavaScript 核心流程

```javascript
// ========== 页面加载 ==========
async function initComparePanel() {
    await loadDatePicker('3d');  // 默认近三日
}

// ========== 日期选择器 ==========
async function loadDatePicker(range = '3d') {
    const res = await fetch(`/api/news-driven/dates?range=${range}`);
    const { dates, max_retention_days } = await res.json();
    renderDateCards(dates);
    if (range === 'all') {
        showHint(`数据最多保留 ${max_retention_days} 天，超出将自动清理`);
    }
}

function switchRange(mode) {
    // '3d' 或 'all'
    loadDatePicker(mode);
    document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
    event.target.classList.add('active');
}

// ========== 规则分析 ==========
async function doCompare() {
    const ids = getSelectedIds();
    if (ids.length < 2) { showToast('请至少选择2天'); return; }

    showSpinner();
    const res = await fetch(`/api/news-driven/compare?ids=${ids.join(',')}`);
    const data = await res.json();
    hideSpinner();

    renderCompareResults(data);
    document.getElementById('btn-llm').style.display = 'inline-block';  // 显示AI总结按钮
    document.getElementById('btn-export').style.display = 'inline-block';
}

// ========== LLM 增强总结 ==========
async function doLLMCompare() {
    const ids = getSelectedIds();
    showSpinner('🧠 AI正在生成深度总结...');
    const res = await fetch(`/api/news-driven/compare-llm?ids=${ids.join(',')}`);
    const data = await res.json();
    hideSpinner();
    renderSummary(data.summary, 'llm');  // 仅刷新总结区域，图表不动
    showToast('✅ AI总结已生成');
}

// ========== 渲染 ==========
function renderCompareResults(data) {
    renderTrendChart(data.trends);          // ECharts 折线图
    renderHeatmap(data.heatmap);            // ECharts 热力图
    renderMetricChart(data.trends.metrics); // ECharts 柱状图
    renderCommonSectors(data.comparison);   // 共性板块
    renderUniqueSectors(data.comparison);   // 特异板块
    renderKeywords(data.comparison);        // 关键词
    renderSummary(data.summary);            // 总结文本
}

// ========== 导出 PNG ==========
async function exportComparePNG() {
    const data = getCurrentCompareData();  // 缓存当前分析结果
    const res = await fetch('/api/news-driven/compare-png', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    });
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `热点对比分析_${new Date().toISOString().slice(0,10)}.png`;
    a.click();
    URL.revokeObjectURL(url);
}
```

### 5.5 模式切换交互

```
┌──────────────────────────────────────────┐
│  ● 近三日(默认)      ○ 自定义范围        │
├──────────────────────────────────────────┤
│  近三日模式:                              │
│  · 硬限3天，日期卡片自动筛选              │
│  · 无明显提示                             │
├──────────────────────────────────────────┤
│  自定义模式:                              │
│  · 加载全部历史日期 (调用 range=all)       │
│  · 顶部提示 "数据最多保留7天"              │
│  · 分析耗时可能更长 (跨天数据量更大)      │
│  · 此时"🧠 AI深度总结"按钮自动高亮推荐   │
└──────────────────────────────────────────┘
```

---

## 6. 文件改动清单

| 文件 | 改动 | 行数 |
|------|------|------|
| `agents/news_orchestrator.py` | 🆕 `get_recent_dates()` + `compare_analyses()` + `_generate_comparison_summary()` + `_build_heatmap_matrix()` + `_llm_enhance_summary()` | +180 |
| `web/app.py` | 🆕 4 个端点: `/dates`, `/compare`, `/compare-llm`, `/compare-png` | +100 |
| `web/static/index.html` | ✏️ 多日对比子面板（模式切换 + 日期勾选 + 图表 + 总结 + 导出） | +300 |

**总计**: 3 个文件，约 **580 行**新增代码。

---

## 7. 对现有功能的影响

| 维度 | 评估 |
|------|------|
| 现有 API | ✅ 零修改，纯追加 |
| 现有 UI | ✅ 现有按钮不变，新增子面板 |
| 数据库 | ✅ 仅读 `news_analysis`，不写 |
| LLM 成本 | ✅ 默认走规则引擎(零成本)，LLM 总结需用户主动触发 |
| 新依赖 | ✅ 零新 Python 包；ECharts + playwright 已有 |

---

## 8. 边界情况

| 场景 | 处理 |
|------|------|
| 近三日无任何数据 | 显示"近三日暂无热点捕获数据"，日期区为空 |
| 仅 1 天有数据 | 禁用分析按钮，提示"至少需要2天数据" |
| 自定义范围选 0 天 | 按钮禁用 |
| 某天缺少某板块 | scores 用 `0` 占位，热力图显示灰色 |
| 跨天板块名不一致 | 不做模糊匹配，名不同即视为不同板块 |
| LLM 不可用 | "AI总结"按钮灰显 + tooltip "LLM未启用" |
| LLM 超时/报错 | 降级到规则引擎结果，toast 提示"AI总结失败，已使用规则引擎" |
| 数据被 7 天清理 | 自定义范围最多 7 天，超出自然不可选 |
| 导出 HTML→PNG 失败 | 弹 toast "导出失败，请确认 playwright 已安装" |

---

## 9. 实现步骤

| 步骤 | 内容 | 预估 |
|------|------|------|
| 1 | 后端：`news_orchestrator.py` 实现 `get_recent_dates()` + `compare_analyses()` | 核心算法 |
| 2 | 后端：实现 `_build_heatmap_matrix()` 热力图数据生成 | 配套 |
| 3 | 后端：实现 `_generate_comparison_summary()` 规则引擎总结 | 配套 |
| 4 | 后端：实现 `_llm_enhance_summary()` LLM 增强总结 | LLM 集成 |
| 5 | API：`app.py` 注册 `/dates` + `/compare` + `/compare-llm` + `/compare-png` | API 层 |
| 6 | 前端：日期选择器 HTML/CSS/JS（模式切换 + 勾选卡片） | 交互 |
| 7 | 前端：ECharts 折线图 + 柱状图 + 热力图配置 | 可视化 |
| 8 | 前端：共性/特异板块 + 关键词 + 总结文本渲染 | 结果展示 |
| 9 | 前端：PNG 导出按钮 + LLM 总结按钮集成 | 导出 |
| 10 | 联调测试：模拟多日数据验证全流程 | 测试 |
