#!/usr/bin/env python
"""acbench.py — formal AnkiConnect benchmark against one running ankiweb server.

Design:
  * 4 client PROCESSES (sidesteps client GIL; matches SO_REUSEPORT per-connection
    load-spreading of the multi-worker server), each running an asyncio loop with
    row-specific concurrency. Fresh TCP connections per row re-roll kernel spreading.
  * Every row = one AnkiConnect action with a deterministic parameter stream.
    Global slot id g = proc*conc + slot owns the sequence k=0,1,2… ; the unique
    call index is idx = g + T*k (T = total concurrency). Paired rows (add/remove,
    suspend/unsuspend, store/delete) share T, warmup and n, so their idx spaces
    mirror exactly and every create has a matching destroy.
  * Warmup requests (k < w_slot) are sent but not timed; a Barrier aligns all
    procs between warmup and the measured window.
  * The bench creates its own data (model family AWB-*, deck tree AWBench::*,
    media bench-*.txt) and removes all of it at the end; baseline entity counts
    are verified. revlog additions are reported for external purge.

Usage: acbench.py --label sqlite --out results_sqlite.json [--smoke]
"""
from __future__ import annotations
import argparse, asyncio, base64, json, math, os, statistics, subprocess, sys, time
import multiprocessing as mp
import httpx

BASE = os.environ.get("ANKIWEB_BENCH_BASE", "http://127.0.0.1:18765")
KEY = os.environ.get("ANKIWEB_AC_KEY", "")
NWORKERS = 4
PAY4K = base64.b64encode(os.urandom(4096)).decode()
SCRATCH = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- helpers

def chunk(ids, idx, size):
    L = len(ids)
    if L == 0:
        return []
    if size >= L:
        return list(ids)
    start = (idx * size) % L
    end = start + size
    if end <= L:
        return list(ids[start:end])
    return list(ids[start:]) + list(ids[: end - L])


def pct(sorted_lat, q):
    if not sorted_lat:
        return None
    i = min(len(sorted_lat) - 1, max(0, int(round(q * (len(sorted_lat) - 1)))))
    return sorted_lat[i]


# ---------------------------------------------------------------- generators
# gen(ctx, row, idx, k, g) -> (action, params, extra_route: bool)

def g_simple(ctx, row, idx, k, g):
    return row["gargs"]["action"], dict(row["gargs"].get("params") or {}), row["gargs"].get("extra", False)

def g_probe(ctx, row, idx, k, g):
    return "__probe__", None, False

def g_multi(ctx, row, idx, k, g):
    return "multi", {"actions": [{"action": "version", "key": KEY} for _ in range(10)]}, False

def g_create_model(ctx, row, idx, k, g):
    name = ctx["model_names"][idx]
    return "createModel", {
        "modelName": name,
        "inOrderFields": ["F1", "F2", "F3"],
        "css": ".card { font-family: arial; } /*tok0*/",
        "cardTemplates": [
            {"Name": "Card 1", "Front": "{{F1}}", "Back": "{{F1}}<hr id=answer>{{F2}}"},
            {"Name": "TR", "Front": "{{F2}} ?", "Back": "{{F3}}"},
        ],
    }, False

def g_create_deck(ctx, row, idx, k, g):
    return "createDeck", {"deck": ctx["deck_names_all"][idx]}, False

def g_add_note(ctx, row, idx, k, g):
    d = ctx["content_decks"][idx % 12]
    return "addNote", {"note": {
        "deckName": d, "modelName": ctx["bm_a"],
        "fields": {"F1": f"b-{row['id']}-{idx}", "F2": "lorem ipsum " * 5, "F3": f"x{idx}"},
        "tags": ["bench-base"], "options": {"allowDuplicate": False},
    }}, False

def g_add_notes(ctx, row, idx, k, g):
    d = ctx["content_decks"][idx] if idx < 12 else ctx["move_pool_deck"]
    notes = [{
        "deckName": d, "modelName": ctx["bm_a"],
        "fields": {"F1": f"bb-{row['id']}-{idx}-{j}", "F2": "dolor sit amet " * 4, "F3": f"y{j}"},
        "tags": ["bench-base"], "options": {"allowDuplicate": False},
    } for j in range(100)]
    return "addNotes", {"notes": notes}, False

def g_can_add(ctx, row, idx, k, g):
    dup = idx % 2 == 0
    f1 = f"b-addNote-{(idx * 7) % 500}" if dup else f"c-{row['id']}-{idx}"
    note = {"deckName": ctx["content_decks"][idx % 12], "modelName": ctx["bm_a"],
            "fields": {"F1": f1, "F2": "z", "F3": ""}, "tags": []}
    return row["gargs"]["action"], {"note": note}, False

def g_can_add_batch(ctx, row, idx, k, g):
    notes = []
    for j in range(25):
        dup = j % 2 == 0
        f1 = f"b-addNote-{((idx * 25 + j) * 3) % 500}" if dup else f"cb-{row['id']}-{idx}-{j}"
        notes.append({"deckName": ctx["content_decks"][j % 12], "modelName": ctx["bm_a"],
                      "fields": {"F1": f1, "F2": "z", "F3": ""}, "tags": []})
    return row["gargs"]["action"], {"notes": notes}, False

def g_store_media(ctx, row, idx, k, g):
    return "storeMediaFile", {"filename": f"bench-{idx}.txt", "data": PAY4K}, False

def g_delete_media(ctx, row, idx, k, g):
    return "deleteMediaFile", {"filename": f"bench-{idx}.txt"}, False

def g_retrieve_media(ctx, row, idx, k, g):
    return "retrieveMediaFile", {"filename": "bench-fixed.txt"}, False

def g_deck_name_from_id(ctx, row, idx, k, g):
    return "deckNameFromId", {"deckId": ctx["deck_ids"]["AWBench::D03"]}, False

def g_get_decks(ctx, row, idx, k, g):
    return "getDecks", {"cards": chunk(ctx["content_cards"], idx, 30)}, False

def g_deck_stats(ctx, row, idx, k, g):
    return "getDeckStats", {"decks": ctx["content_decks"]}, False

def g_model_read(ctx, row, idx, k, g):
    return row["gargs"]["action"], {"modelName": ctx["bm_a"]}, False

def g_model_name_from_id(ctx, row, idx, k, g):
    return "modelNameFromId", {"modelId": ctx["model_ids"][ctx["bm_a"]]}, False

def g_find_models_by_id(ctx, row, idx, k, g):
    return "findModelsById", {"modelIds": [ctx["model_ids"][ctx["bm_a"]]]}, False

def g_find_models_by_name(ctx, row, idx, k, g):
    return "findModelsByName", {"modelNames": [ctx["bm_a"]]}, False

def g_query(ctx, row, idx, k, g):
    return row["gargs"]["action"], {"query": row["gargs"]["query"]}, False

def g_ids_batch(ctx, row, idx, k, g):
    pool = ctx[row["gargs"]["pool"]]
    size = row["gargs"]["size"]
    key = row["gargs"].get("key", "cards")
    return row["gargs"]["action"], {key: chunk(pool, idx, size)}, False

def g_single_id(ctx, row, idx, k, g):
    pool = ctx[row["gargs"]["pool"]]
    key = row["gargs"].get("key", "card")
    val = pool[idx % len(pool)]
    p = {key: [val]} if row["gargs"].get("aslist") else {key: val}
    return row["gargs"]["action"], p, False

def g_deck_param(ctx, row, idx, k, g):
    return row["gargs"]["action"], dict(row["gargs"].get("params") or {}, deck=row["gargs"].get("deck", "AWBench::D03")), row["gargs"].get("extra", False)

def g_upd_fields(ctx, row, idx, k, g):
    nid = ctx["note_ids"][idx % len(ctx["note_ids"])]
    return "updateNoteFields", {"note": {"id": nid, "fields": {"F2": f"upd-{row['id']}-{idx}"}}}, False

def g_upd_note(ctx, row, idx, k, g):
    nid = ctx["note_ids"][idx % len(ctx["note_ids"])]
    return "updateNote", {"note": {"id": nid, "fields": {"F3": f"un-{idx}"},
                                   "tags": ["bench-base", f"unt{idx % 7}"]}}, False

def g_upd_tags(ctx, row, idx, k, g):
    nid = ctx["note_ids"][idx % len(ctx["note_ids"])]
    return "updateNoteTags", {"note": nid, "tags": ["bench-base", f"ut{idx % 9}"]}, False

def g_add_tags(ctx, row, idx, k, g):
    return "addTags", {"notes": chunk(ctx["note_ids"], idx, 20), "tags": f"bt-{idx}"}, False

def g_remove_tags(ctx, row, idx, k, g):
    return "removeTags", {"notes": chunk(ctx["note_ids"], idx, 20), "tags": f"bt-{idx}"}, False

def g_replace_tags(ctx, row, idx, k, g):
    ch = chunk(ctx["note_ids"], g, 20)          # slot-OWNED chunk (by g, not idx)
    a, b = ("bench-base", "bench-alt") if k % 2 == 0 else ("bench-alt", "bench-base")
    return "replaceTags", {"notes": ch, "tag_to_replace": a, "replace_with_tag": b}, False

def g_replace_all_tags(ctx, row, idx, k, g):
    a, b = ("bench-global-0", "bench-global-1") if k % 2 == 0 else ("bench-global-1", "bench-global-0")
    return "replaceTagsInAllNotes", {"tag_to_replace": a, "replace_with_tag": b}, False

def g_set_due(ctx, row, idx, k, g):
    return "setDueDate", {"cards": chunk(ctx["content_cards"], idx, 20), "days": str(idx % 21)}, False

def g_set_ease(ctx, row, idx, k, g):
    ch = chunk(ctx["content_cards"], idx, 20)
    return "setEaseFactors", {"cards": ch, "easeFactors": [2000 + (idx % 15) * 50] * len(ch)}, False

def g_set_specific(ctx, row, idx, k, g):
    cid = ctx["content_cards"][idx % len(ctx["content_cards"])]
    return "setSpecificValueOfCard", {"card": cid, "keys": ["factor"],
                                      "newValues": [2300 + (idx % 10) * 40]}, False

def g_card_chunk_action(ctx, row, idx, k, g):
    return row["gargs"]["action"], {"cards": chunk(ctx["content_cards"], idx, 20)}, False

def g_answer(ctx, row, idx, k, g):
    cid = ctx["content_cards"][idx % len(ctx["content_cards"])]
    return "answerCards", {"answers": [{"cardId": cid, "ease": [3, 3, 4, 2][idx % 4]}]}, False

def g_change_deck(ctx, row, idx, k, g):
    ch = chunk(ctx["move_cards"], g, 20)        # slot-owned
    return "changeDeck", {"cards": ch, "deck": ctx["move_a"] if k % 2 == 0 else ctx["move_b"]}, False

def g_extend_limits(ctx, row, idx, k, g):
    return "extendCardLimits", {"deck": ctx["content_decks"][idx % 12], "new": 1, "review": 1}, True

def g_save_cfg(ctx, row, idx, k, g):
    return "saveDeckConfig", {"config": ctx["bench_cfg"]}, False

def g_set_cfg_id(ctx, row, idx, k, g):
    return "setDeckConfigId", {"decks": [ctx["content_decks"][idx % 12]],
                               "configId": ctx["bench_cfg_id"]}, False

def g_clone_cfg(ctx, row, idx, k, g):
    return "cloneDeckConfigId", {"name": f"bcfg-{idx}", "cloneFrom": ctx["bench_cfg_id"]}, False

def g_remove_cfg(ctx, row, idx, k, g):
    return "removeDeckConfigId", {"configId": ctx["cfg_by_idx"][str(idx)]}, False

def _mut_model(ctx, g):
    return ctx["mut_models"][g % 4]

def g_field_add(ctx, row, idx, k, g):
    return "modelFieldAdd", {"modelName": _mut_model(ctx, g), "fieldName": f"bf{k}", "index": 3}, False

def g_field_remove(ctx, row, idx, k, g):
    return "modelFieldRemove", {"modelName": _mut_model(ctx, g), "fieldName": f"bf{k}"}, False

def g_field_setdesc(ctx, row, idx, k, g):
    return "modelFieldSetDescription", {"modelName": _mut_model(ctx, g), "fieldName": "F1",
                                        "description": f"d{k}"}, False

def g_field_setfont(ctx, row, idx, k, g):
    return "modelFieldSetFont", {"modelName": _mut_model(ctx, g), "fieldName": "F1",
                                 "font": ["Arial", "Courier"][k % 2]}, False

def g_field_setsize(ctx, row, idx, k, g):
    return "modelFieldSetFontSize", {"modelName": _mut_model(ctx, g), "fieldName": "F1",
                                     "fontSize": 18 + (k % 6)}, False

def g_field_rename(ctx, row, idx, k, g):
    a, b = ("F2", "F2x") if k % 2 == 0 else ("F2x", "F2")
    return "modelFieldRename", {"modelName": _mut_model(ctx, g), "oldFieldName": a,
                                "newFieldName": b}, False

def g_field_repos(ctx, row, idx, k, g):
    return "modelFieldReposition", {"modelName": _mut_model(ctx, g), "fieldName": "F1",
                                    "index": k % 2}, False

def g_tmpl_add(ctx, row, idx, k, g):
    return "modelTemplateAdd", {"modelName": _mut_model(ctx, g),
                                "template": {"Name": f"bt{k}", "Front": "{{F1}} q" + str(k),
                                             "Back": "{{F3}} a"}}, False

def g_tmpl_remove(ctx, row, idx, k, g):
    return "modelTemplateRemove", {"modelName": _mut_model(ctx, g), "templateName": f"bt{k}"}, False

def g_tmpl_rename(ctx, row, idx, k, g):
    a, b = ("TR", "TRx") if k % 2 == 0 else ("TRx", "TR")
    return "modelTemplateRename", {"modelName": _mut_model(ctx, g), "oldTemplateName": a,
                                   "newTemplateName": b}, False

def g_tmpl_repos(ctx, row, idx, k, g):
    return "modelTemplateReposition", {"modelName": _mut_model(ctx, g), "templateName": "Card 1",
                                       "index": k % 2}, False

def g_upd_styling(ctx, row, idx, k, g):
    return "updateModelStyling", {"model": {"name": _mut_model(ctx, g),
                                            "css": f".card {{ font-size: 20px; }} /*tok{k % 2}*/"}}, False

def g_upd_templates(ctx, row, idx, k, g):
    return "updateModelTemplates", {"model": {"name": _mut_model(ctx, g), "templates": {
        "Card 1": {"Front": "{{F1}} <!--m%d-->" % (k % 2), "Back": "{{F1}}<hr>{{F3}}"}}}}, False

def g_find_replace_models(ctx, row, idx, k, g):
    return "findAndReplaceInModels", {"modelName": _mut_model(ctx, g),
                                      "findText": f"tok{k % 2}", "replaceText": f"tok{(k + 1) % 2}",
                                      "front": False, "back": False, "css": True}, False

def g_upd_note_model(ctx, row, idx, k, g):
    nid = ctx["note_ids"][idx % len(ctx["note_ids"])]
    m = ctx["bm_a"] if k % 2 == 0 else ctx["bm_b"]
    return "updateNoteModel", {"note": {"id": nid, "modelName": m,
                                        "fields": {"F1": f"m-{row['id']}-{idx}", "F2": "z", "F3": ""},
                                        "tags": ["bench-base"]}}, False

def g_insert_reviews(ctx, row, idx, k, g):
    # ids sit 1h in the future so they can never race the wall-clock id space that
    # answerCards & friends allocate from "now"; external purge removes them by range
    L = len(ctx["content_cards"])
    rows = []
    for j in range(5):
        rid = ctx["revbase"] + 3_600_000 + (idx * 5 + j) * 3
        cid = ctx["content_cards"][(idx * 5 + j) % L]
        rows.append([rid, cid, -1, (idx % 3) + 2, 1, 0, 2500, 3000 + j, 0])
    return "insertReviews", {"reviews": rows}, False

def g_reviews_of(ctx, row, idx, k, g):
    return "getReviewsOfCards", {"cards": chunk(ctx["content_cards"], idx, 20)}, False

def g_remove_dup_dry(ctx, row, idx, k, g):
    return "removeDuplicateNotes", {"deck": "AWBench::D03", "dryRun": True}, True

def g_export(ctx, row, idx, k, g):
    return "exportPackage", {"deck": "AWBench::D05",
                             "path": os.path.join(ctx["expdir"], f"exp-{idx}.apkg"),
                             "includeSched": False}, False

def g_import(ctx, row, idx, k, g):
    return "importPackage", {"path": ctx["apkg_path"]}, False

def g_delete_notes(ctx, row, idx, k, g):
    chunks = ctx["del_chunks"]
    return "deleteNotes", {"notes": chunks[min(idx, len(chunks) - 1)]}, False

def g_delete_deck(ctx, row, idx, k, g):
    return "deleteDecks", {"decks": [ctx["decks_delete_order"][idx]], "cardsToo": True}, False

def g_delete_model(ctx, row, idx, k, g):
    return "deleteModel", {"modelName": ctx["model_names"][idx]}, True

def g_mixed(ctx, row, idx, k, g):
    if g % 4 == 3:  # 8 of 32 slots write
        nid = ctx["note_ids"][(g * 997 + k) % len(ctx["note_ids"])]
        return "updateNoteFields", {"note": {"id": nid, "fields": {"F3": f"mx-{g}-{k}"}}}, False
    r = k % 3
    if r == 0:
        return "notesInfo", {"notes": chunk(ctx["note_ids"], idx, 20)}, False
    if r == 1:
        return "findCards", {"query": "deck:AWBench::D03"}, False
    return "cardsInfo", {"cards": chunk(ctx["content_cards"], idx, 10)}, False

GENS = {n[2:]: f for n, f in list(globals().items()) if n.startswith("g_") and callable(f)}

# ---------------------------------------------------------------- row table

def R(rid, gen, C, n, w, procs=None, items=1, phase="", kind="read", note="", gargs=None,
      capture=False, duration=None, annotate=False):
    procs = procs if procs is not None else (NWORKERS if C >= NWORKERS else C)
    conc = max(1, C // procs)
    return dict(id=rid, gen=gen, C=procs * conc, procs=procs, conc=conc, n=n, w=w,
                items=items, phase=phase, kind=kind, note=note, gargs=gargs or {},
                capture=capture, duration=duration, annotate=annotate)

def simple(rid, action, C, n, w, phase, kind="read", note="", extra=False, params=None, annotate=False):
    return R(rid, "simple", C, n, w, phase=phase, kind=kind, note=note, annotate=annotate,
             gargs={"action": action, "params": params or {}, "extra": extra})

def build_rows():
    rows = []
    A = rows.append
    # ---- meta
    A(R("httpProbe", "probe", 32, 960, 64, phase="meta", kind="meta", note="GET / 裸 HTTP 基线（客户端上限校准）"))
    A(simple("version", "version", 32, 960, 64, "meta", "meta"))
    A(R("multi(10xversion)", "multi", 16, 320, 16, items=10, phase="meta", kind="meta"))
    A(simple("requestPermission", "requestPermission", 32, 640, 32, "meta", "meta"))
    A(simple("apiReflect", "apiReflect", 32, 320, 32, "meta", "meta",
             params={"scopes": ["actions"], "actions": None}))
    A(simple("getProfiles", "getProfiles", 32, 480, 32, "meta", "meta"))
    A(simple("getActiveProfile", "getActiveProfile", 32, 480, 32, "meta", "meta"))
    A(simple("getNotifyConfig*", "getNotifyConfig", 32, 480, 32, "meta", "meta", extra=True))
    # ---- setup writes
    A(R("createModel", "create_model", 4, 24, 0, phase="setup", kind="write", capture=True, note="n小"))
    A(R("createDeck", "create_deck", 8, 80, 0, phase="setup", kind="write", capture=True))
    A(R("addNote", "add_note", 32, 512, 32, phase="setup", kind="write", capture=True))
    A(R("addNotes(100)", "add_notes", 8, 16, 0, phase="setup", kind="write", items=100, capture=True))
    A(R("canAddNote", "can_add", 32, 480, 32, phase="setup", kind="read",
        gargs={"action": "canAddNote"}))
    A(R("canAddNoteWithErrorDetail", "can_add", 32, 480, 32, phase="setup", kind="read",
        gargs={"action": "canAddNoteWithErrorDetail"}))
    A(R("canAddNotes(25)", "can_add_batch", 16, 160, 16, phase="setup", kind="read", items=25,
        gargs={"action": "canAddNotes"}))
    A(R("canAddNotesWithErrorDetail(25)", "can_add_batch", 16, 160, 16, phase="setup", kind="read",
        items=25, gargs={"action": "canAddNotesWithErrorDetail"}))
    A(R("storeMediaFile(4KB)", "store_media", 16, 240, 16, phase="setup", kind="write"))
    # ---- reads
    A(simple("deckNames", "deckNames", 32, 640, 32, "read"))
    A(simple("deckNamesAndIds", "deckNamesAndIds", 32, 640, 32, "read"))
    A(R("deckNameFromId", "deck_name_from_id", 32, 480, 32, phase="read"))
    A(R("getDecks(30)", "get_decks", 16, 240, 16, phase="read", items=30))
    A(R("getDeckConfig", "deck_param", 32, 480, 32, phase="read",
        gargs={"action": "getDeckConfig"}))
    A(R("getDeckStats(12)", "deck_stats", 16, 240, 16, phase="read", items=12))
    A(simple("modelNames", "modelNames", 32, 640, 32, "read"))
    A(simple("modelNamesAndIds", "modelNamesAndIds", 32, 640, 32, "read"))
    A(R("modelNameFromId", "model_name_from_id", 32, 480, 32, phase="read"))
    A(R("findModelsById", "find_models_by_id", 16, 240, 16, phase="read"))
    A(R("findModelsByName", "find_models_by_name", 16, 240, 16, phase="read"))
    for act in ("modelFieldNames", "modelStyling"):
        A(R(act, "model_read", 32, 480, 32, phase="read", gargs={"action": act}))
    for act in ("modelFieldDescriptions", "modelFieldFonts", "modelTemplates", "modelFieldsOnTemplates"):
        A(R(act, "model_read", 32, 320, 32, phase="read", gargs={"action": act}))
    A(R("findCards[deck]", "query", 32, 480, 32, phase="read",
        gargs={"action": "findCards", "query": "deck:AWBench::D03"}))
    A(R("findNotes[deck]", "query", 32, 480, 32, phase="read",
        gargs={"action": "findNotes", "query": "deck:AWBench::D03"}))
    A(R("findNotes[tree~2.5k]", "query", 16, 240, 16, phase="read",
        gargs={"action": "findNotes", "query": "deck:AWBench"}))
    A(R("findNotes[全库]", "query", 8, 64, 8, phase="read", annotate=True,
        note="全库扫描：底库大小不同(SQLite 8.2k vs PG 21.9k notes)",
        gargs={"action": "findNotes", "query": "-tag:zzzneverxyz"}))
    A(R("notesInfo[1]", "single_id", 32, 480, 32, phase="read",
        gargs={"action": "notesInfo", "pool": "note_ids", "key": "notes", "aslist": True}))
    A(R("notesInfo[100]", "ids_batch", 16, 160, 16, phase="read", items=100,
        gargs={"action": "notesInfo", "pool": "note_ids", "key": "notes", "size": 100}))
    A(R("cardsInfo[1]", "single_id", 32, 480, 32, phase="read",
        gargs={"action": "cardsInfo", "pool": "content_cards", "key": "cards", "aslist": True}))
    A(R("cardsInfo[50]", "ids_batch", 8, 96, 8, phase="read", items=50,
        gargs={"action": "cardsInfo", "pool": "content_cards", "key": "cards", "size": 50}))
    for act, pool, key in (("cardsToNotes", "content_cards", "cards"),
                           ("cardsModTime", "content_cards", "cards"),
                           ("areSuspended", "content_cards", "cards"),
                           ("getIntervals", "content_cards", "cards"),
                           ("getEaseFactors", "content_cards", "cards"),
                           ("notesModTime", "note_ids", "notes")):
        A(R(f"{act}(50)", "ids_batch", 32, 320, 32, phase="read", items=50,
            gargs={"action": act, "pool": pool, "key": key, "size": 50}))
    A(R("areDue(5)", "ids_batch", 16, 64, 16, phase="read", items=5, annotate=True,
        note="上游 O(n) 实现：每卡两次全库搜索",
        gargs={"action": "areDue", "pool": "content_cards", "key": "cards", "size": 5}))
    A(R("suspended[1]", "single_id", 32, 480, 32, phase="read",
        gargs={"action": "suspended", "pool": "content_cards", "key": "card"}))
    A(R("getNoteTags[1]", "single_id", 32, 480, 32, phase="read",
        gargs={"action": "getNoteTags", "pool": "note_ids", "key": "note"}))
    A(simple("getTags", "getTags", 32, 480, 32, "read"))
    A(simple("getMediaDirPath", "getMediaDirPath", 32, 480, 32, "read"))
    A(simple("getMediaFilesNames", "getMediaFilesNames", 8, 96, 8, "read",
             params={"pattern": "bench-*"}))
    A(R("retrieveMediaFile(4KB)", "retrieve_media", 16, 240, 16, phase="read"))
    A(R("removeDuplicateNotes[dry]*", "remove_dup_dry", 8, 64, 8, phase="read"))
    # ---- writes
    A(R("updateNoteFields", "upd_fields", 32, 320, 32, phase="write", kind="write"))
    A(R("updateNote", "upd_note", 16, 240, 16, phase="write", kind="write"))
    A(R("updateNoteTags", "upd_tags", 16, 240, 16, phase="write", kind="write"))
    A(R("addTags(20)", "add_tags", 16, 240, 16, phase="write", kind="write", items=20))
    A(R("removeTags(20)", "remove_tags", 16, 240, 16, phase="write", kind="write", items=20))
    A(R("replaceTags(20)", "replace_tags", 16, 160, 16, phase="write", kind="write", items=20))
    A(R("setDueDate(20)", "set_due", 16, 240, 16, phase="write", kind="write", items=20))
    A(R("setEaseFactors(20)", "set_ease", 16, 240, 16, phase="write", kind="write", items=20))
    A(R("setSpecificValueOfCard", "set_specific", 32, 320, 32, phase="write", kind="write"))
    A(R("suspend(20)", "card_chunk_action", 16, 192, 16, phase="write", kind="write", items=20,
        gargs={"action": "suspend"}))
    A(R("unsuspend(20)", "card_chunk_action", 16, 192, 16, phase="write", kind="write", items=20,
        gargs={"action": "unsuspend"}))
    A(R("forgetCards(20)", "card_chunk_action", 16, 192, 16, phase="write", kind="write", items=20,
        gargs={"action": "forgetCards"}))
    A(R("relearnCards(20)", "card_chunk_action", 16, 192, 16, phase="write", kind="write", items=20,
        gargs={"action": "relearnCards"}))
    A(R("answerCards[1]", "answer", 32, 256, 32, phase="write", kind="write"))
    A(R("changeDeck(20)", "change_deck", 16, 160, 16, phase="write", kind="write", items=20))
    A(R("extendCardLimits*", "extend_limits", 8, 96, 8, phase="write", kind="write"))
    A(R("saveDeckConfig", "save_cfg", 4, 96, 8, phase="write", kind="write"))
    A(R("setDeckConfigId", "set_cfg_id", 4, 96, 8, phase="write", kind="write"))
    A(R("cloneDeckConfigId", "clone_cfg", 4, 48, 4, phase="write", kind="write", capture=True))
    A(R("removeDeckConfigId", "remove_cfg", 4, 48, 4, phase="write", kind="write"))
    A(R("modelFieldAdd", "field_add", 4, 96, 4, phase="write", kind="write"))
    A(R("modelFieldSetDescription", "field_setdesc", 4, 96, 4, phase="write", kind="write"))
    A(R("modelFieldSetFont", "field_setfont", 4, 96, 4, phase="write", kind="write"))
    A(R("modelFieldSetFontSize", "field_setsize", 4, 96, 4, phase="write", kind="write"))
    A(R("modelFieldRename", "field_rename", 4, 64, 4, phase="write", kind="write"))
    A(R("modelFieldReposition", "field_repos", 4, 96, 4, phase="write", kind="write"))
    A(R("modelFieldRemove", "field_remove", 4, 96, 4, phase="write", kind="write"))
    A(R("modelTemplateAdd", "tmpl_add", 4, 64, 4, phase="write", kind="write"))
    A(R("modelTemplateRename", "tmpl_rename", 4, 48, 4, phase="write", kind="write"))
    A(R("modelTemplateReposition", "tmpl_repos", 4, 64, 4, phase="write", kind="write"))
    A(R("modelTemplateRemove", "tmpl_remove", 4, 64, 4, phase="write", kind="write"))
    A(R("updateModelStyling", "upd_styling", 4, 96, 4, phase="write", kind="write"))
    A(R("updateModelTemplates", "upd_templates", 4, 96, 4, phase="write", kind="write"))
    A(R("findAndReplaceInModels", "find_replace_models", 4, 48, 4, phase="write", kind="write"))
    A(R("updateNoteModel", "upd_note_model", 16, 160, 16, phase="write", kind="write"))
    A(R("insertReviews(5)", "insert_reviews", 16, 160, 16, phase="write", kind="write", items=5))
    # ---- stats reads (after writes so revlog exists)
    A(simple("getNumCardsReviewedToday", "getNumCardsReviewedToday", 32, 480, 32, "stats"))
    A(simple("getNumCardsReviewedByDay", "getNumCardsReviewedByDay", 16, 240, 16, "stats",
             annotate=True, note="按天聚合全部 revlog：底库不同(SQLite 50.9k vs PG 0.1k)"))
    A(R("cardReviews", "deck_param", 16, 240, 16, phase="stats",
        gargs={"action": "cardReviews", "deck": "AWBench::D00", "params": {"startID": 0}}))
    A(R("getReviewsOfCards(20)", "reviews_of", 16, 240, 16, phase="stats", items=20))
    A(R("getLatestReviewID", "deck_param", 16, 240, 16, phase="stats",
        gargs={"action": "getLatestReviewID", "deck": "AWBench::D00"}))
    A(simple("getCollectionStatsHTML", "getCollectionStatsHTML", 4, 24, 4, "stats",
             params={"wholeCollection": True}, annotate=True, note="全库统计渲染：底库不同"))
    # ---- global writes
    A(R("replaceTagsInAllNotes", "replace_all_tags", 1, 16, 2, procs=1, phase="global",
        kind="write", annotate=True, note="全库扫描写"))
    A(simple("clearUnusedTags", "clearUnusedTags", 4, 40, 4, "global", "write"))
    # ---- export / import
    A(R("exportPackage(~280notes)", "export", 2, 8, 0, procs=2, phase="export", kind="write",
        annotate=True, note="n小"))
    A(R("importPackage(同guid去重)", "import", 1, 4, 0, procs=1, phase="export", kind="write",
        annotate=True, note="n小"))
    # ---- mixed workload
    A(R("mixed(24读+8写)", "mixed", 32, 0, 0, phase="mixed", kind="mixed", duration=30))
    # ---- concurrency sweep
    A(R("version@C1", "simple", 1, 128, 16, procs=1, phase="sweep", kind="meta",
        gargs={"action": "version"}))
    A(R("version@C8", "simple", 8, 480, 32, phase="sweep", kind="meta",
        gargs={"action": "version"}))
    A(R("findCards[deck]@C1", "query", 1, 96, 8, procs=1, phase="sweep",
        gargs={"action": "findCards", "query": "deck:AWBench::D03"}))
    A(R("findCards[deck]@C8", "query", 8, 320, 16, phase="sweep",
        gargs={"action": "findCards", "query": "deck:AWBench::D03"}))
    A(R("notesInfo[100]@C1", "ids_batch", 1, 48, 4, procs=1, phase="sweep", items=100,
        gargs={"action": "notesInfo", "pool": "note_ids", "key": "notes", "size": 100}))
    A(R("notesInfo[100]@C8", "ids_batch", 8, 96, 8, phase="sweep", items=100,
        gargs={"action": "notesInfo", "pool": "note_ids", "key": "notes", "size": 100}))
    A(R("addNote@C1", "add_note", 1, 96, 8, procs=1, phase="sweep", kind="write"))
    A(R("addNote@C8", "add_note", 8, 320, 16, phase="sweep", kind="write"))
    A(R("updateNoteFields@C1", "upd_fields", 1, 96, 8, procs=1, phase="sweep", kind="write"))
    A(R("updateNoteFields@C8", "upd_fields", 8, 320, 16, phase="sweep", kind="write"))
    A(R("answerCards@C1", "answer", 1, 96, 8, procs=1, phase="sweep", kind="write"))
    A(R("answerCards@C8", "answer", 8, 192, 16, phase="sweep", kind="write"))
    # ---- cleanup (measured)
    A(R("deleteMediaFile", "delete_media", 16, 240, 16, phase="cleanup", kind="write"))
    A(R("deleteNotes(50)", "delete_notes", 8, 0, 0, phase="cleanup", kind="write", items=50))
    A(R("deleteDecks[1]", "delete_deck", 8, 80, 0, phase="cleanup", kind="write"))
    A(R("deleteModel*", "delete_model", 4, 24, 0, phase="cleanup", kind="write"))
    # normalize n to a multiple of T
    for r in rows:
        T = r["procs"] * r["conc"]
        if r["duration"] is None and r["n"] % T != 0:
            r["n"] = int(math.ceil(r["n"] / T) * T)
        r["w"] = int(math.ceil(r["w"] / T) * T) if r["w"] else 0
    return rows

# ---------------------------------------------------------------- worker proc

def worker_main(proc_id, jobq, resq, barrier):
    while True:
        job = jobq.get()
        if job == "STOP":
            return
        try:
            out = asyncio.run(worker_row(proc_id, job, barrier))
        except Exception as e:
            try:
                barrier.abort()
            except Exception:
                pass
            out = {"proc": proc_id, "fatal": repr(e), "samples": [], "captures": [],
                   "errors": [["<fatal>", -1, repr(e)]], "t0": 0, "t1": 0}
        resq.put(out)


async def worker_row(proc_id, job, barrier):
    row, ctx = job["row"], job["ctx"]
    active = proc_id < row["procs"]
    conc = row["conc"]
    T = row["procs"] * conc
    gen = GENS[row["gen"]]
    samples, captures, errors = [], [], []
    loop = asyncio.get_running_loop()

    if not active:
        await loop.run_in_executor(None, barrier.wait)
        return {"proc": proc_id, "samples": [], "captures": [], "errors": [], "t0": 0, "t1": 0}

    limits = httpx.Limits(max_connections=conc, max_keepalive_connections=conc)
    timeout = httpx.Timeout(300.0, connect=30.0)
    async with httpx.AsyncClient(base_url=BASE, limits=limits, timeout=timeout) as client:

        async def one_call(idx, k, g, measured):
            try:
                action, params, extra = gen(ctx, row, idx, k, g)
            except Exception as e:
                if len(errors) < 5:
                    errors.append(["<gen>", idx, repr(e)])
                samples.append((time.time(), 0.0, False, "<gen>"))
                return
            ts = time.time()
            t0 = time.perf_counter()
            ok, err, res = True, None, None
            try:
                if action == "__probe__":
                    r = await client.get("/")
                    ok = r.status_code == 200
                elif extra:
                    r = await client.post(f"/extra_actions/{action}", json=params,
                                          headers={"X-API-Key": KEY})
                    j = r.json()
                    err = j.get("error") if r.status_code == 200 else f"http {r.status_code}"
                    ok, res = err is None, j.get("result")
                else:
                    r = await client.post("/", json={"action": action, "version": 6,
                                                     "key": KEY, "params": params})
                    j = r.json()
                    err = j.get("error")
                    ok, res = err is None, j.get("result")
            except Exception as e:
                ok, err = False, repr(e)
            dur = time.perf_counter() - t0
            if measured:
                samples.append((ts, dur, ok, action))
            if not ok and len(errors) < 5:
                errors.append([action, idx, str(err)[:300]])
            if row["capture"] and ok:
                captures.append([idx, res])

        async def slot(s, k0, k1, deadline=None):
            gg = proc_id * conc + s
            k = k0
            while True:
                if deadline is not None:
                    if time.time() >= deadline:
                        break
                elif k >= k1:
                    break
                await one_call(gg + T * k, k, gg, measured=(deadline is not None or k >= job["w_slot"]))
                k += 1

        w_slot, n_slot = job["w_slot"], job["n_slot"]
        if row["duration"] is None and w_slot:
            await asyncio.gather(*(slot(s, 0, w_slot) for s in range(conc)))
        await loop.run_in_executor(None, barrier.wait)
        t0 = time.time()
        if row["duration"] is not None:
            deadline = t0 + row["duration"] + 3.0
            await asyncio.gather(*(slot(s, 0, None, deadline=deadline) for s in range(conc)))
        else:
            await asyncio.gather(*(slot(s, w_slot, w_slot + n_slot) for s in range(conc)))
        t1 = time.time()
    return {"proc": proc_id, "samples": samples, "captures": captures, "errors": errors,
            "t0": t0, "t1": t1}

# ---------------------------------------------------------------- master

class Master:
    def __init__(self, label, smoke):
        self.label, self.smoke = label, smoke
        self.http = httpx.Client(base_url=BASE, timeout=120.0)
        self.ctx = {}
        self.results = []
        self.jobqs = [mp.Queue() for _ in range(NWORKERS)]
        self.resq = mp.Queue()
        self.barrier = mp.Barrier(NWORKERS + 1)
        self.procs = [mp.Process(target=worker_main, args=(i, self.jobqs[i], self.resq, self.barrier),
                                 daemon=True) for i in range(NWORKERS)]
        for p in self.procs:
            p.start()

    # -- one-off unmeasured AC call
    def call(self, action, params=None, extra=False):
        if extra:
            r = self.http.post(f"/extra_actions/{action}", json=params or {},
                               headers={"X-API-Key": KEY})
        else:
            r = self.http.post("/", json={"action": action, "version": 6, "key": KEY,
                                          "params": params or {}})
        j = r.json()
        if j.get("error"):
            raise RuntimeError(f"{action}: {j['error']}")
        return j.get("result")

    def snapshot(self):
        return {
            "notes": len(self.call("findNotes", {"query": "-tag:zzzneverxyz"})),
            "decks": len(self.call("deckNames")),
            "models": len(self.call("modelNames")),
            "tags": len(self.call("getTags")),
            "media_bench": len(self.call("getMediaFilesNames", {"pattern": "bench-*"})),
            "reviewedToday": self.call("getNumCardsReviewedToday"),
        }

    def purge_leftovers(self):
        decks = [d for d in self.call("deckNames") if d == "AWBench" or d.startswith("AWBench::")]
        if decks:
            print(f"  [purge] leftover decks: {len(decks)}", flush=True)
            self.call("deleteDecks", {"decks": decks, "cardsToo": True})
        for m in self.call("modelNames"):
            if m.startswith("AWB-"):
                try:
                    self.call("deleteModel", {"modelName": m}, extra=True)
                except Exception as e:
                    print(f"  [purge] model {m}: {e}", flush=True)
        for f in self.call("getMediaFilesNames", {"pattern": "bench-*"}):
            self.call("deleteMediaFile", {"filename": f})
        # normalize the tag registry BEFORE the baseline snapshot, so the bench's own
        # clearUnusedTags calls can't be blamed for removing the user's unused tags
        self.call("clearUnusedTags")

    def server_cpu(self):
        try:
            pids = []
            for pid in subprocess.run(["pgrep", "-f", "python -m ankiweb"], capture_output=True,
                                      text=True).stdout.split():
                try:
                    with open(f"/proc/{pid}/environ", "rb") as fh:
                        if b"ANKIWEB_AC_PORT=18765" in fh.read():
                            pids.append(pid)
                except Exception:
                    pass
            tck = os.sysconf("SC_CLK_TCK")
            cpu = 0.0
            rss = 0
            for pid in pids:
                try:
                    with open(f"/proc/{pid}/stat") as fh:
                        f = fh.read().rsplit(")", 1)[1].split()
                    cpu += (int(f[11]) + int(f[12])) / tck
                    with open(f"/proc/{pid}/status") as fh:
                        for line in fh:
                            if line.startswith("VmRSS"):
                                rss += int(line.split()[1])
                except Exception:
                    pass
            return {"pids": len(pids), "cpu_s": round(cpu, 2), "rss_kb": rss}
        except Exception as e:
            return {"error": repr(e)}

    def run_row(self, row):
        T = row["procs"] * row["conc"]
        w_slot = row["w"] // T if T else 0
        n_slot = row["n"] // T if T else 0
        job = {"row": row, "ctx": self.ctx, "w_slot": w_slot, "n_slot": n_slot}
        for q in self.jobqs:
            q.put(job)
        # release the measured phase once every proc is warmed and parked
        self.barrier.wait()
        outs = [self.resq.get() for _ in range(NWORKERS)]
        outs.sort(key=lambda o: o["proc"])
        fatal = [o for o in outs if o.get("fatal")]
        if fatal:
            raise RuntimeError(f"worker fatal in row {row['id']}: {fatal[0]['fatal']}")
        active = [o for o in outs if o["t1"] > 0]
        samples = [s for o in outs for s in o["samples"]]
        captures = sorted((c for o in outs for c in o["captures"]), key=lambda c: c[0])
        errors = [e for o in outs for e in o["errors"]][:8]
        rec = {"id": row["id"], "phase": row["phase"], "kind": row["kind"], "C": row["C"],
               "procs": row["procs"], "items": row["items"], "annotate": row["annotate"],
               "note": row["note"]}
        ok_s = [s for s in samples if s[2]]
        rec["n"] = len(samples)
        rec["ok"] = len(ok_s)
        rec["err_n"] = len(samples) - len(ok_s)
        rec["errors"] = errors
        if active:
            wall = max(o["t1"] for o in active) - min(o["t0"] for o in active)
            rec["wall"] = round(wall, 3)
            lat = sorted(s[1] for s in ok_s)
            if lat:
                rec["rps"] = round(len(ok_s) / wall, 2) if wall > 0 else None
                rec["items_ps"] = round(rec["rps"] * row["items"], 1) if rec["rps"] else None
                rec["lat_ms"] = {
                    "mean": round(1000 * statistics.fmean(lat), 2),
                    "p50": round(1000 * pct(lat, 0.50), 2),
                    "p90": round(1000 * pct(lat, 0.90), 2),
                    "p95": round(1000 * pct(lat, 0.95), 2),
                    "p99": round(1000 * pct(lat, 0.99), 2),
                    "max": round(1000 * lat[-1], 2),
                }
        self.results.append(rec)
        msg = f"  [{row['phase']:>7}] {row['id']:<32} C={row['C']:<3} n={rec['ok']}/{rec['n']}"
        if rec.get("rps"):
            msg += f" rps={rec['rps']:<8} p50={rec['lat_ms']['p50']}ms p95={rec['lat_ms']['p95']}ms"
        if rec["err_n"]:
            msg += f"  !!ERR {rec['err_n']}: {errors[:2]}"
        print(msg, flush=True)
        return captures

    def close(self):
        for q in self.jobqs:
            q.put("STOP")
        for p in self.procs:
            p.join(timeout=10)
        self.http.close()

# ---------------------------------------------------------------- main flow

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()

    rows = build_rows()
    if args.smoke:
        for r in rows:
            T = r["procs"] * r["conc"]
            if r["duration"] is not None:
                r["duration"] = 5
            elif r["gen"] in ("create_model", "create_deck", "delete_deck", "delete_model",
                              "add_notes", "export", "import"):
                pass  # exact-list rows keep their n
            else:
                r["n"] = min(r["n"], 2 * T)
                r["w"] = min(r["w"], T)

    m = Master(args.label, args.smoke)
    t_start = time.time()
    run_start_ms = int(t_start * 1000)
    meta = {"label": args.label, "smoke": args.smoke, "base": BASE,
            "started": time.strftime("%F %T"), "run_start_ms": run_start_ms}

    # -- readiness & environment
    meta["apiVersion"] = m.call("version")
    meta["profile"] = m.call("getActiveProfile")
    sockets = subprocess.run(["bash", "-c", "ss -tln | grep -c ':18765 '"],
                             capture_output=True, text=True).stdout.strip()
    meta["listen_sockets_18765"] = sockets
    print(f"== acbench label={args.label} smoke={args.smoke} apiVersion={meta['apiVersion']} "
          f"sockets={sockets}", flush=True)

    m.purge_leftovers()
    baseline = m.snapshot()
    meta["baseline"] = baseline
    meta["cpu0"] = m.server_cpu()
    print(f"  baseline: {baseline}", flush=True)

    ctx = m.ctx
    ctx["model_names"] = (["AWB-BM-A", "AWB-BM-B"] + [f"AWB-M{i}" for i in range(4)]
                          + [f"AWB-S{i:02d}" for i in range(18)])
    ctx["bm_a"], ctx["bm_b"] = "AWB-BM-A", "AWB-BM-B"
    ctx["mut_models"] = [f"AWB-M{i}" for i in range(4)]
    ctx["content_decks"] = [f"AWBench::D{i:02d}" for i in range(12)]
    ctx["move_a"], ctx["move_b"] = "AWBench::Move-A", "AWBench::Move-B"
    ctx["move_pool_deck"] = "AWBench::MovePool"
    ctx["deck_names_all"] = (ctx["content_decks"] + [ctx["move_a"], ctx["move_b"],
                             ctx["move_pool_deck"]] + [f"AWBench::Tmp::T{i:02d}" for i in range(65)])
    ctx["decks_delete_order"] = ([f"AWBench::Tmp::T{i:02d}" for i in range(65)]
                                 + ctx["content_decks"]
                                 + [ctx["move_a"], ctx["move_b"], ctx["move_pool_deck"]])
    ctx["expdir"] = os.path.join(SCRATCH, "exports", args.label)
    os.makedirs(ctx["expdir"], exist_ok=True)
    ctx["note_ids"] = []
    ctx["content_cards"] = []
    ctx["move_cards"] = []

    rows_by_id = {r["id"]: r for r in rows}

    def after_create_model(captures):
        ctx["model_ids"] = {}
        for idx, res in captures:
            ctx["model_ids"][ctx["model_names"][idx]] = res["id"] if isinstance(res, dict) else res

    def after_create_deck(captures):
        # a worker losing the parent-deck creation race (PG multi-worker, E23505) fails
        # that one call; retry the losers unmeasured so the tree is always complete
        got = dict(captures)
        ctx["deck_ids"] = {}
        for idx, name in enumerate(ctx["deck_names_all"]):
            ctx["deck_ids"][name] = got[idx] if idx in got else m.call("createDeck", {"deck": name})
        # bench deck-config preset: identical scheduling params on every server
        cfg_id = m.call("cloneDeckConfigId", {"name": "AWBenchPreset", "cloneFrom": 1})
        m.call("setDeckConfigId", {"decks": ctx["content_decks"] + [ctx["move_a"], ctx["move_b"],
                                   ctx["move_pool_deck"]], "configId": cfg_id})
        cfg = m.call("getDeckConfig", {"deck": "AWBench::D00"})
        if not isinstance(cfg, dict):
            raise RuntimeError(f"getDeckConfig(AWBench::D00) -> {cfg!r}")
        cfg["new"]["perDay"] = 1000
        cfg["rev"]["perDay"] = 1000
        cfg["new"]["delays"] = [1.0, 10.0]
        m.call("saveDeckConfig", {"config": cfg})
        ctx["bench_cfg_id"] = cfg_id
        ctx["bench_cfg"] = m.call("getDeckConfig", {"deck": "AWBench::D00"})

    def after_add_note(captures):
        ctx["note_ids"].extend(res for _, res in captures if isinstance(res, int))

    def after_add_notes(captures):
        for _, res in captures:
            ctx["note_ids"].extend(i for i in (res or []) if isinstance(i, int))
        ctx["content_cards"] = m.call("findCards", {"query": "deck:AWBench::D*"})
        ctx["move_cards"] = m.call("findCards", {"query": "deck:AWBench::MovePool"})
        m.call("storeMediaFile", {"filename": "bench-fixed.txt", "data": PAY4K})
        m.call("addTags", {"notes": ctx["note_ids"][:50], "tags": "bench-global-0"})
        print(f"  pools: notes={len(ctx['note_ids'])} contentCards={len(ctx['content_cards'])} "
              f"moveCards={len(ctx['move_cards'])}", flush=True)

    def before_insert_reviews():
        ctx["revbase"] = int(time.time() * 1000)

    def before_import():
        ctx["apkg_path"] = os.path.join(ctx["expdir"], "exp-0.apkg")

    def before_delete_notes():
        live = m.call("findNotes", {"query": "deck:AWBench"})
        chunks = [live[i:i + 50] for i in range(0, len(live), 50)] or [[]]
        row = rows_by_id["deleteNotes(50)"]
        T = row["procs"] * row["conc"]
        while len(chunks) % T:
            chunks.append(chunks[-1])
        ctx["del_chunks"] = chunks
        row["n"] = len(chunks)
        print(f"  cleanup: {len(live)} bench notes in {len(chunks)} chunks", flush=True)

    pre_hooks = {"insertReviews(5)": before_insert_reviews,
                 "importPackage(同guid去重)": before_import,
                 "deleteNotes(50)": before_delete_notes}
    post_hooks = {"createModel": after_create_model, "createDeck": after_create_deck,
                  "addNote": after_add_note, "addNotes(100)": after_add_notes}

    mixed_out = None
    for row in rows:
        if row["duration"] is not None:
            mixed_out = run_mixed_row(m, row)
            continue
        if row["id"] in pre_hooks:
            pre_hooks[row["id"]]()
        captures = m.run_row(row)
        if row["id"] in post_hooks:
            post_hooks[row["id"]](captures)
        if row["id"] == "cloneDeckConfigId":
            ctx["cfg_by_idx"] = {str(idx): res for idx, res in captures}

    # -- final sweep + verification
    try:
        m.call("deleteDecks", {"decks": ["AWBench"], "cardsToo": True})
    except Exception as e:
        print(f"  [cleanup] parent deck: {e}", flush=True)
    try:
        m.call("deleteMediaFile", {"filename": "bench-fixed.txt"})
        m.call("removeDeckConfigId", {"configId": ctx["bench_cfg_id"]})
        m.call("clearUnusedTags")
    except Exception as e:
        print(f"  [cleanup] {e}", flush=True)
    final = m.snapshot()
    meta["final"] = final
    meta["cpu1"] = m.server_cpu()
    meta["cleanup_ok"] = all(final[k] == baseline[k] for k in ("notes", "decks", "models", "tags"))
    meta["wall_total_s"] = round(time.time() - t_start, 1)
    print(f"  final: {final}  cleanup_ok={meta['cleanup_ok']}", flush=True)

    out = {"meta": meta, "rows": m.results, "mixed": mixed_out}
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"== done in {meta['wall_total_s']}s -> {args.out}", flush=True)
    m.close()


def run_mixed_row(m, row):
    """Duration-mode mixed workload: slots with g%4==3 issue updateNoteFields (writes),
    the rest cycle notesInfo/findCards/cardsInfo (reads). Samples carry the action name,
    so the read/write split is recovered from the per-sample tags; the first 3s after
    the barrier are treated as warmup and trimmed via the measurement window."""
    dur_s = row["duration"]
    job = {"row": row, "ctx": m.ctx, "w_slot": 0, "n_slot": 0}
    for q in m.jobqs:
        q.put(job)
    m.barrier.wait()
    outs = [m.resq.get() for _ in range(NWORKERS)]
    fatal = [o for o in outs if o.get("fatal")]
    if fatal:
        raise RuntimeError(f"worker fatal in mixed: {fatal[0]['fatal']}")
    active = [o for o in outs if o["t1"] > 0]
    t0 = min(o["t0"] for o in active)
    win0, win1 = t0 + 3.0, t0 + 3.0 + dur_s
    kinds = {}
    err_n = 0
    for o in outs:
        for ts, dur, ok, action in o["samples"]:
            if not (win0 <= ts < win1):
                continue
            if not ok:
                err_n += 1
                continue
            kind = "write" if action == "updateNoteFields" else "read"
            kinds.setdefault(kind, []).append(dur)
    res = {"window_s": dur_s, "C": row["C"], "err_n": err_n, "note": "24 读 slot + 8 写 slot"}
    for kind, durs in kinds.items():
        durs.sort()
        res[kind] = {"rps": round(len(durs) / dur_s, 2),
                     "p50_ms": round(1000 * pct(durs, 0.5), 2),
                     "p95_ms": round(1000 * pct(durs, 0.95), 2),
                     "n": len(durs)}
    m.results.append({"id": row["id"], "phase": "mixed", "kind": "mixed", "C": row["C"],
                      "mixed": res, "n": sum(len(d) for d in kinds.values()),
                      "ok": sum(len(d) for d in kinds.values()), "err_n": err_n,
                      "annotate": False, "note": ""})
    print(f"  [  mixed] {res}", flush=True)
    return res


if __name__ == "__main__":
    mp.set_start_method("fork")
    main()
