#!/usr/bin/env python
"""Histogram the actual SQL statements one cardsInfo[10] / canAddNotes[10] /
notesInfo[10] / answerCards[1] call issues, by parsing Parse/Bind messages out of
an strace of the worker PG sockets (-s 200)."""
import os, re, subprocess, time, sys, bisect
from collections import Counter
import httpx

BASE = os.environ.get("ANKIWEB_BENCH_BASE", "http://127.0.0.1:18765")
KEY = os.environ.get("ANKIWEB_AC_KEY", "")
SCRATCH = "/tmp/claude-0/-mnt-sda-git-web-ankiweb-pg/fc42d323-c49d-4401-af4d-b7e6f5e958ce/scratchpad"
client = httpx.Client(timeout=60)


def call(action, params=None):
    r = client.post(BASE + "/", json={"action": action, "version": 6, "key": KEY,
                                      "params": params or {}}).json()
    if r.get("error"):
        raise RuntimeError(f"{action}: {r['error']}")
    return r.get("result")


deck = f"SqlProf{int(time.time())}"
call("createDeck", {"deck": deck})
model = "Basic"
fields = call("modelFieldNames", {"modelName": model})
f0, f1 = fields[0], fields[1]
nids = call("addNotes", {"notes": [
    {"deckName": deck, "modelName": model, "fields": {f0: f"sq-{i}", f1: "x"},
     "tags": ["sq"], "options": {"allowDuplicate": False}} for i in range(30)]})
cids = call("findCards", {"query": f'deck:"{deck}"'})

workers = subprocess.run(["bash", "-c",
    "for pid in $(pgrep -f 'python -m ankiweb'); do "
    "tr '\\0' '\\n' < /proc/$pid/environ 2>/dev/null | grep -q '^ANKIWEB_WORKER_ID=' && echo $pid; done"],
    capture_output=True, text=True).stdout.split()

trace = f"{SCRATCH}/sql_trace.txt"
st = subprocess.Popen(
    ["strace", "-f", "-tt", "-s", "220", "-e", "trace=sendto", "-o", trace,
     *sum((["-p", p] for p in workers), [])],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)

ACTIONS = [
    ("cardsInfo[10]", "cardsInfo", {"cards": cids[:10]}),
    ("canAddNotes[10]", "canAddNotes", {"notes": [
        {"deckName": deck, "modelName": model, "fields": {f0: f"cq-{i}", f1: "y"}, "tags": []}
        for i in range(10)]}),
    ("notesInfo[10]", "notesInfo", {"notes": nids[:10]}),
    ("answerCards[1]", "answerCards", {"answers": [{"cardId": cids[15], "ease": 3}]}),
    ("updateNoteFields", "updateNoteFields", {"note": {"id": nids[0], "fields": {f1: "u"}}}),
    ("getDeckStats[1]", "getDeckStats", {"decks": [deck]}),
]
marks = []
for label, action, params in ACTIONS:
    call(action, params)      # warm (also registers prepared statements)
    time.sleep(0.3)
    t0 = time.time()
    call(action, params)
    t1 = time.time()
    marks.append((label, t0, t1))
    time.sleep(0.3)

time.sleep(1)
st.terminate(); st.wait()

# name -> sql from every Parse seen anywhere in the trace (statement cache persists)
name2sql = {}
events = []  # (tod, kind, payload)
lre = re.compile(r'(\d+)\s+(\d\d):(\d\d):(\d\d\.\d+)\s+sendto\(\d+, "(.*)", \d+, MSG_NOSIGNAL', )
for line in open(trace):
    m = lre.match(line)
    if not m:
        continue
    tod = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + float(m.group(4))
    buf = m.group(5)
    for pm in re.finditer(r'P\\0\\0\\0.{1,4}?(s\d+)\\0([A-Za-z][^\\"]{0,180})', buf):
        name2sql[pm.group(1)] = pm.group(2)
        events.append((tod, "parse", pm.group(1)))
    for bm in re.finditer(r'B\\0\\0\\0.{1,4}?\\0(s\d+)\\0', buf):
        events.append((tod, "bind", bm.group(1)))
    if re.search(r'(?<![A-Za-z])Q\\0\\0\\0', buf):
        events.append((tod, "simpleQ", buf[:60]))
events.sort(key=lambda e: e[0])
times = [e[0] for e in events]

for label, t0, t1 in marks:
    lt0, lt1 = time.localtime(t0), time.localtime(t1)
    a = lt0.tm_hour * 3600 + lt0.tm_min * 60 + lt0.tm_sec + (t0 % 1) - 0.005
    b = lt1.tm_hour * 3600 + lt1.tm_min * 60 + lt1.tm_sec + (t1 % 1) + 0.02
    window = events[bisect.bisect_left(times, a):bisect.bisect_left(times, b)]
    hist = Counter()
    for _, kind, payload in window:
        if kind in ("parse", "bind"):
            hist[name2sql.get(payload, f"<uncached {payload}>")] += 1 if kind == "bind" else 0
            if kind == "parse":
                hist[name2sql.get(payload, payload)] += 1
        else:
            hist[f"[simple] {payload}"] += 1
    print(f"\n=== {label}: {sum(hist.values())} statements")
    for sql, n in hist.most_common(18):
        print(f"  {n:>3}x  {sql[:150]}")

call("deleteDecks", {"decks": [deck], "cardsToo": True})
print("\ncleanup done", file=sys.stderr)
