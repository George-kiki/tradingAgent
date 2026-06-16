# -*- coding: utf-8 -*-
out = open("_rvout.txt", "w", encoding="utf-8")
def log(*a): print(*a, file=out)

from review.store import (save_review, list_reviews, get_review,
                          delete_review, reviews_by_dates, all_dates)

# 注入两天模拟复盘（含 metrics）
m1 = {"indices":[{"name":"上证指数","price":4050.1,"pct":-0.42}],
      "breadth":{"up":1556,"down":3882,"limit_up":75,"limit_down":66,"avg_pct":-0.8},
      "top_sectors":[{"name":"PCB/电子布","pct":5.2}]}
m2 = {"indices":[{"name":"上证指数","price":4091.9,"pct":0.76}],
      "breadth":{"up":2730,"down":2676,"limit_up":188,"limit_down":9,"avg_pct":0.5},
      "top_sectors":[{"name":"半导体","pct":6.8}]}
r1 = save_review("2026-06-15","每日盘后复盘","<html><body>D15</body></html>", m1)
r2 = save_review("2026-06-16","每日盘后复盘","<html><body>D16</body></html>", m2)
log("saved:", r1["id"], r1["date"], "|", r2["id"], r2["date"])

log("\n=== list_reviews ===")
for it in list_reviews():
    log(" ", it["date"], "上证", it["sh_index"], it["sh_pct"], "涨停", it["limit_up"], "跌停", it["limit_down"], "主线", it["top_sector"])

log("\n=== all_dates ===", all_dates())

log("\n=== get_review by id ===")
g = get_review(r2["id"])
log("  id命中:", g is not None, "html含D16:", "D16" in (g or {}).get("html",""))

log("\n=== reviews_by_dates(对比取数) ===")
recs = reviews_by_dates(["2026-06-15","2026-06-16"])
log("  取到", len(recs), "条，按升序:", [x["date"] for x in recs])

# 模拟 compare 接口的约束与序列构建
import datetime as dt
def compare(dates):
    sel=sorted(set(d.strip() for d in dates if d.strip()))
    if len(sel)<2: return {"error":"至少选择 2 个相邻工作日进行对比"}
    d0=dt.datetime.strptime(sel[0],"%Y-%m-%d").date(); d1=dt.datetime.strptime(sel[-1],"%Y-%m-%d").date()
    if (d1-d0).days>7: return {"error":"时间范围最长为最近一周"}
    rs=reviews_by_dates(sel)
    if len(rs)<2: return {"error":"可用复盘不足2天"}
    series={"limit_up":[],"limit_down":[]}
    for r in rs:
        b=(r.get("metrics") or {}).get("breadth") or {}
        series["limit_up"].append(b.get("limit_up")); series["limit_down"].append(b.get("limit_down"))
    return {"days":len(rs),"series":series,
            "lud":series["limit_up"][-1]-series["limit_up"][0],
            "ldd":series["limit_down"][-1]-series["limit_down"][0]}

log("\n=== compare 正常(2天) ===", compare(["2026-06-15","2026-06-16"]))
log("=== compare 仅1天(应报错) ===", compare(["2026-06-16"]))
log("=== compare 跨度>7天(应报错) ===", compare(["2026-06-05","2026-06-16"]))

log("\n=== delete ===")
log("  删除D15:", delete_review(r1["id"]), "| 剩余:", [x["date"] for x in list_reviews()])

# 清理测试数据，避免污染真实库
delete_review(r2["id"])
log("  已清理测试数据，剩余:", len(list_reviews()))

out.close()
print("DONE")
