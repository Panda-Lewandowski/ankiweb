from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from anki.consts import MODEL_CLOZE

from ankiweb.language.card_types import CARD_TYPES, CardTypeSpec, LessonBatch, LessonCard
from ankiweb.language.duplicates import duplicate_key


SAFE_METADATA_FIELDS = ("Language", "CEFR", "Topic", "Source")


@dataclass
class _IndexedNote:
    note_id: int | None
    batch_index: int | None
    note: Any
    fields: dict[str, str]
    tags: set[str]


def process_lesson_batch(col, batch: LessonBatch, deck_name: str, deck_id: int,
                         *, commit: bool) -> tuple[dict[str, Any], dict[str, bool]]:
    """Plan and optionally apply a lesson batch in one serialized collection call."""
    model_errors: dict[str, str] = {}
    models: dict[str, Any] = {}
    for spec in {card.spec for card in batch.cards}:
        model = col.models.by_name(spec.model_name)
        error = _model_error(model, spec)
        if error:
            model_errors[spec.key] = error
        else:
            models[spec.key] = model

    indexes: dict[tuple[str, str], list[_IndexedNote]] = {}
    wanted_models = {spec.model_name: spec for spec in CARD_TYPES.values()}
    for note_id in col.find_notes(f'deck:"{deck_name}"'):
        note = col.get_note(note_id)
        model_name = (note.note_type().get("name") or "").strip()
        spec = wanted_models.get(model_name)
        if spec is None or spec.key in model_errors:
            continue
        fields = {name: note[name] if name in note else "" for name in spec.fields}
        key = duplicate_key(spec, fields)
        indexes.setdefault(key, []).append(_IndexedNote(
            int(note.id), None, note, fields, set(note.tags),
        ))

    decisions: list[dict[str, Any]] = [dict(error) for error in batch.errors]
    operation_results: list[Any] = []
    for card in batch.cards:
        base = {
            "index": card.index,
            "model": card.spec.model_name,
            "deck": deck_name,
            "type": card.spec.key,
        }
        if error := model_errors.get(card.spec.key):
            decisions.append({**base, "status": "error", "message": error})
            continue

        key = duplicate_key(card.spec, card.fields)
        matches = indexes.get(key, [])
        if len(matches) > 1:
            decisions.append({
                **base,
                "status": "ambiguous_duplicate",
                "matching_note_ids": [item.note_id for item in matches if item.note_id is not None],
                "message": "multiple exact matches exist; no note was changed",
            })
            continue
        if matches:
            match = matches[0]
            if match.note is None:
                decisions.append({
                    **base,
                    "status": "skipped_duplicate",
                    "matching_batch_index": match.batch_index,
                })
                continue
            conflicting_fields = [
                name for name in ("Language", "CEFR")
                if name in match.fields
                and match.fields[name].strip()
                and match.fields[name].strip().casefold() != card.fields.get(name, "").strip().casefold()
            ]
            if conflicting_fields:
                decisions.append({
                    **base,
                    "status": "metadata_conflict",
                    "note_id": match.note_id,
                    "conflicting_fields": conflicting_fields,
                    "message": "existing Language/CEFR metadata differs; no note was changed",
                })
                continue
            fields_to_fill = {
                name: card.fields[name]
                for name in SAFE_METADATA_FIELDS
                if name in match.fields and card.fields.get(name) and not match.fields[name].strip()
            }
            missing_tags = sorted(set(card.tags) - match.tags)
            if fields_to_fill or missing_tags:
                if commit:
                    for name, value in fields_to_fill.items():
                        match.note[name] = value
                    match.note.tags = sorted(match.tags | set(missing_tags))
                    operation_results.append(col.update_note(match.note))
                match.fields.update(fields_to_fill)
                match.tags.update(missing_tags)
                decisions.append({
                    **base,
                    "status": "updated_metadata" if commit else "would_update_metadata",
                    "note_id": match.note_id,
                    "filled_fields": sorted(fields_to_fill),
                    "added_tags": missing_tags,
                })
            else:
                decisions.append({
                    **base, "status": "skipped_duplicate", "note_id": match.note_id,
                })
            continue

        if commit:
            note = col.new_note(models[card.spec.key])
            for name, value in card.fields.items():
                if name in note:
                    note[name] = value
            # Existing desktop-compatible listening models may have these helper fields.
            if "_TTS_ES" in note:
                note["_TTS_ES"] = "1" if batch.language == "spanish" else ""
            if "_TTS_EN" in note:
                note["_TTS_EN"] = "1" if batch.language == "english" else ""
            note.tags = list(card.tags)
            operation_results.append(col.add_note(note, deck_id))
            note_id = int(note.id)
            status = "added"
            indexed_note = note
        else:
            note_id = None
            status = "would_add"
            indexed_note = None
        decisions.append({**base, "status": status, **({"note_id": note_id} if note_id else {})})
        indexes.setdefault(key, []).append(_IndexedNote(
            note_id, card.index, indexed_note, dict(card.fields), set(card.tags),
        ))

    decisions.sort(key=lambda item: item["index"])
    summary = _summary(decisions)
    result = {
        "mode": "commit" if commit else "preview",
        "lesson": {"language": batch.language, "source": batch.source, "date": batch.date},
        "summary": summary,
        **summary,
        "items": decisions,
        "errors": [
            {"index": item["index"], "message": item.get("message", "import error")}
            for item in decisions if item["status"] in {"error", "ambiguous_duplicate"}
            or item["status"] == "metadata_conflict"
        ],
        "receipt_id": None,
    }
    return result, _combined_flags(operation_results)


def _model_error(model: Any, spec: CardTypeSpec) -> str | None:
    if model is None:
        return f"required note type does not exist: {spec.model_name}"
    actual_fields = {field["name"] for field in model.get("flds", [])}
    missing = sorted(set(spec.fields) - actual_fields)
    if missing:
        return f"note type {spec.model_name} is missing fields: {', '.join(missing)}"
    is_cloze = int(model.get("type", 0)) == MODEL_CLOZE
    if is_cloze != spec.cloze:
        expected = "cloze" if spec.cloze else "standard"
        return f"note type {spec.model_name} must be {expected}"
    return None


def _summary(decisions: list[dict[str, Any]]) -> dict[str, int]:
    statuses = [item["status"] for item in decisions]
    return {
        "added": statuses.count("added"),
        "would_add": statuses.count("would_add"),
        "updated_metadata": statuses.count("updated_metadata"),
        "would_update_metadata": statuses.count("would_update_metadata"),
        "duplicates": (
            statuses.count("skipped_duplicate") + statuses.count("ambiguous_duplicate")
            + statuses.count("metadata_conflict")
        ),
        "error_count": (
            statuses.count("error") + statuses.count("ambiguous_duplicate")
            + statuses.count("metadata_conflict")
        ),
    }


def _combined_flags(results: list[Any]) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    for result in results:
        changes = getattr(result, "changes", result)
        descriptor = getattr(changes, "DESCRIPTOR", None)
        if descriptor is None:
            continue
        for field in descriptor.fields:
            if field.type == field.TYPE_BOOL:
                flags[field.name] = flags.get(field.name, False) or bool(getattr(changes, field.name))
    return flags
