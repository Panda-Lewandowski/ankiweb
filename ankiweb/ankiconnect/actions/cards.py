"""AnkiConnect card actions."""
from __future__ import annotations
from typing import Optional
from anki.errors import NotFoundError
from anki.utils import ids2str
from ankiweb.ankiconnect.registry import action
from ankiweb.ankiconnect.actions._helpers import card_to_info, run_emit, in_chunks
from ankiweb.ankiconnect.schemas.cards import (
    FindCardsParams, CardsInfoParams, CardsModTimeParams, SuspendParams, UnsuspendParams,
    SuspendedParams, AreSuspendedParams, AreDueParams, GetEaseFactorsParams, SetEaseFactorsParams,
    SetSpecificValueOfCardParams, GetIntervalsParams, ForgetCardsParams, RelearnCardsParams,
    AnswerCardsParams, SetDueDateParams,
)


@action("findCards", params=FindCardsParams, returns=list[int], summary="Find card ids by query")
async def find_cards(rt, query=""):
    return await rt.service.run(lambda col: list(col.find_cards(query or "")))


@action("cardsInfo", params=CardsInfoParams, summary="Full info for each card")
async def cards_info(rt, cards=None):
    cards = cards or []

    def fn(col):
        out = []
        model_cache = {}  # distinct notetypes resolved once per call, not per card
        for cid in cards:
            try:
                out.append(card_to_info(col, col.get_card(cid), model_cache))
            except Exception:
                out.append({})
        return out
    return await rt.service.run(fn)


@action("cardsModTime", params=CardsModTimeParams, summary="Modification time of each card")
async def cards_mod_time(rt, cards=None):
    cards = cards or []

    def fn(col):
        if not cards:
            return []
        # One query instead of one DB round-trip per card (on PG that is N
        # round-trips -> 1); `{}` for ids absent from the table, as before.
        mods = dict(col.db.all(f"select id, mod from cards where id in {ids2str(cards)}"))
        return [{"cardId": cid, "mod": mods[cid]} if cid in mods else {} for cid in cards]
    return await rt.service.run(fn)


@action("suspend", params=SuspendParams, returns=bool, summary="Suspend/unsuspend cards")
async def suspend(rt, cards=None, suspend=True):
    cards = cards or []

    def fn(col):
        op = col.sched.suspend_cards(cards) if suspend else col.sched.unsuspend_cards(cards)
        return True, op
    return await run_emit(rt, fn)


@action("unsuspend", params=UnsuspendParams, summary="Unsuspend cards")
async def unsuspend(rt, cards=None):
    cards = cards or []

    def fn(col):
        return None, col.sched.unsuspend_cards(cards)
    await run_emit(rt, fn)
    return None


@action("suspended", params=SuspendedParams, returns=bool, summary="Is a card suspended")
async def suspended(rt, card=None):
    return await rt.service.run(lambda col: col.get_card(card).queue == -1)


@action("areSuspended", params=AreSuspendedParams, returns=list[Optional[bool]],
        summary="Per-card suspended state")
async def are_suspended(rt, cards=None):
    cards = cards or []

    def fn(col):
        if not cards:
            return []
        # Batch the per-card queue read into one query (PG: N round-trips -> 1);
        # `None` for ids absent from the table, `queue == -1` means suspended.
        queues = dict(col.db.all(f"select id, queue from cards where id in {ids2str(cards)}"))
        return [(queues[cid] == -1) if cid in queues else None for cid in cards]
    return await rt.service.run(fn)


@action("areDue", params=AreDueParams, returns=list[bool], summary="Per-card due flag")
async def are_due(rt, cards=None):
    cards = cards or []
    return await rt.service.run(
        lambda col: [cid in set(col.find_cards("is:due")) or
                     cid in set(col.find_cards("is:new")) for cid in cards])


@action("getEaseFactors", params=GetEaseFactorsParams, returns=list[Optional[int]],
        summary="Per-card ease factor")
async def get_ease_factors(rt, cards=None):
    cards = cards or []

    def fn(col):
        if not cards:
            return []
        # Batch the per-card factor read into one query (PG: N round-trips -> 1);
        # `None` for ids absent from the table, faithful to AnkiConnect.
        factors = dict(col.db.all(f"select id, factor from cards where id in {ids2str(cards)}"))
        return [factors.get(cid) for cid in cards]
    return await rt.service.run(fn)


@action("setEaseFactors", params=SetEaseFactorsParams, returns=list[bool],
        summary="Set per-card ease factor")
async def set_ease_factors(rt, cards=None, easeFactors=None):
    cards = cards or []
    easeFactors = easeFactors or []

    def fn(col):
        out = []
        last_op = None
        for cid, factor in zip(cards, easeFactors):
            try:
                c = col.get_card(cid)
            except NotFoundError:
                out.append(False)  # faithful: AnkiConnect appends False for missing cards
                continue
            c.factor = int(factor)
            last_op = col.update_card(c)
            out.append(True)
        return out, last_op
    return await run_emit(rt, fn)


@action("setSpecificValueOfCard", params=SetSpecificValueOfCardParams,
        summary="Set arbitrary card column(s)")
async def set_specific_value_of_card(rt, card=None, keys=None, newValues=None, warning_check=False):
    keys = keys or []
    newValues = newValues or []
    risky = {"id", "nid", "did", "ord", "mod", "usn", "type", "queue", "due", "odue",
             "odid", "flags", "data"}

    def fn(col):
        try:
            c = col.get_card(card)
        except NotFoundError:
            return False, None  # faithful: AnkiConnect returns False on a missing card
        out = []
        for key, val in zip(keys, newValues):
            if key in risky and not warning_check:
                out.append([False, "Can't set this key without explicit warning_check"])
                continue
            try:
                setattr(c, key, val)
                out.append(True)
            except Exception as exc:
                out.append([False, str(exc)])
        op = col.update_card(c)
        return out, op
    return await run_emit(rt, fn)


@action("getIntervals", params=GetIntervalsParams, summary="Per-card review interval(s)")
async def get_intervals(rt, cards=None, complete=False):
    cards = cards or []
    if not complete:
        # Batch: one `id in (…)` read for every card's current interval instead of
        # col.get_card(cid).ivl per id (a PG round-trip each). Output order/dupes
        # follow the input `cards`. An unknown id raises (KeyError) -> action error,
        # the same "errors on a bad card id" contract as the old get_card(cid); for
        # valid ids (the normal path) the result is byte-identical.
        def fn_simple(col):
            if not cards:
                return []
            ivl = {}
            for chunk in in_chunks(list(dict.fromkeys(cards))):
                ph = ",".join("?" * len(chunk))
                ivl.update(col.db.all("select id, ivl from cards where id in (%s)" % ph, *chunk))
            return [ivl[cid] for cid in cards]
        return await rt.service.run(fn_simple)

    def fn(col):
        if not cards:
            return []
        # complete=True: every card's full revlog interval history, ordered by
        # revlog id. Batch the per-card revlog query into one `cid in (…)` read;
        # `order by cid, id` reproduces, within each card, the same id order the old
        # per-card `order by id` gave. A card with no revlog -> [] as before.
        by_cid = {}
        for chunk in in_chunks(list(dict.fromkeys(cards))):
            ph = ",".join("?" * len(chunk))
            for cid, ivl in col.db.all(
                    "select cid, ivl from revlog where cid in (%s) order by cid, id" % ph, *chunk):
                by_cid.setdefault(cid, []).append(ivl)
        return [by_cid.get(cid, []) for cid in cards]
    return await rt.service.run(fn)


@action("forgetCards", params=ForgetCardsParams, summary="Reset cards to new")
async def forget_cards(rt, cards=None):
    cards = cards or []

    def fn(col):
        return None, col.sched.schedule_cards_as_new(cards)
    await run_emit(rt, fn)
    return None


@action("relearnCards", params=RelearnCardsParams, summary="Move cards to relearning")
async def relearn_cards(rt, cards=None):
    cards = cards or []

    def fn(col):
        if not cards:  # avoid invalid "where id in ()"
            return None
        col.db.execute(
            "update cards set type=3, queue=1 where id in (%s)" %
            ",".join("?" * len(cards)), *cards)
        return None
    await rt.service.run(fn)
    return None


@action("answerCards", params=AnswerCardsParams, returns=list[bool],
        summary="Answer cards as if reviewed")
async def answer_cards(rt, answers=None):
    from anki.scheduler.v3 import CardAnswer
    answers = answers or []
    rating_map = {1: CardAnswer.Rating.AGAIN, 2: CardAnswer.Rating.HARD,
                  3: CardAnswer.Rating.GOOD, 4: CardAnswer.Rating.EASY}

    def fn(col):
        out = []
        last_op = None
        for a in answers:
            cid, ease = a["cardId"], a["ease"]
            try:
                card = col.get_card(cid)
            except NotFoundError:
                out.append(False)  # faithful: AnkiConnect appends False for missing cards
                continue
            card.start_timer()
            states = col._backend.get_scheduling_states(cid)
            answer = col.sched.build_answer(
                card=card, states=states, rating=rating_map[ease])
            last_op = col.sched.answer_card(answer)
            out.append(True)
        return out, last_op
    return await run_emit(rt, fn)


@action("setDueDate", params=SetDueDateParams, returns=bool, summary="Reschedule cards to a due date")
async def set_due_date(rt, cards=None, days="0"):
    cards = cards or []

    def fn(col):
        return True, col.sched.set_due_date(cards, str(days))
    return await run_emit(rt, fn)
