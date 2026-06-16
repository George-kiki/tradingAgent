# -*- coding: utf-8 -*-
import urllib.request, json
out = open("_httpout.txt", "w", encoding="utf-8")
def log(*a): print(*a, file=out)
BASE="http://127.0.0.1:8000"
def g(p):
    try:
        with urllib.request.urlopen(BASE+p, timeout=120) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except Exception as e:
        return -1, str(e)

# 1) 生成今日复盘（落库）
st, body = g("/api/review")
try: j=json.loads(body)
except: j={}
log("POST /api/review status:", st, "有html:", bool(j.get("html")), "record:", j.get("record"), "date:", j.get("date"))

# 2) 历史列表
st, body = g("/api/review/history")
j=json.loads(body)
items=j.get("items",[])
log("\n/api/review/history status:", st, "条数:", len(items))
for it in items[:3]:
    log("  ", it.get("date"), "上证", it.get("sh_index"), it.get("sh_pct"), "涨停", it.get("limit_up"), "跌停", it.get("limit_down"))

rid = items[0]["id"] if items else None
# 3) 详情
if rid:
    st, body = g("/api/review/get?id="+rid)
    j=json.loads(body)
    log("\n/api/review/get status:", st, "html长度:", len(j.get("html","")), "含h2:", "<h2>" in j.get("html",""))
    # 4) 导出
    st, body = g("/api/review/export?id="+rid)
    log("/api/review/export status:", st, "是HTML文档:", body.strip().startswith("<!DOCTYPE") or "<html" in body[:200], "长度:", len(body))

out.close()
print("DONE")
