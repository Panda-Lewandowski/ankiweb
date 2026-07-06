#!/usr/bin/env python
"""Live validation of the two PG multi-worker race fixes against a running pg4 server.

Every request uses a FRESH connection so consecutive ops hop workers (SO_REUSEPORT
assigns per-connection), which is exactly the condition that exposed both races."""
import os
import asyncio, json, time, sys
import httpx

BASE = os.environ.get("ANKIWEB_BENCH_BASE", "http://127.0.0.1:18765")
KEY = os.environ.get("ANKIWEB_AC_KEY", "")


async def call(action, params=None):
    async with httpx.AsyncClient(timeout=30) as c:  # fresh connection per call
        r = await c.post(BASE + "/", json={"action": action, "version": 6, "key": KEY,
                                           "params": params or {}})
        j = r.json()
        return j.get("result"), j.get("error")


async def test_create_deck_race(rounds=12, fanout=8):
    fails = []
    for r in range(rounds):
        parent = f"RaceChk{int(time.time()*1000)}"
        results = await asyncio.gather(
            *(call("createDeck", {"deck": f"{parent}::c{i}"}) for i in range(fanout)))
        errs = [e for _, e in results if e]
        if errs:
            fails.append((parent, errs))
        await call("deleteDecks", {"decks": [parent], "cardsToo": True})
    print(f"A createDeck race: {rounds} rounds x {fanout} concurrent -> "
          f"{'0 failures PASS' if not fails else f'FAIL {fails[:3]}'}")
    return not fails


async def test_notetype_coherence(n=24):
    name = f"RaceChkModel{int(time.time()*1000)}"
    _, err = await call("createModel", {
        "modelName": name, "inOrderFields": ["F1", "F2"],
        "cardTemplates": [{"Name": "Card 1", "Front": "{{F1}}", "Back": "{{F2}}"}]})
    assert not err, err
    bad = []
    for i in range(n):
        _, e1 = await call("modelFieldAdd", {"modelName": name, "fieldName": f"bf{i}", "index": 2})
        fields, e2 = await call("modelFieldNames", {"modelName": name})
        _, e3 = await call("modelFieldRemove", {"modelName": name, "fieldName": f"bf{i}"})
        if e1 or e2 or e3 or (fields and f"bf{i}" not in fields):
            bad.append((i, e1, e2, e3, fields))
    fields, _ = await call("modelFieldNames", {"modelName": name})
    ok = not bad and fields == ["F1", "F2"]
    print(f"B notetype coherence: {n} add/list/remove cycles over fresh conns -> "
          f"{'0 stale reads PASS' if ok else f'FAIL {bad[:3]} final={fields}'}")
    async with httpx.AsyncClient(timeout=30) as c:
        await c.post(BASE + f"/extra_actions/deleteModel", json={"modelName": name},
                     headers={"X-API-Key": KEY})
    return ok


async def test_create_model_race(fanout=8):
    name = f"RaceChkDup{int(time.time()*1000)}"
    spec = {"modelName": name, "inOrderFields": ["F1"],
            "cardTemplates": [{"Name": "Card 1", "Front": "{{F1}}", "Back": "x"}]}
    results = await asyncio.gather(*(call("createModel", spec) for _ in range(fanout)))
    wins = sum(1 for res, e in results if not e)
    canonical = sum(1 for _, e in results if e and "already exists" in e)
    dberr = [e for _, e in results if e and "already exists" not in e]
    ok = wins == 1 and canonical == fanout - 1 and not dberr
    print(f"C createModel same-name x{fanout}: wins={wins} canonical-errs={canonical} "
          f"db-errs={len(dberr)} -> {'PASS' if ok else f'FAIL {dberr[:2]}'}")
    async with httpx.AsyncClient(timeout=30) as c:
        await c.post(BASE + f"/extra_actions/deleteModel", json={"modelName": name},
                     headers={"X-API-Key": KEY})
    return ok


async def main():
    a = await test_create_deck_race()
    b = await test_notetype_coherence()
    c = await test_create_model_race()
    print("ALL PASS" if (a and b and c) else "SOME FAILED")
    sys.exit(0 if (a and b and c) else 1)

asyncio.run(main())
