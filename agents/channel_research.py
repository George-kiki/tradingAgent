"""产业链/渠道线索自动检索与交叉验证。

Agent 会自动从公开新闻、搜索结果、财务摘要中寻找类似“客户合作、订单排期、
物料拉货、产能规划、产业链卡位、财报科目验证”的线索。
注意：网络搜索/渠道信息默认按“待核验线索”处理，不作为确定事实。
"""
from __future__ import annotations

import html
import re
from typing import Optional
from urllib.parse import quote_plus


def _extract_tags(text: str) -> list[str]:
    tags = re.findall(r"[#＃]([\w\u4e00-\u9fa5A-Za-z0-9_.-]+)", text or "")
    keywords = re.findall(
        r"(光模块|CPO|NPO|AOC|DSP|PCB|硅光|芯片|订单|产能|备货|拉货|客户|合作|预付账款|存货|合同负债|出货|保供|扩产|海外|北美|马来西亚)",
        text or "",
    )
    seen, out = set(), []
    for t in tags + keywords:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:14]


def _clean_html(s: str) -> str:
    s = re.sub(r"<.*?>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _split_points(text: str) -> list[str]:
    lines = [x.strip(" \t　-•·") for x in re.split(r"[\r\n]+", text or "")]
    return [x for x in lines if x][:10]


def _search_web(query: str, limit: int = 5) -> list[dict]:
    """轻量公开搜索。失败时返回空，避免阻塞主流程。"""
    try:
        import requests
        url = "https://duckduckgo.com/html/?q=" + quote_plus(query)
        resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return []
        text = resp.text
        blocks = re.findall(r'<a[^>]+class="result__a"[^>]*>(.*?)</a>.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', text, re.S)
        out = []
        for title, snippet in blocks[:limit]:
            out.append({"title": _clean_html(title), "snippet": _clean_html(snippet), "source": "web_search"})
        return out
    except Exception:
        return []


def _collect_public_clues(fetcher, symbol: str, name: str = "", industry: Optional[str] = None) -> dict:
    news = []
    try:
        news = fetcher.get_news(symbol, limit=12) or []
    except Exception:
        news = []

    queries = []
    base = f"{name or symbol} {symbol}"
    queries.append(f"{base} 产业链 订单 产能 客户 合作")
    queries.append(f"{base} 拉货 备货 预付账款 存货 财报")
    if industry:
        queries.append(f"{base} {industry} 上游 下游 供应链")
    # 通用高景气硬件/AI产业链关键词，不代表事实，仅用于发现可能线索
    queries.append(f"{base} 光模块 DSP PCB 硅光 CPO AOC 客户")

    web_hits = []
    seen = set()
    for q in queries[:4]:
        for h in _search_web(q, limit=4):
            k = h.get("title")
            if k and k not in seen:
                seen.add(k)
                web_hits.append({**h, "query": q})
        if len(web_hits) >= 10:
            break

    lines = []
    if news:
        lines.append("【公司新闻】")
        for n in news[:12]:
            lines.append(f"- {n.get('time','')} {n.get('title','')}")
    if web_hits:
        lines.append("\n【公开搜索结果】")
        for h in web_hits[:10]:
            lines.append(f"- {h.get('title','')}：{h.get('snippet','')}")
    return {"news": news, "web_hits": web_hits, "text": "\n".join(lines)}


def analyze_channel_notes(symbol: str, name: str = "", notes: str = "", llm: Optional[object] = None) -> dict:
    """兼容旧入口：分析给定文本。"""
    notes = (notes or "").strip()
    if not notes:
        return {"available": False, "summary": "未发现产业链/渠道线索"}
    return _analyze_text(symbol, name, notes, llm=llm, source="manual")


def analyze_public_channel_research(fetcher, symbol: str, name: str = "", industry: Optional[str] = None,
                                    llm: Optional[object] = None) -> dict:
    """自动检索并分析公开产业链/渠道线索。"""
    clues = _collect_public_clues(fetcher, symbol, name=name, industry=industry)
    text = clues.get("text") or ""
    if not text.strip():
        return {
            "available": False,
            "summary": "未检索到可用的公开产业链/渠道线索",
            "sources": clues,
        }
    res = _analyze_text(symbol, name, text, llm=llm, source="auto_public")
    res["sources"] = clues
    res["source_mode"] = "自动公开检索"
    return res


def _analyze_text(symbol: str, name: str, text: str, llm: Optional[object], source: str) -> dict:
    text = (text or "").strip()[:8000]
    tags = _extract_tags(text)
    points = _split_points(text)
    llm_view = ""

    if llm is not None and getattr(llm, "available", False):
        try:
            prompt = (
                f"股票：{name}（{symbol}）\n"
                "以下是系统自动检索到的公开新闻/搜索线索，可能包含公告、新闻、研报摘要、论坛传闻或渠道信息。\n"
                "请像产业链研究员一样主动寻找类似“客户合作、订单排期、物料拉货、产能规划、财报科目验证”的逻辑，"
                "但必须严格区分事实、线索、推断，不得把未核验信息当成确定事实。\n\n"
                f"【检索材料】\n{text}\n\n"
                "请输出结构化分析，控制在 500 字内：\n"
                "1) 可能的产业链定位/客户合作/物料或产能线索；\n"
                "2) 证据链分层：公告/财报可验证、新闻/渠道待验证、推断性假设；\n"
                "3) 业绩传导路径：订单/客户/产能/物料如何传导收入利润；\n"
                "4) 关键核验清单：应查哪些公告、财报科目、客户/供应商/同行数据；\n"
                "5) 风险与反证：哪些情况会推翻该产业链逻辑。\n"
                "若材料不足，请明确写“未检索到足够证据”，不要硬编。"
            )
            llm_view = llm.chat(
                system=("你是A股产业链调研与财报交叉验证分析师，擅长主动从公开材料中挖掘订单、客户、产能、物料和财报验证线索。"
                        "必须审慎，区分事实/线索/推断/风险。"),
                user=prompt,
                max_tokens=1200,
            )
        except Exception as e:
            llm_view = f"[产业链线索分析失败: {e}]"

    summary = "；".join(points[:4]) if points else "系统已自动检索公开产业链/渠道线索，需进一步核验。"
    return {
        "available": True,
        "summary": summary,
        "raw": text,
        "tags": tags,
        "points": points,
        "llm_view": llm_view,
        "source": source,
        "risk_note": "该模块由系统自动检索公开信息并推演，含新闻/搜索/渠道类线索，默认未完成独立核验，仅供研究辅助，不构成投资建议。",
    }
