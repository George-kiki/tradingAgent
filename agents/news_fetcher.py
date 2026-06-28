"""多渠道全球热点新闻抓取模块（方案C优化版）。

经验证可用的数据源（无登录/无签名/无反爬封锁）：
  1. ✅ 东方财富全球要闻（akshare，30条/次，稳定）
  2. ✅ 华尔街见闻快讯API（100条/次，含"苹果长鑫"命中）
  3. ✅ 澎湃新闻热门（20条/次）
  4. ✅ DuckDuckGo搜索（关键词补充，含海外源）
  5. ⚠️ 财联社/新浪/金十/雪球 → 需登录Cookie或已失效，降级处理

输出统一为 list[dict]：
  {title, source, url, summary, published, region, keywords}
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import re
import time
from typing import Optional
from urllib.parse import quote_plus

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/json,application/xml,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _safe_get(url: str, timeout: int = 10, headers: dict = None) -> Optional[requests.Response]:
    try:
        import requests
        resp = requests.get(url, timeout=timeout, headers=headers or _HEADERS)
        return resp
    except Exception:
        return None


def _with_timeout(fn, seconds: int, **kwargs):
    """给抓取函数加超时保护（线程版，兼容所有平台）。"""
    import threading

    result = []
    error = []
    done = threading.Event()

    def _run():
        try:
            result.append(fn(**kwargs))
        except Exception as e:
            error.append(e)
        finally:
            done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    if done.wait(timeout=seconds):
        if error:
            raise error[0]
        return result[0] if result else []
    return []  # 超时返回空列表


def _clean_html(s: str) -> str:
    s = re.sub(r"<.*?>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_keywords(text: str) -> list[str]:
    kw_map = {
        "半导体": ["芯片", "半导体", "存储", "HBM", "光刻", "晶圆", "封测", "EDA", "美光", "英伟达", "台积电", "长鑫"],
        "AI算力": ["AI", "GPU", "算力", "服务器", "数据中心", "液冷", "OpenAI", "微软", "谷歌", "Meta"],
        "光通信": ["光模块", "CPO", "硅光", "光纤", "800G", "400G", "光通信"],
        "新能源": ["电池", "光伏", "风电", "储能", "锂电", "充电桩", "固态电池", "特斯拉", "比亚迪", "宁德", "清洁能源", "电力投资"],
        "消费电子": ["苹果", "iPhone", "华为", "折叠屏", "MR", "可穿戴", "Vision Pro"],
        "医药": ["FDA", "创新药", "GLP-1", "ADC", "疫苗", "医保", "减肥药"],
        "机器人": ["人形机器人", "Optimus", "减速器", "伺服", "谐波", "机器人"],
        "军工": ["军工", "国防", "航天", "导弹", "军贸"],
        "地产": ["房地产", "楼市", "保交楼", "城中村"],
        "金融": ["券商", "银行", "保险", "降息", "降准", "LPR"],
        "宏观": ["美联储", "CPI", "PMI", "非农", "关税", "制裁", "地缘", "20万亿"],
    }
    text_lower = (text or "").lower()
    found = []
    for sector, kws in kw_map.items():
        if any(kw.lower() in text_lower for kw in kws):
            found.append(sector)
    return found[:5]


# ────────────────── 各渠道抓取 ──────────────────

def fetch_eastmoney_global(limit: int = 30) -> list[dict]:
    """东方财富全球要闻（akshare，最稳定主源）。"""
    try:
        from data.fetcher import _require_ak
        _require_ak()
        import akshare as ak
        df = ak.stock_info_global_em()
        if df is None or df.empty:
            return []
        results = []
        for _, row in df.head(limit).iterrows():
            title = str(row.get("标题", "") or row.get("title", ""))
            content = str(row.get("内容", "") or row.get("content", ""))
            pub = str(row.get("发布时间", "") or row.get("datetime", ""))
            results.append({
                "title": title[:120],
                "source": "东方财富",
                "url": "",
                "summary": content[:300],
                "published": pub,
                "region": "全球",
                "keywords": _extract_keywords(title + content),
            })
        return results
    except Exception:
        return []


def fetch_wallstreetcn(limit: int = 100) -> list[dict]:
    """华尔街见闻快讯API（经验证最全面的财经快讯源）。

    成功捕获"苹果长鑫内存"等重大产业新闻。
    """
    try:
        import requests
        url = "https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=100"
        resp = requests.get(url, timeout=10, headers=_HEADERS)
        if resp.status_code != 200:
            return []
        data = resp.json()
        items = data.get("data", {}).get("items", [])
        results = []
        for item in items[:limit]:
            title = item.get("title", "") or ""
            content = item.get("content_text", "") or item.get("content", "")
            pub_ts = item.get("display_time", 0)
            pub = _dt.datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M") if pub_ts else ""
            uri = item.get("uri", "")
            url_str = f"https://wallstreetcn.com/news/live/{uri}" if uri else ""
            results.append({
                "title": title[:120] if title else content[:120],
                "source": "华尔街见闻",
                "url": url_str,
                "summary": content[:300],
                "published": pub,
                "region": "全球",
                "keywords": _extract_keywords(title + " " + content),
            })
        return results
    except Exception:
        return []


def fetch_thepaper(limit: int = 20) -> list[dict]:
    """澎湃新闻热门。"""
    try:
        import requests
        url = "https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar"
        resp = requests.get(url, timeout=8, headers=_HEADERS)
        if resp.status_code != 200:
            return []
        data = resp.json()
        items = data.get("data", {}).get("hotNews", [])
        results = []
        for item in items[:limit]:
            title = item.get("name", "") or item.get("title", "")
            pub = item.get("pubTimeLong", "")
            if pub:
                try:
                    pub = _dt.datetime.fromtimestamp(int(pub)).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass
            results.append({
                "title": title[:120],
                "source": "澎湃新闻",
                "url": "",
                "summary": "",
                "published": pub,
                "region": "国内",
                "keywords": _extract_keywords(title),
            })
        return results
    except Exception:
        return []


def fetch_douyin_hotspot(limit: int = 15) -> list[dict]:
    """抖音热点榜单（tophub聚合）。"""
    try:
        import requests
        url = "https://tophub.today/n/DpQvNABoNE"
        resp = requests.get(url, timeout=8, headers=_HEADERS)
        if resp.status_code != 200:
            return []
        # 解析热榜
        items = re.findall(r'<a[^>]*href="([^"]*)"[^>]*class="[^"]*t[^"]*"[^>]*>(.*?)</a>', resp.text, re.S)
        results = []
        for url_str, title_html in items[:limit]:
            title = _clean_html(title_html)
            if title and len(title) > 4:
                results.append({
                    "title": f"抖音热点: {title}",
                    "source": "抖音热点",
                    "url": url_str,
                    "summary": "",
                    "published": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "region": "国内",
                    "keywords": _extract_keywords(title),
                })
        return results
    except Exception:
        return []


def fetch_web_search_multi(limit: int = 10) -> list[dict]:
    """DuckDuckGo 多关键词搜索（海外源补充）。"""
    queries = [
        "美光 财报 HBM 存储",
        "英伟达 AI GPU 算力",
        "FDA 新药 批准 创新药",
        "美联储 CPI 降息",
    ]
    all_results = []
    for q in queries:
        results = _ddg_search(q, limit=2)
        all_results.extend(results)
        time.sleep(0.3)
    return all_results[:limit]


def fetch_eastmoney_search_multi(limit: int = 15) -> list[dict]:
    """东方财富资讯搜索API（国内新闻主动搜索，补充列表源遗漏）。

    独立渠道，避免被DuckDuckGo超时拖累。
    """
    # 搜索高频产业关键词，确保重要新闻不遗漏
    queries = [
        "20万亿 清洁能源",
        "苹果 长鑫存储",
        "新型能源体系",
        "半导体 突破",
        "AI 芯片 算力",
        "电池 固态 突破",
        "存储芯片 涨价",
    ]
    all_results = []
    for q in queries:
        results = _eastmoney_search(q, limit=3)
        all_results.extend(results)
        time.sleep(0.2)  # 礼貌延迟
    return all_results[:limit]


def _eastmoney_search(keyword: str, limit: int = 3) -> list[dict]:
    """东方财富资讯搜索API。"""
    try:
        import requests
        from urllib.parse import quote
        url = (f"https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery"
               f"&param=%7B%22uid%22%3A%22%22%2C%22keyword%22%3A%22{quote(keyword)}%22"
               f"%2C%22type%22%3A%5B%22cmsArticleWebOld%22%5D%2C%22client%22%3A%22web%22"
               f"%2C%22clientVersion%22%3A%22curr%22%2C%22param%22%3A%7B%22cmsArticleWebOld%22%3A"
               f"%7B%22searchScope%22%3A%22default%22%2C%22sort%22%3A%22default%22%2C"
               f"%22pageIndex%22%3A1%2C%22pageSize%22%3A{limit}%2C%22preTag%22%3A%22%22%2C%22postTag%22%3A%22%22%7D%7D%7D")
        resp = requests.get(url, timeout=8, headers={
            **_HEADERS, "Referer": "https://so.eastmoney.com/"})
        if resp.status_code != 200:
            return []
        import re, json
        m = re.search(r'\((\{.*\})\)', resp.text, re.S)
        if not m:
            return []
        data = json.loads(m.group(1))
        result = data.get("result", {})
        out = []
        for type_name, type_data in result.items():
            # type_data 可能是 list（新API）或 dict（旧API）
            if isinstance(type_data, list):
                items = type_data
            elif isinstance(type_data, dict):
                items = type_data.get("list", [])
            else:
                continue
            for item in items[:limit]:
                title = item.get("title", "").replace("<em>", "").replace("</em>", "")
                date = item.get("date", "")
                out.append({
                    "title": title[:120],
                    "source": "东方财富搜索",
                    "url": item.get("url", ""),
                    "summary": item.get("content", "")[:200],
                    "published": date,
                    "region": "国内",
                    "keywords": _extract_keywords(title + " " + keyword),
                })
        return out
    except Exception:
        return []


def _ddg_search(query: str, limit: int = 3) -> list[dict]:
    try:
        import requests
        url = "https://duckduckgo.com/html/?q=" + quote_plus(query)
        resp = requests.get(url, timeout=8, headers=_HEADERS)
        if resp.status_code != 200:
            return []
        blocks = re.findall(
            r'<a[^>]+class="result__a"[^>]*>(.*?)</a>.*?'
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            resp.text, re.S)
        out = []
        for title, snippet in blocks[:limit]:
            out.append({
                "title": _clean_html(title)[:120],
                "source": "DuckDuckGo",
                "url": "",
                "summary": _clean_html(snippet)[:300],
                "published": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "region": "全球",
                "keywords": _extract_keywords(title + snippet),
            })
        return out
    except Exception:
        return []


def fetch_cls_telegraph(limit: int = 20) -> list[dict]:
    """财联社电报（akshare，可能因API变更失败，降级处理）。"""
    try:
        from data.fetcher import _require_ak
        _require_ak()
        import akshare as ak
        df = ak.stock_info_global_cls()
        if df is None or df.empty:
            return []
        results = []
        for _, row in df.head(limit).iterrows():
            title = str(row.get("标题", "") or "")
            content = str(row.get("内容", "") or title)
            pub = str(row.get("发布时间", "") or "")
            results.append({
                "title": title[:120],
                "source": "财联社",
                "url": "",
                "summary": content[:300],
                "published": pub,
                "region": "国内",
                "keywords": _extract_keywords(title + content),
            })
        return results
    except Exception:
        return []


def fetch_reuters_rss(limit: int = 15) -> list[dict]:
    """Reuters RSS 海外财经。"""
    rss_url = "https://feeds.reuters.com/reuters/businessNews"
    try:
        import requests
        resp = requests.get(rss_url, timeout=8, headers=_HEADERS)
        if resp.status_code != 200:
            return []
        items = re.findall(r"<item>(.*?)</item>", resp.text, re.S)
        results = []
        for item_xml in items[:limit]:
            title_m = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", item_xml, re.S)
            desc_m = re.search(r"<description><!\[CDATA\[(.*?)\]\]></description>|<description>(.*?)</description>", item_xml, re.S)
            link_m = re.search(r"<link>(.*?)</link>", item_xml, re.S)
            pub_m = re.search(r"<pubDate>(.*?)</pubDate>", item_xml, re.S)
            title = _clean_html(title_m.group(1) or title_m.group(2) or "") if title_m else ""
            desc = _clean_html(desc_m.group(1) or desc_m.group(2) or "") if desc_m else ""
            link = link_m.group(1).strip() if link_m else ""
            pub = pub_m.group(1).strip() if pub_m else ""
            if title:
                results.append({
                    "title": title[:120],
                    "source": "Reuters",
                    "url": link,
                    "summary": desc[:300],
                    "published": pub,
                    "region": "海外",
                    "keywords": _extract_keywords(title + desc),
                })
        return results
    except Exception:
        return []


# ────────────────── 聚合入口 ──────────────────

def fetch_all_news() -> list[dict]:
    """聚合所有数据源，去重后返回最新新闻列表。"""
    all_news: list[dict] = []

    channels = [
        ("东方财富", fetch_eastmoney_global, 30),
        ("华尔街见闻", fetch_wallstreetcn, 100),
        ("澎湃新闻", fetch_thepaper, 20),
        ("财联社", fetch_cls_telegraph, 20),
        ("抖音热点", fetch_douyin_hotspot, 15),
        ("Reuters", fetch_reuters_rss, 15),
        ("东财搜索", fetch_eastmoney_search_multi, 15),
        # DuckDuckGo 在中国大陆DNS级被墙，_with_timeout 超时无法中断C层DNS调用
        # ("DuckDuckGo", fetch_web_search_multi, 10),
    ]

    import signal as _signal

    for name, fn, limit in channels:
        try:
            print(f"  [新闻抓取] {name}...", end="", flush=True)
            # 每个渠道最多15秒，超时则跳过
            results = _with_timeout(fn, 15, limit=limit)
            print(f" {len(results)}条")
            all_news.extend(results)
        except Exception as e:
            print(f" 失败: {str(e)[:50]}")

    # 去重
    seen = set()
    unique = []
    for n in all_news:
        key = n.get("title", "")[:30]
        if key and key not in seen:
            seen.add(key)
            unique.append(n)

    print(f"  [新闻抓取] 汇总: {len(all_news)}条 → 去重后 {len(unique)}条")
    return unique
