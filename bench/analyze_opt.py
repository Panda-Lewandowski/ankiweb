#!/usr/bin/env python
"""Before/after comparison for the review_agent-relevant action set:
PG×1 vs PG×1opt, PG×4ᶠ vs PG×4opt (SQLite as reference column)."""
import json, math, statistics

S = json.load(open("results_sqlite.json"))
P1 = json.load(open("results_pg1.json"))
P1O = json.load(open("results_pg1opt.json"))
PF = json.load(open("results_pg4fix.json"))
PO = json.load(open("results_pg4opt.json"))
by = lambda d: {r["id"]: r for r in d["rows"]}
bs, b1, b1o, bf, bo = by(S), by(P1), by(P1O), by(PF), by(PO)
order = [r["id"] for r in S["rows"]]

# review_agent's actual AnkiConnect surface, mapped to bench rows
RA_ROWS = [
    "findCards[deck]", "findNotes[deck]", "getDeckStats(12)", "getDeckConfig",
    "cardsInfo[1]", "cardsInfo[50]", "notesInfo[1]", "notesInfo[100]",
    "cardsToNotes(50)", "answerCards[1]", "createDeck", "createModel",
    "modelNames", "modelFieldNames", "canAddNote", "canAddNotes(25)",
    "addNote", "addNotes(100)", "updateNoteFields", "addTags(20)", "removeTags(20)",
    "deleteNotes(50)", "deleteDecks[1]", "deckNames",
    "saveDeckConfig", "setDeckConfigId", "cloneDeckConfigId", "removeDeckConfigId",
    "deleteModel*",
    "findCards[deck]@C1", "findCards[deck]@C8", "notesInfo[100]@C1", "notesInfo[100]@C8",
    "addNote@C1", "addNote@C8", "answerCards@C1", "answerCards@C8",
    "updateNoteFields@C1", "updateNoteFields@C8",
]

def rps(r):
    return r.get("rps") if r else None

def cell(r):
    v = rps(r)
    if v is None:
        return "—"
    frac = r["ok"] / r["n"] if r["n"] else 1
    star = "†" if frac < 0.99 else ""
    return (f"{v:,.0f}" if v >= 100 else f"{v:.1f}") + star

def imp(a, b):
    va, vb = rps(a), rps(b)
    return f"{vb/va:.2f}×" if va and vb else "—"

print("| 接口 | C | SQLite | PG×1 | PG×1ᵒᵖᵗ | 提升 | PG×4ᶠ | PG×4ᵒᵖᵗ | 提升 | PG×4ᵒᵖᵗ/SQLite |")
print("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
r1s, r4s, rss = [], [], []
for rid in RA_ROWS:
    if rid not in bs:
        continue
    a, b, c, d, e = bs[rid], b1.get(rid), b1o.get(rid), bf.get(rid), bo.get(rid)
    print(f"| {rid} | {a['C']} | {cell(a)} | {cell(b)} | {cell(c)} | {imp(b, c)} | "
          f"{cell(d)} | {cell(e)} | {imp(d, e)} | {imp(a, e)} |")
    if rps(b) and rps(c):
        r1s.append(rps(c) / rps(b))
    if rps(d) and rps(e):
        r4s.append(rps(e) / rps(d))
    if rps(a) and rps(e):
        rss.append(rps(e) / rps(a))

geo = lambda v: math.exp(statistics.fmean(math.log(x) for x in v)) if v else 0
print(f"\nreview_agent 操作集几何平均：PG×1 优化提升 {geo(r1s):.2f}×  |  "
      f"PG×4 优化提升 {geo(r4s):.2f}×  |  PG×4ᵒᵖᵗ vs SQLite {geo(rss):.2f}×")

for name, d in (("PG×1", P1), ("PG×1opt", P1O), ("PG×4ᶠ", PF), ("PG×4opt", PO), ("SQLite", S)):
    m = next(r for r in d["rows"] if r["kind"] == "mixed")["mixed"]
    print(f"mixed {name:>8}: read {m['read']['rps']:>6} rps (p50 {m['read']['p50_ms']}ms)  "
          f"write {m['write']['rps']:>6} rps (p50 {m['write']['p50_ms']}ms)  "
          f"total {m['read']['rps']+m['write']['rps']:.0f}")

# regressions check across ALL rows (not just RA set)
print("\n可能回归（优化后慢于 15% 以上的行）:")
for rid in order:
    for pre, post, tag in ((b1, b1o, "PG×1"), (bf, bo, "PG×4")):
        a, b = pre.get(rid), post.get(rid)
        if rps(a) and rps(b) and rps(b) < rps(a) * 0.85:
            print(f"  {tag} {rid}: {rps(a)} -> {rps(b)}  ({rps(b)/rps(a):.2f}×)")
