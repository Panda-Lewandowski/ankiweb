from __future__ import annotations

from typing import NotRequired, TypedDict


class AnkiCoreHealthDTO(TypedDict):
    anki_version: str
    scheduler_version: int


class HealthDTO(TypedDict):
    ok: bool
    anki_core: AnkiCoreHealthDTO


class QuestionDTO(TypedDict):
    kind: str
    prompt_html: str
    instruction: str
    input_required: bool
    audio_urls: list[str]
    tts_locale: str | None
    tense: NotRequired[str]
    person: NotRequired[str]


class BackDTO(TypedDict):
    fields: dict[str, str]
    audio_urls: list[str]


class ReviewDTO(TypedDict):
    token: str
    expires_in_seconds: int
    language: str
    card_id: int
    note_id: int
    question: QuestionDTO


class CheckDTO(TypedDict):
    token: str
    correct: bool | None
    diff_html: str | None
    back: BackDTO


class TodayDTO(TypedDict):
    language: str
    deck: str
    new: int
    learning: int
    review: int
    total: int


class AnswerDTO(TypedDict):
    answered: bool
    card_id: int
    rating: str
    next: ReviewDTO | None


class CardActionDTO(TypedDict):
    ok: bool
    card_id: int
    action: str


class CardSummaryDTO(TypedDict):
    card_id: int
    note_id: int
    deck: str
    queue: int
    type: int
