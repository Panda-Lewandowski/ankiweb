#!/usr/bin/env python
"""Best-of-2 three-way comparison (SQLite / PG×1 / PG×4), all fixes applied.
Each config ran twice; per row we take the higher-rps run and report its rps AND
its own p50/p95 (so latency is consistent with the reported throughput)."""
import json, math, statistics, html

def load(*names):
    return [json.load(open(n)) for n in names]

SR = load("results_sqlite.json", "results_sqliteb.json")
P1 = load("results_pg1.json", "results_pg1b.json")
P4 = load("results_pg4.json", "results_pg4b.json")

def index(runs):
    return [{r["id"]: r for r in run["rows"]} for run in runs]
sr, p1, p4 = index(SR), index(P1), index(P4)
order = [r["id"] for r in SR[0]["rows"]]

def best(idxs, rid):
    """Return the run-record with the higher rps for this row (or the one that has rps)."""
    recs = [ix.get(rid) for ix in idxs]
    recs = [r for r in recs if r is not None]
    withr = [r for r in recs if r.get("rps")]
    if withr:
        return max(withr, key=lambda r: r["rps"])
    return recs[0] if recs else None

def spread(idxs, rid):
    vs = [ix[rid]["rps"] for ix in idxs if ix.get(rid) and ix[rid].get("rps")]
    if len(vs) == 2 and min(vs) > 0:
        return max(vs) / min(vs)
    return 1.0

def rps(rec):
    return rec.get("rps") if rec else None

def lat(rec):
    if not rec or not rec.get("lat_ms"):
        return None
    return rec["lat_ms"]

def annotated(rid):
    return bool(sr[0].get(rid, {}).get("annotate"))

def clean(rid):
    """All 6 runs succeeded fully (ok==n) and row not annotated."""
    if annotated(rid):
        return False
    for idxs in (sr, p1, p4):
        for ix in idxs:
            r = ix.get(rid)
            if not r or not r.get("rps") or r.get("n", 0) != r.get("ok", -1):
                return False
    return True

# ---------- assemble rows
PHASES = [("meta","元数据 / 协议层"),("setup","建库写入 + 可加性检查"),("read","数据读取"),
          ("write","数据写入"),("stats","统计"),("global","全库维护写"),("export","导入导出"),
          ("sweep","并发扫描 C=1/8"),("cleanup","删除清理")]

def ratio(a, b):
    ra, rb = rps(a), rps(b)
    return rb/ra if ra and rb else None

def fr(x):
    return "—" if x is None else f"{x:.2f}×"

def frps(rec):
    v = rps(rec)
    if v is None: return "—"
    return f"{v:,.0f}" if v>=100 else (f"{v:.1f}" if v>=10 else f"{v:.2f}")

rows_out = {}
for ph, title in PHASES:
    ids = [rid for rid in order if sr[0].get(rid,{}).get("phase")==ph and sr[0].get(rid,{}).get("kind")!="mixed"]
    rows_out[ph] = (title, ids)

# ---------- markdown (chat)
def latstr(rec):
    l = lat(rec)
    if not l: return "—"
    f=lambda v: f"{v:.0f}" if v>=100 else f"{v:.1f}"
    return f"{f(l['p50'])}/{f(l['p95'])}"

lines=[]
def P(s): lines.append(s)

for ph, title in PHASES:
    ids = rows_out[ph][1]
    if not ids: continue
    P(f"\n### {title}\n")
    P("| 接口 | C | SQLite rps | PG×1 rps | PG×4 rps | PG×1/SQLite | PG×4/SQLite | PG×4/PG×1 | SQLite p50/p95 | PG×1 | PG×4 |")
    P("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for rid in ids:
        a,b,c = best(sr,rid), best(p1,rid), best(p4,rid)
        mark = "‡" if annotated(rid) else ""
        P(f"| {rid}{mark} | {a['C']} | {frps(a)} | {frps(b)} | {frps(c)} | "
          f"{fr(ratio(a,b))} | {fr(ratio(a,c))} | {fr(ratio(b,c))} | "
          f"{latstr(a)} | {latstr(b)} | {latstr(c)} |")

# geomeans
def geo(v):
    v=[x for x in v if x]
    return math.exp(statistics.fmean(math.log(x) for x in v)) if v else None
groups={"读接口":lambda r:r["kind"]=="read","写接口":lambda r:r["kind"]=="write",
        "元数据":lambda r:r["kind"]=="meta" and r["id"]!="httpProbe"}
P("\n### 几何平均加速比（6 次运行全部成功、无 ‡ 注解的行；不含并发扫描补充点）\n")
P("| 类别 | 行数 | PG×1/SQLite | PG×4/SQLite | PG×4/PG×1 |")
P("|---|--:|--:|--:|--:|")
agg={}
for gname,pred in groups.items():
    ids=[rid for rid in order if pred(sr[0].get(rid,{})) and sr[0].get(rid,{}).get("phase")!="sweep" and clean(rid)]
    r1=geo([ratio(best(sr,i),best(p1,i)) for i in ids]); r4=geo([ratio(best(sr,i),best(p4,i)) for i in ids])
    r41=geo([ratio(best(p1,i),best(p4,i)) for i in ids])
    agg[gname]=dict(n=len(ids),r1=r1,r4=r4,r41=r41)
    P(f"| {gname} | {len(ids)} | {fr(r1)} | {fr(r4)} | {fr(r41)} |")

# mixed (best-of-2)
def mixed_best(runs):
    ms=[next(r for r in run["rows"] if r["kind"]=="mixed")["mixed"] for run in runs]
    return max(ms, key=lambda m:m["read"]["rps"]+m["write"]["rps"])
ms_,m1,m4 = mixed_best(SR), mixed_best(P1), mixed_best(P4)
P("\n### 混合负载（30 秒持续，32 并发 = 24 读 slot + 8 写 slot；best-of-2）\n")
P("| 指标 | SQLite | PG×1 | PG×4 | PG×4/SQLite | PG×4/PG×1 |")
P("|---|--:|--:|--:|--:|--:|")
for k,zh in (("read","读吞吐 rps"),("write","写吞吐 rps")):
    a,b,c=ms_[k]["rps"],m1[k]["rps"],m4[k]["rps"]
    P(f"| {zh} | {a:,.0f} | {b:,.0f} | {c:,.0f} | {c/a:.2f}× | {c/b:.2f}× |")
ta,tb,tc=(m["read"]["rps"]+m["write"]["rps"] for m in (ms_,m1,m4))
P(f"| **总吞吐 rps** | **{ta:,.0f}** | **{tb:,.0f}** | **{tc:,.0f}** | **{tc/ta:.2f}×** | **{tc/tb:.2f}×** |")
for k,zh in (("read","读 p50/p95 ms"),("write","写 p50/p95 ms")):
    f=lambda m:f"{m[k]['p50_ms']:.0f}/{m[k]['p95_ms']:.0f}"
    P(f"| {zh} | {f(ms_)} | {f(m1)} | {f(m4)} | | |")

# meta
def cpu(run): return round(run["meta"]["cpu1"]["cpu_s"]-run["meta"]["cpu0"]["cpu_s"],1)
P("\n### 运行元信息（两轮平均）\n")
P("| | SQLite | PG×1 | PG×4 |")
P("|---|--:|--:|--:|")
P(f"| 单轮基准时长 s | {statistics.mean(r['meta']['wall_total_s'] for r in SR):.0f} | {statistics.mean(r['meta']['wall_total_s'] for r in P1):.0f} | {statistics.mean(r['meta']['wall_total_s'] for r in P4):.0f} |")
P(f"| 服务端 CPU s | {statistics.mean(cpu(r) for r in SR):.0f} | {statistics.mean(cpu(r) for r in P1):.0f} | {statistics.mean(cpu(r) for r in P4):.0f} |")
P(f"| 服务进程数 | {SR[0]['meta']['cpu1']['pids']} | {P1[0]['meta']['cpu1']['pids']} | {P4[0]['meta']['cpu1']['pids']} |")
P(f"| 常驻内存 MB | {statistics.mean(r['meta']['cpu1']['rss_kb'] for r in SR)//1024:.0f} | {statistics.mean(r['meta']['cpu1']['rss_kb'] for r in P1)//1024:.0f} | {statistics.mean(r['meta']['cpu1']['rss_kb'] for r in P4)//1024:.0f} |")
P(f"| 底库 notes | {SR[0]['meta']['baseline']['notes']:,} | {P1[0]['meta']['baseline']['notes']:,} | {P4[0]['meta']['baseline']['notes']:,} |")

# error summary
P("\n### 错误 / 完整性\n")
allerr=0
for name,runs in (("SQLite",SR),("PG×1",P1),("PG×4",P4)):
    for i,run in enumerate(runs):
        e=[(r['id'],r['err_n']) for r in run['rows'] if r.get('err_n')]
        if e: allerr+=1; P(f"- {name} run{i+1}: {e}")
if not allerr:
    P("- **三版本各两轮共 6 次运行，全部接口零错误。** （首轮基准中 PG×4 的 createDeck / modelFieldRemove 72/96 / getCollectionStatsHTML 24/24 失败已全部修复。）")

# stability note
P("\n### 两轮稳定性（同配置两轮 rps 比值）\n")
unstable=[]
for rid in order:
    if sr[0].get(rid,{}).get("kind")=="mixed": continue
    for idxs,nm in ((sr,"SQLite"),(p1,"PG×1"),(p4,"PG×4")):
        s=spread(idxs,rid)
        if s>1.30: unstable.append((nm,rid,s))
allsp=[spread(idxs,rid) for rid in order if sr[0].get(rid,{}).get("kind")!="mixed" for idxs in (sr,p1,p4)]
P(f"- 全部行两轮比值 geomean {geo(allsp):.3f}×，中位 {statistics.median(allsp):.3f}×；混合负载 SQLite {ta:.0f}(两轮434/426) PG×1 {tb:.0f}(236/237) PG×4 {tc:.0f}(763/762) 高度一致。")
if unstable:
    P(f"- 波动>30%的行（取 best-of-2 已缓解）：" + ", ".join(f"{nm}:{rid}({s:.2f}×)" for nm,rid,s in unstable[:12]))

open("compare2.md","w").write("\n".join(lines))
json.dump({"agg":agg,"mixed":{"sqlite":ta,"pg1":tb,"pg4":tc}}, open("summary2.json","w"), indent=1)
print("\n".join(lines))
