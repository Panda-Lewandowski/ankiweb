#!/usr/bin/env python
"""Count PG round-trips per AnkiConnect action by strace-ing worker sendto()s
on their PostgreSQL sockets. Zero server-side changes.

Method: find each worker's PG connection fds (ss -tpn dst :15432), strace all
workers' sendto with timestamps, then bracket single warmed action calls with
wall-clock marks and count PG-fd sendto lines inside each bracket."""
import json, re, subprocess, time, sys
import httpx

BASE = os.environ.get("ANKIWEB_BENCH_BASE", "http://127.0.0.1:18765")
KEY = os.environ.get("ANKIWEB_AC_KEY", "")
SCRATCH = "/tmp/claude-0/-mnt-sda-git-web-ankiweb-pg/fc42d323-c49d-4401-af4d-b7e6f5e958ce/scratchpad"

client = httpx.Client(timeout=60)  # one persistent conn -> one worker handles all calls


def call(action, params=None, ok_required=True):
    r = client.post(BASE + "/", json={"action": action, "version": 6, "key": KEY,
                                      "params": params or {}}).json()
    if ok_required and r.get("error"):
        raise RuntimeError(f"{action}: {r['error']}")
    return r.get("result")


def pg_fds():
    """{pid: set(fd)} for sockets connected to :15432, per ankiweb worker."""
    out = subprocess.run(["ss", "-tpn", "state", "established", "dport", "=", ":15432"],
                         capture_output=True, text=True).stdout
    fds = {}
    for m in re.finditer(r'pid=(\d+),fd=(\d+)', out):
        fds.setdefault(int(m.group(1)), set()).add(int(m.group(2)))
    return fds


def main():
    # ---- bench data
    deck = f"ProfDeck{int(time.time())}"
    call("createDeck", {"deck": deck})
    model = "Basic" if "Basic" in (call("modelNames") or []) else None
    if model is None:
        call("createModel", {"modelName": f"ProfModel", "inOrderFields": ["F1", "F2"],
                             "cardTemplates": [{"Name": "C1", "Front": "{{F1}}", "Back": "{{F2}}"}]})
        model = "ProfModel"
    fields = call("modelFieldNames", {"modelName": model})
    f0, f1 = fields[0], fields[1]
    nids = call("addNotes", {"notes": [
        {"deckName": deck, "modelName": model,
         "fields": {f0: f"prof-{i}", f1: "x"}, "tags": ["prof"],
         "options": {"allowDuplicate": False}} for i in range(40)]})
    cids = call("findCards", {"query": f'deck:"{deck}"'})
    print(f"data: deck={deck} notes={len(nids)} cards={len(cids)} model={model}", file=sys.stderr)

    workers = subprocess.run(["bash", "-c",
        "for pid in $(pgrep -f 'python -m ankiweb'); do "
        "tr '\\0' '\\n' < /proc/$pid/environ 2>/dev/null | grep -q '^ANKIWEB_WORKER_ID=' && echo $pid; done"],
        capture_output=True, text=True).stdout.split()
    fdmap = pg_fds()
    fdmap = {int(p): fdmap.get(int(p), set()) for p in workers}
    # strace logs THREAD ids (tokio backend threads do the PG I/O); map tid -> pid
    import os
    tid2pid = {}
    for p in workers:
        for tid in os.listdir(f"/proc/{p}/task"):
            tid2pid[int(tid)] = int(p)
    print(f"workers={workers} pg_fds={fdmap} tids={len(tid2pid)}", file=sys.stderr)

    trace_path = f"{SCRATCH}/rt_trace.txt"
    st = subprocess.Popen(
        ["strace", "-f", "-tt", "-e", "trace=sendto,write,writev,sendmsg", "-o", trace_path,
         *sum((["-p", p] for p in workers), [])],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)

    ACTIONS = [
        ("version", "version", {}),
        ("findCards[deck]", "findCards", {"query": f'deck:"{deck}"'}),
        ("findCards[deck+is:due]", "findCards", {"query": f'deck:"{deck}" is:due -is:new -is:learn'}),
        ("findCards[deck+is:new]", "findCards", {"query": f'deck:"{deck}" is:new'}),
        ("getDeckStats[1]", "getDeckStats", {"decks": [deck]}),
        ("getDeckConfig", "getDeckConfig", {"deck": deck}),
        ("cardsInfo[10]", "cardsInfo", {"cards": cids[:10]}),
        ("cardsInfo[1]", "cardsInfo", {"cards": cids[:1]}),
        ("notesInfo[10]", "notesInfo", {"notes": nids[:10]}),
        ("cardsToNotes[10]", "cardsToNotes", {"cards": cids[:10]}),
        ("answerCards[1]", "answerCards", {"answers": [{"cardId": cids[20], "ease": 3}]}),
        ("addTags[1note]", "addTags", {"notes": nids[:1], "tags": "pt"}),
        ("removeTags[1note]", "removeTags", {"notes": nids[:1], "tags": "pt"}),
        ("addTags[10notes]", "addTags", {"notes": nids[:10], "tags": "pt2"}),
        ("removeTags[10notes]", "removeTags", {"notes": nids[:10], "tags": "pt2"}),
        ("canAddNotes[10]", "canAddNotes", {"notes": [
            {"deckName": deck, "modelName": model, "fields": {f0: f"can-{i}", f1: "y"}, "tags": []}
            for i in range(10)]}),
        ("addNotes[5]", "addNotes", {"notes": [
            {"deckName": deck, "modelName": model, "fields": {f0: f"an2-{i}", f1: "y"},
             "tags": [], "options": {"allowDuplicate": False}} for i in range(5)]}),
        ("addNote[1]", "addNote", {"note": {"deckName": deck, "modelName": model,
                                            "fields": {f0: "single-1", f1: "y"}, "tags": [],
                                            "options": {"allowDuplicate": False}}}),
        ("updateNoteFields", "updateNoteFields", {"note": {"id": nids[0], "fields": {f1: "upd"}}}),
        ("createDeck[child]", "createDeck", {"deck": f"{deck}::sub1"}),
        ("deleteNotes[5]", "deleteNotes", {"notes": nids[30:35]}),
    ]

    marks = []
    for label, action, params in ACTIONS:
        for _ in range(2):  # warm rust caches / plans
            try:
                call(action, params, ok_required=False)
            except Exception:
                pass
        time.sleep(0.25)
        t0 = time.time()
        call(action, params, ok_required=False)
        t1 = time.time()
        marks.append((label, t0, t1))
        time.sleep(0.25)

    time.sleep(1)
    st.terminate()
    st.wait()

    # parse: lines like "PID HH:MM:SS.ffffff sendto(FD, ..."
    day0 = time.strftime("%Y-%m-%d")
    events = []
    for line in open(trace_path):
        m = re.match(r"(\d+)\s+(\d\d:\d\d:\d\d\.\d+)\s+(?:sendto|sendmsg|writev|write)\((\d+)[,)]", line)
        if not m:
            continue
        tid, ts, fd = int(m.group(1)), m.group(2), int(m.group(3))
        pid = tid2pid.get(tid, tid)
        if fd in fdmap.get(pid, ()):  # only PG-socket sends
            h, mi, s = ts.split(":")
            tod = int(h) * 3600 + int(mi) * 60 + float(s)
            events.append(tod)
    events.sort()

    print(f"{'action':<28} {'PG_roundtrips':>13}")
    import bisect
    for label, t0, t1 in marks:
        lt0 = time.localtime(t0)
        tod0 = lt0.tm_hour * 3600 + lt0.tm_min * 60 + lt0.tm_sec + (t0 % 1)
        lt1 = time.localtime(t1)
        tod1 = lt1.tm_hour * 3600 + lt1.tm_min * 60 + lt1.tm_sec + (t1 % 1)
        n = bisect.bisect_left(events, tod1 + 0.02) - bisect.bisect_left(events, tod0 - 0.005)
        print(f"{label:<28} {n:>13}")

    # cleanup
    call("deleteDecks", {"decks": [deck], "cardsToo": True})
    if model == "ProfModel":
        pass  # left for reuse across runs; harmless
    print("cleanup done", file=sys.stderr)


main()
