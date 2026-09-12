"""Offline rehearsal helpers. Called only through AnkiAdapter/CollectionService.

No collection SQL, scheduling edits, or public migration routes. Snapshots contain
content hashes, not a second copy of the learning database.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path

from anki import import_export_pb2 as ie


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def snapshot(col):
    from ankiweb.anki_core.adapter import _kind, _question, _expected, _back, LANGUAGE_DECKS

    notes, cards, models, decks, rendering = {}, {}, {}, {}, {}
    for nid in sorted(col.find_notes("")):
        note = col.get_note(nid)
        model = note.note_type()
        notes[str(nid)] = {"guid": note.guid, "model": model["name"],
                           "tags": sorted(note.tags), "fields_hash": digest(dict(note.items()))}
        models[model["name"]] = {
            "fields": [field["name"] for field in model["flds"]],
            "templates_hash": digest({template["name"]: {
                "Front": template["qfmt"], "Back": template["afmt"]
            } for template in model["tmpls"]}),
            "css_hash": digest(model["css"]), "type": model["type"],
        }
    for cid in sorted(col.find_cards("")):
        card = col.get_card(cid)
        deck = col.decks.name(card.did)
        state = {key: getattr(card, key) for key in (
            "id", "nid", "ord", "type", "queue", "due", "ivl", "factor", "reps",
            "lapses", "left", "odue", "odid", "flags", "original_position", "custom_data",
            "desired_retention", "decay", "last_review_time",
        )}
        state["deck"] = deck
        state["memory"] = (card.memory_state.SerializeToString().hex()
                           if card.memory_state is not None else None)
        logs = col.get_review_logs(cid)
        state["review_count"] = len(logs)
        state["reviews_hash"] = digest(sorted(entry.SerializeToString().hex() for entry in logs))
        cards[str(cid)] = state
        config = col.decks.config_dict_for_deck_id(card.did)
        decks[deck] = {key: value for key, value in config.items() if key not in {"id", "mod", "usn"}}
        note = card.note()
        kind = _kind(note)
        language = next((lang for lang, name in LANGUAGE_DECKS.items() if name == deck), None)
        try:
            if language is None or kind == "unknown":
                raise ValueError("unsupported deck or note type")
            question = _question(card, note, kind, language)
            expected, _ = _expected(col, card, note, kind)
            back = _back(card, note, kind, expected)
            if question["input_required"] and not expected:
                raise ValueError("empty expected answer")
            if not question["prompt_html"] and not kind.startswith("listening_"):
                raise ValueError("empty prompt")
            rendering[str(cid)] = {"ok": True, "kind": kind,
                                    "question_hash": digest(question), "back_hash": digest(back)}
        except Exception as exc:
            # Do not record exceptions containing card text.
            rendering[str(cid)] = {"ok": False, "kind": kind, "error_type": type(exc).__name__}
    media_dir = Path(col.media.dir())
    media = {path.name: file_digest(path) for path in sorted(media_dir.iterdir()) if path.is_file()}
    guids = Counter(note["guid"] for note in notes.values())
    return {"notes": notes, "cards": cards, "models": models, "decks": decks, "media": media,
            "rendering": rendering, "fsrs_enabled": col.get_config("fsrs", False),
            "summary": {"notes": len(notes), "cards": len(cards), "media": len(media),
                        "reviews": sum(card["review_count"] for card in cards.values()),
                        "fsrs_cards": sum(card["memory"] is not None for card in cards.values()),
                        "rendered": sum(item["ok"] for item in rendering.values()),
                        "duplicate_guids": sum(count > 1 for count in guids.values()),
                        "decks": dict(Counter(card["deck"] for card in cards.values()))}}


def export_package(col, path, *, legacy=False):
    path = Path(path)
    if path.exists():
        raise FileExistsError("export destination already exists")
    if path.suffix == ".colpkg":
        count = len(col.find_notes(""))
        try:
            col.export_collection_package(str(path), include_media=True, legacy=legacy)
        finally:
            col.reopen()
        return {"notes_exported": count, "sha256": file_digest(path)}
    limit = ie.ExportLimit()
    limit.whole_collection.SetInParent()
    count = col.export_anki_package(
        out_path=str(path), limit=limit,
        options=ie.ExportAnkiPackageOptions(with_scheduling=True, with_media=True,
                                           with_deck_configs=True, legacy=legacy),
    )
    return {"notes_exported": count, "sha256": file_digest(path)}


def import_empty(col, package, expected_sha256, backup, *, apply=False):
    package, backup = Path(package), Path(backup)
    if file_digest(package) != expected_sha256:
        raise ValueError("package changed since preview")
    if col.find_notes("") or col.find_cards(""):
        raise ValueError("rehearsal import requires an empty target; merging is not implemented")
    if not apply:
        return {"mode": "preview", "target_empty": True, "package_sha256": expected_sha256}
    export_package(col, backup)
    if package.suffix == ".colpkg":
        # The owner performs this import after preparation in the SAME serialized job.
        return {"mode": "prepared", "backup_sha256": file_digest(backup)}
    col.import_anki_package(ie.ImportAnkiPackageRequest(
        package_path=str(package), options=ie.ImportAnkiPackageOptions(
            with_scheduling=True, with_deck_configs=True, merge_notetypes=False,
            update_notes=ie.IMPORT_ANKI_PACKAGE_UPDATE_CONDITION_NEVER,
            update_notetypes=ie.IMPORT_ANKI_PACKAGE_UPDATE_CONDITION_NEVER,
        ),
    ))
    return {"mode": "applied", "backup_sha256": file_digest(backup)}


def compare(before, after):
    """Report exact field changes; never repair/recalculate scheduling on mismatch."""
    differences = []
    for section in ("notes", "cards", "models", "decks", "media", "rendering"):
        for key in sorted(before[section].keys() | after[section].keys()):
            left, right = before[section].get(key), after[section].get(key)
            if left != right:
                fields = ([field for field in sorted(left.keys() | right.keys())
                           if left.get(field) != right.get(field)]
                          if isinstance(left, dict) and isinstance(right, dict) else ["presence/value"])
                differences.append({"section": section, "id": key, "fields": fields})
    if before["fsrs_enabled"] != after["fsrs_enabled"]:
        differences.append({"section": "collection", "id": "fsrs_enabled", "fields": ["value"]})
    # Anki localizes the built-in preset name on import (e.g. Russian -> English).
    # Keep this visible, but distinguish a label from actual scheduler settings.
    labels = [item for item in differences
              if item["section"] == "decks" and item["fields"] == ["name"]]
    return {"equal": not differences, "preserved": len(labels) == len(differences),
            "differences": differences, "preset_label_changes": labels}
