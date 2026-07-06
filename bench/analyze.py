#!/usr/bin/env python
"""Merge the four acbench result files into comparison tables (markdown + json)."""
import json, math, statistics

S = json.load(open("results_sqlite.json"))
P1 = json.load(open("results_pg1.json"))
P4 = json.load(open("results_pg4.json"))
PF = json.load(open("results_pg4fix.json"))
RUNS = [("SQLite", S), ("PG×1", P1), ("PG×4", P4), ("PG×4ᶠ", PF)]

by = lambda d: {r["id"]: r for r in d["rows"]}
bs, b1, b4, bf = by(S), by(P1), by(P4), by(PF)
order = [r["id"] for r in S["rows"]]

def cell_rps(r):
    if r is None:
        return "—"
    if not r.get("rps"):
        return "失败"
    frac = r["ok"] / r["n"] if r["n"] else 1
    star = "†" if frac < 0.99 else ""
    v = r["rps"]
    s = f"{v:,.0f}" if v >= 100 else (f"{v:.1f}" if v >= 10 else f"{v:.2f}")
    return s + star

def cell_lat(r):
    if r is None or not r.get("lat_ms"):
        return "—"
    l = r["lat_ms"]
    f = lambda v: f"{v:,.1f}" if v < 100 else f"{v:,.0f}"
    return f"{f(l['p50'])}/{f(l['p95'])}"

def ratio(a, b):
    if not a or not b or not a.get("rps") or not b.get("rps"):
        return None
    return b["rps"] / a["rps"]

def fr(x):
    return "—" if x is None else f"{x:.2f}×"

PHASE_TITLES = [
    ("meta", "元数据 / 协议层"),
    ("setup", "新建写入 + 可加性检查"),
    ("read", "数据读取"),
    ("write", "数据写入"),
    ("stats", "统计"),
    ("global", "全库维护写"),
    ("export", "导入导出"),
    ("sweep", "并发扫描（C=1/8 补充点）"),
    ("cleanup", "删除清理"),
]

chat, full = [], []

def emit(line, both=True):
    full.append(line)
    if both:
        chat.append(line)

for phase, title in PHASE_TITLES:
    rows = [rid for rid in order if bs[rid]["phase"] == phase and bs[rid]["kind"] != "mixed"]
    if not rows:
        continue
    emit(f"\n### {title}\n")
    emit("| 接口 | C | SQLite | PG×1 | PG×4 | PG×4ᶠ | PG×1/SQLite | PG×4ᶠ/SQLite | PG×4ᶠ/PG×1 |")
    emit("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    full[-2:] = ["| 接口 | C | SQLite | PG×1 | PG×4 | PG×4ᶠ | PG×1/SQLite | PG×4ᶠ/SQLite | PG×4ᶠ/PG×1 | SQLite p50/p95 | PG×1 p50/p95 | PG×4 p50/p95 | PG×4ᶠ p50/p95 |",
                 "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for rid in rows:
        a, b, c, d = bs.get(rid), b1.get(rid), b4.get(rid), bf.get(rid)
        mark = "‡" if a.get("annotate") else ""
        base = (f"| {rid}{mark} | {a['C']} | {cell_rps(a)} | {cell_rps(b)} | {cell_rps(c)} | {cell_rps(d)} | "
                f"{fr(ratio(a, b))} | {fr(ratio(a, d))} | {fr(ratio(b, d))} |")
        chat.append(base)
        full.append(base + f" {cell_lat(a)} | {cell_lat(b)} | {cell_lat(c)} | {cell_lat(d)} |")

def geo(vals):
    vals = [v for v in vals if v]
    return math.exp(statistics.fmean(math.log(v) for v in vals)) if vals else None

groups = {
    "读接口": lambda r: r["kind"] == "read" and not r["annotate"],
    "写接口": lambda r: r["kind"] == "write" and not r["annotate"],
    "元数据": lambda r: r["kind"] == "meta" and r["id"] != "httpProbe",
}
emit("\n### 几何平均加速比（全成功、无注解的行）\n")
emit("| 类别 | 行数 | PG×1/SQLite | PG×4/SQLite | PG×4ᶠ/SQLite | PG×4ᶠ/PG×1 |")
emit("|---|--:|--:|--:|--:|--:|")
agg = {}
for gname, pred in groups.items():
    ids = [rid for rid in order if pred(bs[rid]) and bs[rid]["phase"] != "sweep"
           and all(x.get(rid, {}).get("rps") for x in (bs, b1, b4, bf))
           and all(x[rid]["ok"] == x[rid]["n"] for x in (bs, b1, b4, bf))]
    r1 = geo([ratio(bs[i], b1[i]) for i in ids])
    r4 = geo([ratio(bs[i], b4[i]) for i in ids])
    rf = geo([ratio(bs[i], bf[i]) for i in ids])
    rf1 = geo([ratio(b1[i], bf[i]) for i in ids])
    agg[gname] = dict(n=len(ids), r1=r1, r4=r4, rf=rf, rf1=rf1)
    emit(f"| {gname} | {len(ids)} | {fr(r1)} | {fr(r4)} | {fr(rf)} | {fr(rf1)} |")

emit("\n### 混合负载（30 秒持续，32 并发 = 24 读 slot + 8 写 slot）\n")
emit("| 指标 | SQLite | PG×1 | PG×4 | PG×4ᶠ | PG×4ᶠ/SQLite | PG×4ᶠ/PG×1 |")
emit("|---|--:|--:|--:|--:|--:|--:|")
mm = [next(r for r in d["rows"] if r["kind"] == "mixed")["mixed"] for _, d in RUNS]
for kind, zh in (("read", "读吞吐 rps"), ("write", "写吞吐 rps")):
    v = [m[kind]["rps"] for m in mm]
    emit(f"| {zh} | {v[0]} | {v[1]} | {v[2]} | {v[3]} | {v[3]/v[0]:.2f}× | {v[3]/v[1]:.2f}× |")
tot = [m["read"]["rps"] + m["write"]["rps"] for m in mm]
emit(f"| 总吞吐 rps | {tot[0]:.0f} | {tot[1]:.0f} | {tot[2]:.0f} | {tot[3]:.0f} | {tot[3]/tot[0]:.2f}× | {tot[3]/tot[1]:.2f}× |")
for kind, zh in (("read", "读延迟 p50/p95 ms"), ("write", "写延迟 p50/p95 ms")):
    f = lambda m: f"{m[kind]['p50_ms']:.0f}/{m[kind]['p95_ms']:.0f}"
    emit(f"| {zh} | {f(mm[0])} | {f(mm[1])} | {f(mm[2])} | {f(mm[3])} | | |")

emit("\n### 运行元信息\n")
emit("| | SQLite | PG×1 | PG×4 | PG×4ᶠ |")
emit("|---|--|--|--|--|")
g = lambda fn: " | ".join(str(fn(d)) for _, d in RUNS)
emit(f"| 基准总时长 s | {g(lambda d: d['meta']['wall_total_s'])} |")
emit(f"| 服务端 CPU 秒 | {g(lambda d: round(d['meta']['cpu1']['cpu_s']-d['meta']['cpu0']['cpu_s'],1))} |")
emit(f"| 服务进程数 | {g(lambda d: d['meta']['cpu1']['pids'])} |")
emit(f"| RSS MB | {g(lambda d: d['meta']['cpu1']['rss_kb']//1024)} |")
emit(f"| 基线 notes | {g(lambda d: d['meta']['baseline']['notes'])} |")
emit(f"| 清理还原 | {g(lambda d: d['meta']['cleanup_ok'])} |")

emit("\n### 失败 / 完整性备注\n")
for n, d in RUNS:
    for r in d["rows"]:
        if r.get("err_n"):
            emit(f"- **{n} / {r['id']}**: {r['err_n']}/{r['n']} 失败 — `{(r['errors'] or [['','','?']])[0][2][:110]}`")

open("compare_chat.md", "w").write("\n".join(chat))
open("compare_full.md", "w").write("\n".join(full))
json.dump(agg, open("agg.json", "w"), indent=1)
print("\n".join(chat))
