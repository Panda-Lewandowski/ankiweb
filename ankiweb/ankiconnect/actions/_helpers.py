from __future__ import annotations
from ankiweb.collection_service import op_changes_to_flags

_EMPTY, _DUPLICATE = 1, 2  # note.fields_check() int states


def in_chunks(seq, n=900):
    """Yield slices of `seq` no larger than `n`. Lets a batched `... id in (?,?,…)`
    read stay under SQLite's bound-variable limit (and well under PG's) for callers
    that may pass thousands of ids; the per-chunk results merge into one dict, so the
    output is identical to a single IN-list and to the original per-id loop."""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# PG multi-worker: two workers get-or-creating the same deck/notetype (or colliding
# on a same-millisecond id) race on the unique indexes; the loser's INSERT fails with
# SqlState 23505 instead of resolving the winner's row. These markers identify exactly
# that failure shape. SQLite never produces them (single exclusive writer), so the
# retry below is inert there and canonical behavior is unchanged.
_CREATE_RACE_MARKERS = (
    "idx_decks_name", "idx_notetypes_name", "decks_pkey", "notetypes_pkey", "E23505",
)


def _is_create_race(exc: Exception) -> bool:
    msg = str(exc)
    return any(marker in msg for marker in _CREATE_RACE_MARKERS)


async def retry_create_races(call, attempts=3):
    """await call(), retrying when a PG unique-violation shows a concurrent worker won
    a get-or-create race. The op re-runs in a fresh transaction where the get-or-create
    resolves the winner's committed row (or, for createModel, reports the canonical
    "Model name already exists"). Only for idempotent-on-retry operations."""
    for attempt in range(attempts):
        try:
            return await call()
        except Exception as exc:
            if attempt + 1 >= attempts or not _is_create_race(exc):
                raise


async def run_emit(rt, fn):
    """Run fn(col) -> (value, op_with_changes | None); broadcast its OpChanges flags on the
    bus (so an open web UI refreshes); return value. Tolerates a None op (no-op actions)."""
    value, op = await rt.service.run(fn)
    if op is None:
        return value
    changes = getattr(op, "changes", op)
    flags = op_changes_to_flags(changes)
    if any(flags.values()):
        await rt.service.emit(flags, "ankiconnect")
    return value


def build_note(col, spec, model=None):
    """Build (not add) an anki Note from an AnkiConnect note spec. Case-insensitive field
    matching. Media fields (audio/video/picture) deferred to B3.

    `model` may be passed pre-resolved so a batch caller (addNotes) resolves each
    distinct modelName once instead of paying `models.by_name` (a DB round-trip on
    PG) per note; omitted, it falls back to resolving from the spec as before."""
    spec = spec or {}
    if model is None:
        model = col.models.by_name(spec.get("modelName", ""))
    if model is None:
        raise Exception("model was not found: " + str(spec.get("modelName")))
    note = col.new_note(model)
    by_lower = {f["name"].lower(): f["name"] for f in model["flds"]}
    for key, val in (spec.get("fields") or {}).items():
        real = by_lower.get(str(key).lower())
        if real is not None:
            note[real] = val
    for tag in spec.get("tags") or []:
        note.add_tag(tag)
    return note, model


def check_addable(col, note, options):
    options = options or {}
    fc = note.fields_check()
    if fc == _EMPTY:
        return False, "cannot create note because it is empty"
    if fc == _DUPLICATE and not options.get("allowDuplicate", False):
        return False, "cannot create note because it is a duplicate"
    return True, None


def note_to_info(col, note):
    model = note.note_type()
    fields = {}
    for name, (ord_, _f) in col.models.field_map(model).items():
        fields[name] = {"value": note.fields[ord_], "order": ord_}
    return {
        "noteId": note.id,
        "profile": "User 1",
        "tags": list(note.tags),
        "fields": fields,
        "modelName": model["name"],
        "mod": note.mod,
        "cards": list(note.card_ids()),
    }


def card_to_info(col, card, model_cache=None):
    """`model_cache` ({mid: model dict}) lets a batch caller (cardsInfo) resolve each
    distinct notetype once per call instead of once per card — same dict either way."""
    note = card.note()
    if model_cache is None:
        model = note.note_type()
    else:
        if note.mid not in model_cache:
            model_cache[note.mid] = note.note_type()
        model = model_cache[note.mid]
    fields = {}
    for name, (ord_, _f) in col.models.field_map(model).items():
        fields[name] = {"value": note.fields[ord_], "order": ord_}
    try:
        states = col._backend.get_scheduling_states(card.id)
        next_reviews = list(col.sched.describe_next_states(states))
    except Exception:
        next_reviews = []
    return {
        "cardId": card.id, "note": note.id, "deckName": col.decks.name(card.did),
        "modelName": model["name"], "fieldOrder": card.ord,
        "fields": fields, "question": card.question(), "answer": card.answer(),
        "css": model.get("css", ""), "ord": card.ord, "type": card.type,
        "queue": card.queue, "due": card.due, "reps": card.reps, "lapses": card.lapses,
        "left": card.left, "mod": card.mod, "factor": card.factor, "interval": card.ivl,
        "nextReviews": next_reviews,
    }
