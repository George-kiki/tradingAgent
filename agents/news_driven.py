"""热点驱动多Agent协作系统：过滤 → 多分析师 → 板块映射 → 个股筛选。

架构：
  ┌─────────────┐     ┌──────────────┐     ┌──────────────────┐
  │ NewsFetcher │────▶│ FilterAgent  │────▶│ Analyst Agents   │
  │ 全球热点抓取 │     │ 过滤噪音     │     │ (半导体/AI/新能源 │
  └─────────────┘     └──────────────┘     │  /消费电子/医药等)│
                                           └────────┬─────────┘
                                                    │
                                           ┌────────▼─────────┐
                                           │ Sector Mapper    │
                                           │ 板块→个股映射     │
                                           └────────┬─────────┘
                                                    │
                                           ┌────────▼─────────┐
                                           │ Result Aggregator│
                                           │ 汇总+排序+输出    │
                                           └──────────────────┘

输出结构：
  {
    "scan_time": "...",
    "news_count": 50,
    "filtered_count": 15,
    "sectors": [
      {
        "name": "半导体",
        "bullish_score": 85,
        "catalyst": "美光财报超预期，HBM需求爆发",
        "evidence": "Micron Q2营收同比+58%，HBM产能已售罄至2026年",
        "benefit_type": "直接受益",
        "stocks": [
          {"code":"688256","name":"寒武纪","reason":"AI芯片需求+HBM紧缺","score":82},
          ...
        ]
      },
      ...
    ]
  }
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any, Optional


# ── 分析师角色定义 ──
ANALYST_ROLES = [
    {
        "id": "semiconductor",
        "name": "半导体分析师",
        "sectors": ["半导体"],
        "system": "你是资深半导体行业分析师。擅长从新闻中识别对A股半导体产业链的利好信号，"
                  "包括设计、制造、封测、设备、材料、HBM/存储等子方向。"
                  "只输出JSON，不要其他文字。",
        "stock_pool": [
            ("688256", "寒武纪", "AI芯片设计"),
            ("002049", "紫光国微", "特种集成电路+存储"),
            ("688041", "海光信息", "CPU/DCU"),
            ("002371", "北方华创", "半导体设备龙头"),
            ("688012", "中微公司", "刻蚀设备"),
            ("300661", "圣邦股份", "模拟芯片设计"),
            ("688521", "芯原股份", "IP授权+Chiplet"),
            ("603501", "韦尔股份", "CIS芯片设计"),
            ("300142", "沃森生物", "疫苗(非半导体，移除)"),
            ("002916", "深南电路", "PCB+封装基板"),
        ],
    },
    {
        "id": "ai_computing",
        "name": "AI算力分析师",
        "sectors": ["AI算力"],
        "system": "你是AI算力基础设施分析师。关注GPU/服务器/液冷/IDC/连接器等方向。"
                  "只输出JSON，不要其他文字。",
        "stock_pool": [
            ("688256", "寒武纪", "AI训练芯片"),
            ("688041", "海光信息", "DCU加速卡"),
            ("300474", "景嘉微", "GPU"),
            ("000977", "浪潮信息", "AI服务器"),
            ("603019", "中科曙光", "算力系统"),
            ("000063", "中兴通讯", "服务器+网络"),
            ("002475", "立讯精密", "高速连接器"),
            ("300308", "中际旭创", "光模块(算力配套)"),
        ],
    },
    {
        "id": "optical",
        "name": "光通信分析师",
        "sectors": ["光通信"],
        "system": "你是光通信行业分析师。专注光模块/光芯片/光纤光缆/CPO等方向。"
                  "只输出JSON，不要其他文字。",
        "stock_pool": [
            ("300308", "中际旭创", "800G/1.6T光模块龙头"),
            ("300502", "新易盛", "光模块"),
            ("300394", "天孚通信", "光器件"),
            ("002281", "光迅科技", "光模块+光芯片"),
            ("601869", "长飞光纤", "光纤光缆"),
            ("600487", "亨通光电", "光纤光缆+海洋通信"),
            ("688048", "长华化学", "光芯片(移除)"),
            ("300620", "光库科技", "光器件"),
        ],
    },
    {
        "id": "new_energy",
        "name": "新能源分析师",
        "sectors": ["新能源"],
        "system": "你是新能源行业分析师。覆盖锂电/光伏/风电/储能/充电桩等方向。"
                  "只输出JSON，不要其他文字。",
        "stock_pool": [
            ("300750", "宁德时代", "动力+储能电池"),
            ("002594", "比亚迪", "新能源车+电池"),
            ("601012", "隆基绿能", "光伏组件"),
            ("300274", "阳光电源", "逆变器+储能"),
            ("600438", "通威股份", "硅料+电池片"),
            ("300014", "亿纬锂能", "锂电"),
            ("002460", "赣锋锂业", "锂盐"),
            ("603799", "华友钴业", "前驱体+镍钴"),
        ],
    },
    {
        "id": "consumer_electronics",
        "name": "消费电子分析师",
        "sectors": ["消费电子"],
        "system": "你是消费电子行业分析师。关注苹果产业链/折叠屏/VRAR/可穿戴等方向。"
                  "只输出JSON，不要其他文字。",
        "stock_pool": [
            ("002475", "立讯精密", "苹果链龙头"),
            ("002241", "歌尔股份", "声学+VRAR"),
            ("300433", "蓝思科技", "玻璃盖板"),
            ("002241", "歌尔股份", "声学(去重)"),
            ("300115", "长盈精密", "结构件"),
            ("002138", "顺络电子", "电感"),
            ("300782", "卓胜微", "射频前端"),
        ],
    },
    {
        "id": "pharma",
        "name": "医药分析师",
        "sectors": ["医药"],
        "system": "你是医药行业分析师。覆盖创新药/CXO/医疗器械/疫苗等方向。"
                  "只输出JSON，不要其他文字。",
        "stock_pool": [
            ("600276", "恒瑞医药", "创新药龙头"),
            ("603259", "药明康德", "CXO"),
            ("300015", "爱尔眼科", "眼科医疗服务"),
            ("300760", "迈瑞医疗", "医疗器械"),
            ("002422", "科伦药业", "ADC+大输液"),
            ("688185", "康希诺", "疫苗(非康希诺重复)"),
        ],
    },
    {
        "id": "robotics",
        "name": "机器人分析师",
        "sectors": ["机器人"],
        "system": "你是机器人行业分析师。关注人形机器人/减速器/伺服/丝杠等方向。"
                  "只输出JSON，不要其他文字。",
        "stock_pool": [
            ("300024", "机器人", "工业机器人"),
            ("300124", "汇川技术", "伺服+驱动"),
            ("002747", "埃斯顿", "工业机器人"),
            ("688017", "绿的谐波", "谐波减速器"),
            ("603728", "鸣志电器", "步进电机"),
            ("300083", "创世纪", "CNC(配套)"),
        ],
    },
]


# ── 过滤 Agent ──
FILTER_SYSTEM = (
    "你是金融新闻过滤专家。你的任务是从一批全球新闻中筛选出对A股投资有实际参考价值的资讯。"
    "剔除纯娱乐、体育、社会新闻、重复报道、无实质内容的标题党。"
    "保留：财报业绩、产业政策、技术突破、供应链变化、重大订单、价格异动、地缘政治影响等。"
    "只输出JSON，不要其他文字。"
)


def filter_news(news_list: list[dict], llm=None) -> list[dict]:
    """过滤Agent：从原始新闻中提取有价值的信息。

    Returns:
        [{"title","source","summary","keywords","value":"high/medium/low","reason":""}]
    """
    if not news_list:
        return []

    # 先用规则筛选保证重要新闻不丢（关键词引擎<0.1s）
    rule_filtered = _rule_filter(news_list)

    # 无LLM时直接返回规则结果
    if not llm or not llm.available:
        return rule_filtered

    # 强制保留规则判定为 high 的新闻（LLM不可过滤）
    must_keep = [n for n in rule_filtered if n.get("value") == "high"]
    # medium/low 交给 LLM 精筛
    for_llm = [n for n in rule_filtered if n.get("value") != "high"]

    # LLM在medium/low上做精筛
    news_for_llm = for_llm[:25]
    news_text = "\n".join(
        f"{i+1}. [{n.get('source','')}] {n.get('title','')}"
        for i, n in enumerate(news_for_llm)
    )

    prompt = f"""以下是今日全球财经新闻列表：

{news_text}

请筛选出对A股投资有参考价值的新闻（保留5-15条），输出JSON：

{{
  "filtered": [
    {{
      "index": 1,
      "value": "high",
      "reason": "美光财报超预期，HBM产能紧缺直接利好国产存储和AI芯片",
      "related_sectors": ["半导体", "AI算力"]
    }}
  ]
}}

value分三档：high（直接催化）/ medium（间接影响）/ low（宏观背景）
只输出JSON。"""

    try:
        resp = llm.chat(FILTER_SYSTEM, prompt, temperature=0.2, max_tokens=2000, json_mode=True)
        data = _safe_json(resp)
        if not data or not data.get("filtered"):
            return _rule_filter(news_list)

        results = list(must_keep)  # 先放high新闻
        for item in data["filtered"]:
            idx = int(item.get("index", 0)) - 1
            if 0 <= idx < len(news_for_llm):
                n = news_for_llm[idx]
                results.append({
                    "title": n.get("title", ""),
                    "source": n.get("source", ""),
                    "summary": n.get("summary", ""),
                    "keywords": n.get("keywords", []),
                    "value": item.get("value", "medium"),
                    "reason": item.get("reason", ""),
                    "related_sectors": item.get("related_sectors", []),
                    "published": n.get("published", ""),
                    "score": n.get("score", 0),
                    "hotness": n.get("hotness", 0),
                })
        # 按hotness排序
        results.sort(key=lambda x: x.get("hotness", 0), reverse=True)
        return results[:30]
    except Exception:
        return _rule_filter(news_list)


def _rule_filter(news_list: list[dict]) -> list[dict]:
    """关键词三语法筛选引擎（移植自trendradar）+ 热度权重算法。

    筛选语法：
      +必须词：标题/摘要必须包含（命中=1分）
      !过滤词：包含则直接剔除
      普通词：命中加分（0.5分/个）

    热度算法（移植自trendradar）：
      hotness = rank_weight(60%) + freq_weight(30%) + heat_weight(10%)
    """
    # ── 三语法关键词库 ──
    MUST_KW = [  # +必须词（命中任意一个即保留）
        "财报", "业绩", "超预期", "突破", "量产", "涨价", "订单", "扩产",
        "获批", "上市", "交付", "销量", "产能", "短缺", "紧缺", "制裁",
        "关税", "降息", "降准", "CPI", "PMI", "投资", "投向", "合作", "收购",
        "苹果", "特斯拉", "英伟达", "美光", "华为", "宁德", "比亚迪",
        "清洁能源", "新型能源", "能源体系", "电力", "电力投资",
        "半导体", "芯片", "存储", "HBM", "光模块",
        "AI", "GPU", "算力", "服务器", "电池", "光伏", "储能", "机器人",
        "FDA", "新药", "疫苗", "减肥药",
        "20万亿", "万亿", "新型能源", "能源体系",
    ]
    BLOCK_KW = [  # !过滤词（命中即剔除）
        "娱乐", "体育", "明星", "综艺", "八卦", "花边", "直播带货",
        "彩票", "游戏充值",
    ]
    SCORE_KW = {  # 普通加分词 → 权重
        "超预期": 3, "量产": 3, "涨价": 3, "短缺": 3, "紧缺": 3,
        "财报": 2, "业绩": 2, "突破": 2, "订单": 2, "扩产": 2,
        "获批": 2, "交付": 2, "产能": 2, "投资": 2, "合作": 2,
        "制裁": 2, "关税": 2, "降息": 2,
        "苹果": 2, "特斯拉": 2, "英伟达": 2, "美光": 2,
        "清洁能源": 5, "20万亿": 5, "电力投资": 4, "新型能源": 5, "能源体系": 5, "万亿": 4,
        "长鑫": 3, "存储": 2, "HBM": 3,
        "AI": 1, "芯片": 1, "半导体": 1, "光模块": 2,
        "电池": 1, "光伏": 1, "储能": 1, "机器人": 1,
    }

    results = []
    # 统计频次（用于热度算法）
    sector_freq: dict[str, int] = {}

    for n in news_list:
        text = (n.get("title", "") + " " + n.get("summary", "")).lower()

        # 1. !过滤词检查
        if any(kw.lower() in text for kw in BLOCK_KW):
            continue

        # 2. +必须词检查（至少命中1个才保留）
        must_hit = sum(1 for kw in MUST_KW if kw.lower() in text)
        if must_hit == 0:
            continue

        # 3. 计算得分
        score = sum(w for kw, w in SCORE_KW.items() if kw.lower() in text)
        score += must_hit * 0.5  # 必须词基础分

        # 4. 价值分级
        if score >= 8:
            value = "high"
        elif score >= 4:
            value = "medium"
        else:
            value = "low"

        # 5. 统计板块频次（热度算法用）
        for s in n.get("keywords", []):
            sector_freq[s] = sector_freq.get(s, 0) + 1

        results.append({
            "title": n.get("title", ""),
            "source": n.get("source", ""),
            "summary": n.get("summary", "")[:200],
            "keywords": n.get("keywords", []),
            "value": value,
            "score": round(score, 1),
            "reason": f"关键词得分{score:.1f}（必须词{must_hit}个命中）",
            "related_sectors": n.get("keywords", []),
            "published": n.get("published", ""),
        })

    # 6. 热度权重排序（trendradar算法）
    # rank_weight(60%): 来源质量分  +  freq_weight(30%): 同板块频次  +  heat_weight(10%): score
    SOURCE_RANK = {"华尔街见闻": 90, "东方财富": 85, "东方财富搜索": 80, "财联社": 80,
                   "Reuters": 75, "澎湃新闻": 60, "DuckDuckGo": 40, "抖音热点": 30}
    max_freq = max(sector_freq.values()) if sector_freq else 1
    max_score = max((r["score"] for r in results), default=1)

    for r in results:
        rank = SOURCE_RANK.get(r["source"], 50) / 100
        # 该新闻所属板块的最高频次
        freq = max((sector_freq.get(s, 0) for s in r.get("related_sectors", [])), default=0)
        freq_w = freq / max_freq if max_freq > 0 else 0
        heat_w = r["score"] / max_score if max_score > 0 else 0
        r["hotness"] = round(rank * 0.6 + freq_w * 0.3 + heat_w * 0.1, 3)

    results.sort(key=lambda x: x.get("hotness", 0), reverse=True)
    # 确保high价值新闻不被截断（即使来源权重低）
    high_items = [r for r in results if r.get("value") == "high"]
    other_items = [r for r in results if r.get("value") != "high"]
    return (high_items + other_items)[:30]


# ── 多分析师 Agent ──
def run_analysts(filtered_news: list[dict], llm=None) -> list[dict]:
    """多个分析师Agent并行分析，输出板块利好结果。

    Returns:
        [{"sector","bullish_score","catalyst","evidence","benefit_type","stocks":[...]}]
    """
    if not filtered_news:
        return []

    # 按related_sectors分组新闻
    sector_news: dict[str, list[dict]] = {}
    for n in filtered_news:
        for s in n.get("related_sectors", []) or n.get("keywords", []):
            sector_news.setdefault(s, []).append(n)

    # 每个分析师处理自己负责的板块
    results = []
    for role in ANALYST_ROLES:
        sector_name = role["sectors"][0]
        relevant = sector_news.get(sector_name, [])
        if not relevant:
            continue

        analysis = _analyze_with_role(role, relevant, llm)
        if analysis:
            results.append(analysis)

    # 未归入任何分析师板块的→通用宏观分析
    all_sectors_covered = {r["sectors"][0] for r in ANALYST_ROLES}
    uncovered = [n for n in filtered_news
                 if not any(s in all_sectors_covered for s in (n.get("related_sectors") or n.get("keywords") or []))]
    if uncovered:
        macro = _macro_analysis(uncovered, llm)
        if macro:
            results.append(macro)

    results.sort(key=lambda x: x.get("bullish_score", 0), reverse=True)
    return results


def _analyze_with_role(role: dict, news: list[dict], llm=None) -> Optional[dict]:
    """单个分析师Agent分析其负责板块的新闻。"""
    sector_name = role["sectors"][0]
    stock_pool = role.get("stock_pool", [])

    # 无LLM→规则分析
    if not llm or not llm.available:
        return _rule_analyze(sector_name, news, stock_pool)

    news_text = "\n".join(
        f"- [{n.get('value','?')}] {n.get('title','')} ({n.get('reason','')})"
        for n in news[:10]
    )
    pool_text = "；".join(f"{c}({n})-{desc}" for c, n, desc in stock_pool[:10])

    prompt = f"""你是{role['name']}。以下是近期{sector_name}相关新闻：

{news_text}

你的候选股票池：
{pool_text}

请分析这些新闻对{sector_name}板块的影响，输出JSON：

{{
  "sector": "{sector_name}",
  "bullish_score": 75,
  "catalyst": "核心催化剂一句话",
  "evidence": "关键证据（财报数据/政策内容/技术指标）",
  "benefit_type": "直接受益/间接受益/情绪面",
  "stocks": [
    {{"code":"688256","name":"寒武纪","reason":"具体受益逻辑","score":82}}
  ]
}}

bullish_score: 0-100（80+强烈看好，60-79中性偏多，<60偏空）
stocks最多5只，score 0-100
只输出JSON。"""

    try:
        resp = llm.chat(role["system"], prompt, temperature=0.3, max_tokens=2000, json_mode=True)
        data = _safe_json(resp)
        if not data:
            return _rule_analyze(sector_name, news, stock_pool)

        # 验证并补全
        data["sector"] = sector_name
        data["analyst"] = role["name"]
        data["news_count"] = len(news)
        data["news_titles"] = [n.get("title", "")[:50] for n in news[:5]]
        return data
    except Exception:
        return _rule_analyze(sector_name, news, stock_pool)


def _rule_analyze(sector: str, news: list[dict], stock_pool: list) -> dict:
    """规则兜底分析。"""
    high_count = sum(1 for n in news if n.get("value") == "high")
    med_count = sum(1 for n in news if n.get("value") == "medium")
    score = min(50 + high_count * 15 + med_count * 8, 95)

    catalysts = [n.get("title", "")[:60] for n in news[:3]]

    # 选前5只池内股票
    stocks = []
    for code, name, desc in stock_pool[:5]:
        stocks.append({
            "code": code, "name": name,
            "reason": f"{sector}板块利好，{desc}",
            "score": min(60 + high_count * 5, 90),
        })

    return {
        "sector": sector,
        "analyst": f"{sector}规则分析",
        "bullish_score": score,
        "catalyst": "；".join(catalysts)[:120],
        "evidence": f"规则匹配{len(news)}条相关新闻（高价值{high_count}条）",
        "benefit_type": "间接受益" if high_count == 0 else "直接受益",
        "stocks": stocks,
        "news_count": len(news),
        "news_titles": [n.get("title", "")[:50] for n in news[:5]],
    }


def _macro_analysis(news: list[dict], llm=None) -> Optional[dict]:
    """宏观/未归类新闻分析。"""
    if not news:
        return None
    titles = [n.get("title", "")[:60] for n in news[:5]]
    return {
        "sector": "宏观/其他",
        "analyst": "宏观分析师",
        "bullish_score": 50,
        "catalyst": "；".join(titles)[:120],
        "evidence": f"未归入特定板块的{len(news)}条新闻",
        "benefit_type": "情绪面",
        "stocks": [],
        "news_count": len(news),
        "news_titles": titles,
    }


# ── 工具函数 ──
def _safe_json(raw: str) -> Optional[dict]:
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    for strategy in ["direct", "extract_obj"]:
        try:
            if strategy == "direct":
                return json.loads(text)
            else:
                s = text.find("{")
                e = text.rfind("}")
                if s >= 0 and e > s:
                    return json.loads(text[s:e + 1])
        except (json.JSONDecodeError, ValueError):
            continue
    return None
