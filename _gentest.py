# -*- coding: utf-8 -*-
out = open("_genout.txt", "w", encoding="utf-8")
def log(*a): print(*a, file=out)
import traceback, time
t0=time.time()
try:
    from review.html_report import generate_and_store
    res = generate_and_store(store=True)
    log("OK 耗时:%.1fs"%(time.time()-t0))
    log("html长度:", len(res.get("html","")))
    log("date:", res.get("date"))
    log("record:", res.get("record"))
    log("metrics.indices:", (res.get("metrics") or {}).get("indices"))
    log("metrics.breadth:", (res.get("metrics") or {}).get("breadth"))
    from review.store import list_reviews
    log("落库条数:", len(list_reviews()))
except Exception as e:
    log("ERR:", e)
    log(traceback.format_exc())
out.close()
print("DONE")
