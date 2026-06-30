from __future__ import annotations
import re
from typing import Optional
from anki.utils import split_fields
from ankiweb.ankiconnect.registry import action
from ankiweb.ankiconnect.actions._helpers import run_emit, build_note, check_addable, in_chunks
from ankiweb.ankiconnect.actions.media import attach_media
from ankiweb.ankiconnect.schemas.notes import (
    AddNoteParams, CanAddNoteParams, CanAddNoteWithErrorDetailParams, AddNotesParams,
    CanAddNotesParams, CanAddNotesWithErrorDetailParams, FindNotesParams, NotesInfoParams,
    UpdateNoteFieldsParams, UpdateNoteTagsParams, GetNoteTagsParams, UpdateNoteParams,
    UpdateNoteModelParams, AddTagsParams, RemoveTagsParams, GetTagsParams, ClearUnusedTagsParams,
    ReplaceTagsParams, ReplaceTagsInAllNotesParams, NotesModTimeParams, DeleteNotesParams,
    RemoveEmptyNotesParams, CardsToNotesParams,
)


@action("addNote", params=AddNoteParams, returns=Optional[int], summary="Create a single note")
async def add_note(rt, note=None):
    spec = note or {}

    def fn(col):
        attach_media(col, spec)
        n, _ = build_note(col, spec)
        ok, err = check_addable(col, n, spec.get("options"))
        if not ok:
            raise Exception(err)
        did = col.decks.id(spec.get("deckName", "Default"))
        res = col.add_note(n, did)
        return n.id, res
    return await run_emit(rt, fn)


@action("canAddNote", params=CanAddNoteParams, returns=bool, summary="Can a note be added")
async def can_add_note(rt, note=None):
    spec = note or {}

    def fn(col):
        try:
            n, _ = build_note(col, spec)
            ok, _err = check_addable(col, n, spec.get("options"))
            return ok
        except Exception:
            return False
    return await rt.service.run(fn)


@action("canAddNoteWithErrorDetail", params=CanAddNoteWithErrorDetailParams,
        summary="Can a note be added (with error detail)")
async def can_add_note_with_error_detail(rt, note=None):
    spec = note or {}

    def fn(col):
        try:
            n, _ = build_note(col, spec)
            ok, err = check_addable(col, n, spec.get("options"))
            return {"canAdd": ok} if ok else {"canAdd": False, "error": err}
        except Exception as exc:
            return {"canAdd": False, "error": str(exc)}
    return await rt.service.run(fn)


@action("addNotes", params=AddNotesParams, returns=list[int], summary="Create multiple notes")
async def add_notes(rt, notes=None):
    specs = notes or []

    def fn(col):
        # Faithful to AnkiConnect (__init__.py:2134): add each (addNote raises on empty/dup);
        # collect errors; if ANY failed, roll back ALL added notes and raise; else return ids.
        added_ids = []
        errs = []
        last_op = None
        # Resolve each distinct notetype/deck NAME -> id once for the whole batch,
        # not per note: on PG `models.by_name` and `decks.id` are uncached DB
        # round-trips, and a bulk addNotes is overwhelmingly one model + one deck.
        # Output is identical (same model object, same deck id) — pure de-duplication.
        model_cache = {}  # modelName -> model
        deck_cache = {}   # deckName  -> did
        for spec in specs:
            try:
                spec = spec or {}
                attach_media(col, spec)
                mname = spec.get("modelName", "")
                if mname not in model_cache:
                    m = col.models.by_name(mname)
                    if m is None:
                        raise Exception("model was not found: " + str(mname))
                    model_cache[mname] = m
                n, _ = build_note(col, spec, model=model_cache[mname])
                ok, err = check_addable(col, n, spec.get("options"))
                if not ok:
                    raise Exception(err)
                dname = spec.get("deckName", "Default")
                if dname not in deck_cache:
                    deck_cache[dname] = col.decks.id(dname)
                did = deck_cache[dname]
                last_op = col.add_note(n, did)
                added_ids.append(n.id)
            except Exception as e:
                errs.append(str(e))
        if errs:
            if added_ids:
                col.remove_notes(added_ids)
            raise Exception(str(errs))
        return added_ids, last_op
    return await run_emit(rt, fn)


@action("canAddNotes", params=CanAddNotesParams, returns=list[bool],
        summary="Can each note be added")
async def can_add_notes(rt, notes=None):
    return [await can_add_note(rt, note=n) for n in (notes or [])]


@action("canAddNotesWithErrorDetail", params=CanAddNotesWithErrorDetailParams,
        summary="Can each note be added (with error detail)")
async def can_add_notes_with_error_detail(rt, notes=None):
    return [await can_add_note_with_error_detail(rt, note=n) for n in (notes or [])]


@action("findNotes", params=FindNotesParams, returns=list[int], summary="Find note ids by query")
async def find_notes(rt, query=None):
    return await rt.service.run(lambda col: list(col.find_notes(query or "")))


@action("notesInfo", params=NotesInfoParams, summary="Full info for each note")
async def notes_info(rt, notes=None, query=None):
    def fn(col):
        ids = list(notes) if notes is not None else list(col.find_notes(query or ""))
        if not ids:
            return []
        # Batch the two per-note round-trips (col.get_note + note.card_ids()) into
        # set-based reads. Byte-identical to the canonical per-note note_to_info loop
        # (proven by the m12 probe): fields via split_fields, tags via the rslib
        # separator set (' ' / '　', drop empties = split_tags), card ids in
        # template (ord) order = note.card_ids(), mod/model straight from the row.
        # On PG each get_note / card_ids() was a network round-trip; SQLite ran them
        # in-process. Queries use the DISTINCT ids (so a duplicate id straddling a
        # chunk boundary can't double a card list); the output loop walks the
        # original `ids`, preserving order AND duplicate entries like the old loop.
        distinct = list(dict.fromkeys(ids))
        note_rows = {}
        for chunk in in_chunks(distinct):
            ph = ",".join("?" * len(chunk))
            for nid, mid, mod, tags, flds in col.db.all(
                    "select id, mid, mod, tags, flds from notes where id in (%s)" % ph, *chunk):
                note_rows[nid] = (mid, mod, tags, flds)
        cards_by_nid = {}
        for chunk in in_chunks(distinct):
            ph = ",".join("?" * len(chunk))
            for nid, cid in col.db.all(
                    "select nid, id from cards where nid in (%s) order by nid, ord" % ph, *chunk):
                cards_by_nid.setdefault(nid, []).append(cid)
        out = []
        for nid in ids:
            row = note_rows.get(nid)
            model = col.models.get(row[0]) if row is not None else None
            if model is None:            # unknown id / orphan note -> {} (get_note had raised)
                out.append({})
                continue
            _mid, mod, tags, flds = row
            field_vals = split_fields(flds)
            fields = {name: {"value": field_vals[ord_], "order": ord_}
                      for name, (ord_, _f) in col.models.field_map(model).items()}
            out.append({
                "noteId": nid,
                "profile": "User 1",
                "tags": [t for t in re.split(r"[ \u3000]", tags) if t],
                "fields": fields,
                "modelName": model["name"],
                "mod": mod,
                "cards": cards_by_nid.get(nid, []),
            })
        return out
    return await rt.service.run(fn)


@action("updateNoteFields", params=UpdateNoteFieldsParams, summary="Update a note's fields")
async def update_note_fields(rt, note=None):
    spec = note or {}

    def fn(col):
        n = col.get_note(spec["id"])
        for name, val in (spec.get("fields") or {}).items():
            if name in n:  # case-sensitive (AnkiConnect updateNoteFields is case-sensitive)
                n[name] = val
        return None, col.update_note(n, skip_undo_entry=True)
    await run_emit(rt, fn)
    return None


@action("updateNoteTags", params=UpdateNoteTagsParams, summary="Replace a note's tags")
async def update_note_tags(rt, note=None, tags=None):
    tags = tags or []

    def fn(col):
        n = col.get_note(note)
        n.tags = list(tags)
        return None, col.update_note(n)
    await run_emit(rt, fn)
    return None


@action("getNoteTags", params=GetNoteTagsParams, returns=list[str], summary="Get a note's tags")
async def get_note_tags(rt, note=None):
    return await rt.service.run(lambda col: list(col.get_note(note).tags))


@action("updateNote", params=UpdateNoteParams, summary="Update a note's fields and/or tags")
async def update_note(rt, note=None):
    spec = note or {}
    if "fields" not in spec and "tags" not in spec:
        raise Exception('Must provide a "fields" or "tags" property.')
    if "fields" in spec:
        await update_note_fields(rt, note=spec)
    if "tags" in spec:
        await update_note_tags(rt, note=spec["id"], tags=spec["tags"])
    return None


@action("updateNoteModel", params=UpdateNoteModelParams,
        summary="Reassign a note's model, fields and tags")
async def update_note_model(rt, note=None):
    # Reassign a note's notetype + fields/tags. Minimal: change mid, rebuild fields by name.
    spec = note or {}

    def fn(col):
        n = col.get_note(spec["id"])
        model = col.models.by_name(spec.get("modelName", ""))
        if model is None:
            raise Exception("model was not found: " + str(spec.get("modelName")))
        n.mid = model["id"]
        n.fields = [""] * len(model["flds"])
        by_lower = {f["name"].lower(): i for i, f in enumerate(model["flds"])}
        for name, val in (spec.get("fields") or {}).items():
            idx = by_lower.get(str(name).lower())
            if idx is not None:
                n.fields[idx] = val
        if "tags" in spec:
            n.tags = list(spec["tags"])
        return None, col.update_note(n)
    await run_emit(rt, fn)
    return None


@action("addTags", params=AddTagsParams, summary="Add tags to notes")
async def add_tags(rt, notes=None, tags=None, add=True):
    notes = notes or []

    def fn(col):
        return None, col.tags.bulk_add(notes, tags or "")
    await run_emit(rt, fn)
    return None


@action("removeTags", params=RemoveTagsParams, summary="Remove tags from notes")
async def remove_tags(rt, notes=None, tags=None):
    notes = notes or []

    def fn(col):
        return None, col.tags.bulk_remove(notes, tags or "")
    await run_emit(rt, fn)
    return None


@action("getTags", params=GetTagsParams, returns=list[str], summary="List all tags")
async def get_tags(rt):
    return await rt.service.run(lambda col: col.tags.all())


@action("clearUnusedTags", params=ClearUnusedTagsParams, summary="Remove unused tags")
async def clear_unused_tags(rt):
    def fn(col):
        return None, col.tags.clear_unused_tags()
    await run_emit(rt, fn)
    return None


@action("replaceTags", params=ReplaceTagsParams, summary="Replace a tag on notes")
async def replace_tags(rt, notes=None, tag_to_replace=None, replace_with_tag=None):
    notes = notes or []

    def fn(col):
        for nid in notes:
            n = col.get_note(nid)
            if tag_to_replace in n.tags:
                n.tags = [replace_with_tag if t == tag_to_replace else t for t in n.tags]
                col.update_note(n)
        return None
    await rt.service.run(fn)
    return None


@action("replaceTagsInAllNotes", params=ReplaceTagsInAllNotesParams,
        summary="Replace a tag across all notes")
async def replace_tags_in_all_notes(rt, tag_to_replace=None, replace_with_tag=None):
    def fn(col):
        return None, col.tags.rename(tag_to_replace, replace_with_tag)
    await run_emit(rt, fn)
    return None


@action("notesModTime", params=NotesModTimeParams, summary="Modification time of each note")
async def notes_mod_time(rt, notes=None):
    notes = notes or []

    def fn(col):
        if not notes:
            return []
        # Batch: one `id in (…)` read for all mod times instead of col.get_note per
        # id (a PG network round-trip each). A missing id yields {} exactly like the
        # old get_note/except path; the loop walks the original `notes` so order and
        # duplicate entries are preserved.
        mods = {}
        for chunk in in_chunks(list(dict.fromkeys(notes))):
            ph = ",".join("?" * len(chunk))
            mods.update(col.db.all("select id, mod from notes where id in (%s)" % ph, *chunk))
        return [{"noteId": nid, "mod": mods[nid]} if nid in mods else {} for nid in notes]
    return await rt.service.run(fn)


@action("deleteNotes", params=DeleteNotesParams, summary="Delete notes")
async def delete_notes(rt, notes=None):
    notes = notes or []

    def fn(col):
        return None, col.remove_notes(notes)
    await run_emit(rt, fn)
    return None


@action("removeEmptyNotes", params=RemoveEmptyNotesParams, summary="Remove empty notes")
async def remove_empty_notes(rt):
    def fn(col):
        report = col.get_empty_cards()
        # use the backend's own "all this note's cards are empty" flag
        nids = [e.note_id for e in report.notes if e.will_delete_note]
        if nids:
            return None, col.remove_notes(nids)
        return None, None  # run_emit tolerates a None op
    await run_emit(rt, fn)
    return None


@action("cardsToNotes", params=CardsToNotesParams, returns=list[int],
        summary="Map card ids to note ids")
async def cards_to_notes(rt, cards=None):
    cards = cards or []

    def fn(col):
        if not cards:
            return []
        # Match canonical AnkiConnect: a single non-raising SQL query that omits unknown
        # card ids (instead of looping col.get_card, which raises NotFoundError on a bad id).
        placeholders = ",".join("?" * len(cards))
        return col.db.list(
            "select distinct nid from cards where id in (%s)" % placeholders, *cards)
    return await rt.service.run(fn)
