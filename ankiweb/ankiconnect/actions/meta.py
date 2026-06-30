from __future__ import annotations
from ankiweb.ankiconnect.registry import action, ACTIONS
from ankiweb.ankiconnect.schemas.meta import (
    VersionParams, ApiReflectParams, RequestPermissionParams, ReloadCollectionParams,
    GetProfilesParams, GetActiveProfileParams, LoadProfileParams, SyncParams,
)


@action("version", params=VersionParams, returns=int, summary="Get the API version")
async def version(rt):
    return 6


@action("apiReflect", params=ApiReflectParams, summary="List available actions")
async def api_reflect(rt, scopes=None, actions=None):
    scopes = scopes or []
    out = {"scopes": [], "actions": []}
    if "actions" in scopes:
        out["scopes"] = ["actions"]
        names = sorted(ACTIONS.keys()) + ["multi"]
        if actions is not None:
            names = [n for n in names if n in actions]
        out["actions"] = names
    return out


@action("requestPermission", params=RequestPermissionParams, summary="Request API permission")
async def request_permission(rt, allowed=False, origin=None):
    # CORS result is injected by the app. Single-user local → auto-grant when allowed.
    if not allowed:
        return {"permission": "denied"}
    return {"permission": "granted",
            "requireApikey": rt.config.api_key is not None,
            "version": 6}


@action("reloadCollection", params=ReloadCollectionParams, summary="Reload the collection")
async def reload_collection(rt):
    # col.reset() is a deprecated no-op in anki 25.9.4; the single shared collection is
    # always live, so there's nothing to reload. Return None (AnkiConnect returns null).
    return None


@action("_testSetConfig", summary="TEST-ONLY: set a collection config key (e.g. enable FSRS)")
async def _test_set_config(rt, key=None, value=None):
    # Loose-params test hook used by the PG-vs-SQLite scheduling faithfulness sim to
    # enable FSRS (`fsrs`=true) before any reviews. Not part of the AnkiConnect API.
    def fn(col):
        col.set_config(key, value)
        return None
    await rt.service.run(fn)
    return None


@action("_testCardState", summary="TEST-ONLY: full scheduling state of cards for comparison")
async def _test_card_state(rt, cards=None):
    # Returns each card's scheduler/FSRS state (memory state s/d if FSRS, plus ivl/due/
    # reps/lapses/factor/type/queue) keyed by note content, so a PG run and a SQLite run
    # can be compared field-by-field independent of differing absolute ids.
    cards = cards or []

    def fn(col):
        out = []
        for cid in cards:
            try:
                c = col.get_card(cid)
            except Exception:
                out.append(None)
                continue
            ms = None
            try:
                m = c.memory_state
                if m is not None:
                    ms = {"stability": round(m.stability, 4), "difficulty": round(m.difficulty, 4)}
            except Exception:
                ms = None
            note = c.note()
            out.append({
                "key": [note.note_type()["name"], list(note.fields), c.ord],
                "type": int(c.type), "queue": int(c.queue), "due": c.due,
                "ivl": c.ivl, "factor": c.factor, "reps": c.reps, "lapses": c.lapses,
                "memory": ms,
            })
        return out
    return await rt.service.run(fn)


@action("getProfiles", params=GetProfilesParams, returns=list[str], summary="List profile names")
async def get_profiles(rt):
    return ["User 1"]


@action("getActiveProfile", params=GetActiveProfileParams, returns=str,
        summary="Get the active profile name")
async def get_active_profile(rt):
    return "User 1"


@action("loadProfile", params=LoadProfileParams, returns=bool, summary="Select a profile")
async def load_profile(rt, name=None):
    return True


@action("sync", params=SyncParams, summary="Synchronize the collection")
async def sync(rt):
    raise Exception("sync is not supported by ankiweb")
