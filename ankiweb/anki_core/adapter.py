from __future__ import annotations

import asyncio
from importlib.metadata import version
import re
import time
import unicodedata
from typing import Literal
from urllib.parse import quote

from anki.scheduler.v3 import CardAnswer
from anki.sound import SoundOrVideoTag
from anki.utils import strip_html

from ankiweb.anki_core.review_sessions import (
    ReviewConflict,
    ReviewSession,
    ReviewSessionStore,
    ReviewTokenNotRevealed,
)
from ankiweb.anki_core.lesson_imports import process_lesson_batch
from ankiweb.anki_core.dto import (
    AnswerDTO, BackDTO, CardActionDTO, CardSummaryDTO, CheckDTO, HealthDTO,
    QuestionDTO, ReviewDTO, TTSSpecDTO, TodayDTO,
)
from ankiweb.collection_service import CollectionService
from ankiweb.language.card_types import LessonBatch
from ankiweb.language.receipts import LessonReceiptStore

Language = Literal["spanish", "english"]
LANGUAGE_DECKS: dict[str, str] = {
    "spanish": "Languages::Spanish",
    "english": "Languages::English",
}

_MODEL_KINDS = {
    "vocabulary production": "vocabulary_production",
    "vocabulary recognition": "vocabulary_recognition",
    "grammar cloze": "grammar_cloze",
    "personal error": "personal_error",
    "spanish conjugation": "spanish_conjugation",
    "listening dictation": "listening_dictation",
    "listening comprehension": "listening_comprehension",
    "phrase retrieval": "phrase_retrieval",
}


def _field(note, name: str) -> str:
    return note[name] if name in note else ""


def _require_language_deck(col, deck_name: str):
    did = col.decks.id_for_name(deck_name)
    if did is None:
        raise ReviewConflict(f"required deck does not exist: {deck_name}")
    prefix = deck_name + "::"
    if any(item.name.startswith(prefix) for item in col.decks.all_names_and_ids()):
        raise ReviewConflict(f"topic subdecks are unsupported below {deck_name}")
    return did


def _fingerprint(card) -> tuple:
    memory = card.memory_state.SerializeToString() if card.memory_state is not None else b""
    return (
        int(card.id), int(card.nid), int(card.did), int(card.odid), int(card.ord),
        int(card.type), int(card.queue), card.due, card.ivl, card.factor, card.reps,
        card.lapses, card.left, card.odue, card.flags, card.custom_data, memory,
    )


def _kind(note) -> str:
    model_name = (note.note_type().get("name") or "").strip().casefold()
    if model_name in _MODEL_KINDS:
        return _MODEL_KINDS[model_name]
    tagged = {tag.removeprefix("type::") for tag in note.tags if tag.startswith("type::")}
    for candidate in (
        "personal_error", "listening_dictation", "listening_comprehension",
        "conjugation", "cloze", "grammar", "vocabulary", "chunk",
    ):
        if candidate in tagged:
            return {
                "conjugation": "spanish_conjugation",
                "cloze": "grammar_cloze",
                "grammar": "grammar_cloze",
                "vocabulary": "vocabulary_production",
                "chunk": "phrase_retrieval",
            }.get(candidate, candidate)
    return "unknown"


def _audio_urls(card, question_side: bool) -> list[str]:
    tags = card.question_av_tags() if question_side else card.answer_av_tags()
    return [f"/{quote(tag.filename)}" for tag in tags if isinstance(tag, SoundOrVideoTag)]


def _expected(col, card, note, kind: str) -> tuple[str | None, bool]:
    if kind in {
        "vocabulary_production", "personal_error", "spanish_conjugation", "phrase_retrieval",
    }:
        return strip_html(_field(note, "Answer")), True
    if kind == "listening_dictation":
        return strip_html(_field(note, "Sentence")), True
    if kind == "grammar_cloze":
        return strip_html(col.extract_cloze_for_typing(_field(note, "Text"), card.ord + 1)), True
    return None, True


def _question(card, note, kind: str, language: str) -> QuestionDTO:
    out: QuestionDTO = {
        "kind": kind,
        "prompt_html": "",
        "instruction": "",
        "input_required": False,
        "audio_urls": _audio_urls(card, True),
        "tts_locale": None,
        "topic": strip_html(_field(note, "Topic")),
        "cefr": strip_html(_field(note, "CEFR")),
    }
    if kind == "vocabulary_production":
        out.update(prompt_html=_field(note, "Prompt"), input_required=True)
    elif kind == "vocabulary_recognition":
        out["prompt_html"] = _field(note, "Target")
    elif kind == "grammar_cloze":
        # Anki embeds the hidden cloze answer in a data-cloze attribute for its own
        # reviewer. Product clients must not receive that value before reveal.
        safe_question = re.sub(
            r"\sdata-cloze=(['\"]).*?\1", "", card.question(), flags=re.DOTALL)
        out.update(prompt_html=safe_question, input_required=True)
    elif kind == "personal_error":
        out.update(prompt_html=_field(note, "Prompt"), input_required=True)
    elif kind == "spanish_conjugation":
        out.update(
            prompt_html=_field(note, "Verb"), instruction="Напиши правильную форму",
            input_required=True, tense=_field(note, "Tense"), person=_field(note, "Person"),
        )
    elif kind in {"listening_dictation", "listening_comprehension"}:
        out.update(
            instruction=("Напечатай услышанную фразу" if kind == "listening_dictation" else ""),
            input_required=kind == "listening_dictation",
            tts_locale="es_ES" if language == "spanish" else "en_US",
        )
    elif kind == "phrase_retrieval":
        out.update(prompt_html=_field(note, "Prompt"), input_required=True)
    else:
        out["prompt_html"] = card.question()
    return out


def _expanded_cloze(text: str) -> str:
    return re.sub(r"\{\{c\d+::(.*?)(?:::[^{}]*?)?\}\}", r"\1", text)


def _back(card, note, kind: str, expected: str | None) -> BackDTO:
    fields_by_kind = {
        "vocabulary_production": ("Answer", "Example", "Translation"),
        "vocabulary_recognition": ("Translation", "Example"),
        "grammar_cloze": ("Text", "BackExtra"),
        "personal_error": ("Answer", "Explanation"),
        "spanish_conjugation": ("Answer", "Example"),
        "listening_dictation": ("Sentence", "Translation", "Note"),
        "listening_comprehension": ("Sentence", "Translation", "Note"),
        "phrase_retrieval": ("Answer", "Example"),
    }
    fields = {name: _field(note, name) for name in fields_by_kind.get(kind, ())}
    if kind == "grammar_cloze":
        fields = {
            "Answer": expected or "",
            "Text": _expanded_cloze(_field(note, "Text")),
            "BackExtra": _field(note, "BackExtra"),
        }
    if not fields:
        fields["answer_html"] = card.answer()
    return {"fields": fields, "audio_urls": _audio_urls(card, False)}


class AnkiAdapter:
    """Only product-facing access to Anki Core; never owns or opens a collection."""

    def __init__(self, service: CollectionService, *, token_ttl_seconds: int = 20 * 60) -> None:
        self._service = service
        self._sessions = ReviewSessionStore(token_ttl_seconds)
        self._lock = asyncio.Lock()
        self._receipts = LessonReceiptStore(
            service.settings.collection_path.parent / "lesson-receipts")

    async def health(self) -> HealthDTO:
        data = await self._service.run(lambda col: {
            "anki_version": version("anki"), "scheduler_version": col.sched.version,
        })
        return {"ok": True, "anki_core": data}

    async def today(self, language: Language) -> TodayDTO:
        deck_name = LANGUAGE_DECKS[language]

        def read(col):
            did = _require_language_deck(col, deck_name)
            node = col.sched.deck_due_tree(did)
            if node is None:
                return None
            return {
                "language": language, "deck": deck_name,
                "new": node.new_count, "learning": node.learn_count,
                "review": node.review_count,
                "total": node.new_count + node.learn_count + node.review_count,
            }

        result = await self._service.run(read)
        if result is None:
            raise ReviewConflict(f"deck has no scheduler state: {deck_name}")
        return result

    async def next_review(self, language: Language, client_id: str) -> ReviewDTO | None:
        async with self._lock:
            if current := self._sessions.for_client(client_id, language):
                return self._session_payload(current)
            deck_name = LANGUAGE_DECKS[language]

            def load(col):
                did = _require_language_deck(col, deck_name)
                col.decks.set_current(did)
                queued = col.sched.get_queued_cards(fetch_limit=100)
                top = next(
                    (item for item in queued.cards
                     if not self._sessions.card_is_leased(int(item.card.id))), None)
                if top is None:
                    return None
                card = col.get_card(top.card.id)
                card.start_timer()
                note = card.note()
                kind = _kind(note)
                expected, combining = _expected(col, card, note, kind)
                return {
                    "client_id": client_id, "language": language,
                    "card_id": int(card.id), "note_id": int(card.nid),
                    "card": card, "states": top.states, "context": top.context,
                    "collection_generation": self._service.generation,
                    "fingerprint": _fingerprint(card),
                    "question": _question(card, note, kind, language),
                    "back": _back(card, note, kind, expected),
                    "expected": expected, "combining": combining,
                }

            loaded = await self._service.run(load)
            if loaded is None:
                return None
            return self._session_payload(self._sessions.create(**loaded))

    async def check(self, token: str, client_id: str, typed_answer: str, *, case_sensitive: bool,
                    punctuation_sensitive: bool) -> CheckDTO:
        async with self._lock:
            session = self._sessions.get(token)
            self._verify_client(session, client_id)
            expected = session.expected
            diff_html = None
            correct = None
            if expected is not None:
                provided = _normalize(typed_answer, case_sensitive, punctuation_sensitive)
                wanted = _normalize(expected, case_sensitive, punctuation_sensitive)
                diff_html = await self._service.run(
                    lambda col: col.compare_answer(wanted, provided, session.combining))
                correct = provided == wanted
            session.revealed = True
            return {"token": token, "correct": correct, "diff_html": diff_html, "back": session.back}

    async def answer(self, token: str, client_id: str,
                     rating: Literal["again", "hard", "good", "easy"]) -> AnswerDTO:
        rating_map = {
            "again": CardAnswer.Rating.AGAIN, "hard": CardAnswer.Rating.HARD,
            "good": CardAnswer.Rating.GOOD, "easy": CardAnswer.Rating.EASY,
        }
        async with self._lock:
            session = self._sessions.get(token)
            self._verify_client(session, client_id)
            if not session.revealed:
                raise ReviewTokenNotRevealed("reveal/check the answer before rating")

            def mutate(col):
                if self._service.generation != session.collection_generation:
                    raise ReviewConflict("collection was reopened after the card was issued")
                current = col.get_card(session.card_id)
                if _fingerprint(current) != session.fingerprint:
                    raise ReviewConflict("card scheduling state changed after it was issued")
                answer = col.sched.build_answer(
                    card=session.card, states=session.states, rating=rating_map[rating])
                return col.sched.answer_card(answer)

            try:
                await self._service.run_op(mutate, initiator="language-trainer-api")
            except ReviewConflict:
                self._sessions.stale(session)
                raise
            self._sessions.consume(session)
            result = {"answered": True, "card_id": session.card_id, "rating": rating}
        result["next"] = await self.next_review(session.language, client_id)
        return result

    async def suspend(self, card_id: int) -> CardActionDTO:
        return await self._card_action(card_id, "suspend")

    async def unsuspend(self, card_id: int) -> CardActionDTO:
        return await self._card_action(card_id, "unsuspend")

    async def bury(self, card_id: int) -> CardActionDTO:
        return await self._card_action(card_id, "bury")

    async def card_summary(self, card_id: int) -> CardSummaryDTO:
        return await self._service.run(lambda col: self._validated_card_summary(col, card_id))

    async def tts_spec(self, token: str, client_id: str) -> TTSSpecDTO:
        """Resolve hidden listening text internally without returning it through review JSON."""
        async with self._lock:
            session = self._sessions.get(token)
            self._verify_client(session, client_id)
            if session.question.get("kind") not in {
                "listening_dictation", "listening_comprehension",
            }:
                raise ReviewConflict("TTS is only available for listening cards")
            locale = session.question.get("tts_locale")
            text = strip_html(session.back["fields"].get("Sentence", "")).strip()
            if not locale or not text:
                raise ReviewConflict("listening card has no TTS source")
            return {"text": text, "locale": locale}

    async def lesson_cards_batch(self, batch: LessonBatch, *, commit: bool) -> dict:
        """Preview or safely apply a lesson import without touching scheduler state."""
        async with self._lock:
            deck_name = LANGUAGE_DECKS[batch.language]

            def process(col):
                deck_id = _require_language_deck(col, deck_name)
                return process_lesson_batch(col, batch, deck_name, deck_id, commit=commit)

            result, flags = await self._service.run(process)
            if flags:
                await self._service.emit(flags, "language-trainer-lesson-import")
            if commit:
                durable = await self._receipts.save({
                    "mode": result["mode"],
                    "lesson": result["lesson"],
                    "summary": result["summary"],
                    "items": result["items"],
                    "errors": result["errors"],
                })
                result["receipt_id"] = durable["receipt_id"]
                result["created_at"] = durable["created_at"]
            return result

    async def lesson_receipts(self, limit: int = 20) -> list[dict]:
        return await self._receipts.list(limit)

    async def _card_action(self, card_id: int, action: str) -> CardActionDTO:
        async with self._lock:
            def mutate(col):
                self._validated_card_summary(col, card_id)
                fn = {
                    "suspend": col.sched.suspend_cards,
                    "unsuspend": col.sched.unsuspend_cards,
                    "bury": col.sched.bury_cards,
                }[action]
                return fn([card_id])

            await self._service.run_op(mutate, initiator="language-trainer-api")
            self._sessions.release_card(card_id)
            return {"ok": True, "card_id": card_id, "action": action}

    @staticmethod
    def _validated_card_summary(col, card_id: int) -> CardSummaryDTO:
        card = col.get_card(card_id)
        allowed = {col.decks.id_for_name(name) for name in LANGUAGE_DECKS.values()}
        if card.current_deck_id() not in allowed:
            raise ReviewConflict("card is not in an exact Language Trainer deck")
        return {
            "card_id": int(card.id), "note_id": int(card.nid),
            "deck": col.decks.name(card.current_deck_id()),
            "queue": int(card.queue), "type": int(card.type),
        }

    @staticmethod
    def _session_payload(session: ReviewSession) -> ReviewDTO:
        return {
            "token": session.token,
            "expires_in_seconds": max(0, int(session.expires_at - time.monotonic())),
            "language": session.language, "card_id": session.card_id,
            "note_id": session.note_id, "question": session.question,
        }

    @staticmethod
    def _verify_client(session: ReviewSession, client_id: str) -> None:
        if session.client_id != client_id:
            raise ReviewConflict("review token belongs to a different client")


def _normalize(value: str, case_sensitive: bool, punctuation_sensitive: bool) -> str:
    value = unicodedata.normalize("NFC", strip_html(value))
    value = " ".join(value.split())
    if not case_sensitive:
        value = value.casefold()
    if not punctuation_sensitive:
        value = re.sub(r"[^\w\s]", "", value, flags=re.UNICODE)
    return value
