from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


ALLOWED_CEFR = frozenset({"B1", "B2", "C1"})
ALLOWED_SOURCES = frozenset({
    "chatgpt_lesson", "teacher_lesson", "speaking", "writing", "listening",
})


@dataclass(frozen=True)
class CardTypeSpec:
    key: str
    model_name: str
    fields: tuple[str, ...]
    required_fields: tuple[str, ...]
    duplicate_fields: tuple[str, ...]
    duplicate_kind: str
    type_tags: tuple[str, ...]
    cloze: bool = False


CARD_TYPES: dict[str, CardTypeSpec] = {
    "vocabulary_production": CardTypeSpec(
        "vocabulary_production", "Vocabulary Production",
        ("Prompt", "Answer", "Example", "Translation", "Language", "CEFR", "Topic", "Source"),
        ("Prompt", "Answer"), ("Prompt", "Answer"), "vocabulary_production",
        ("type::vocabulary",),
    ),
    "vocabulary_recognition": CardTypeSpec(
        "vocabulary_recognition", "Vocabulary Recognition",
        ("Target", "Translation", "Example", "Language", "CEFR", "Topic", "Source"),
        ("Target", "Translation"), ("Target",), "vocabulary_recognition",
        ("type::vocabulary",),
    ),
    "grammar_cloze": CardTypeSpec(
        "grammar_cloze", "Grammar Cloze",
        ("Text", "BackExtra", "Language", "CEFR", "Topic", "Source"),
        ("Text",), ("Text",), "grammar_cloze", ("type::grammar", "type::cloze"), True,
    ),
    "personal_error": CardTypeSpec(
        "personal_error", "Personal Error",
        ("Prompt", "Answer", "Explanation", "OriginalError", "Language", "CEFR", "Topic", "Source"),
        ("Prompt", "Answer"), ("Prompt", "Answer"), "personal_error",
        ("type::personal_error",),
    ),
    "spanish_conjugation": CardTypeSpec(
        "spanish_conjugation", "Spanish Conjugation",
        ("Verb", "Tense", "Person", "Answer", "Example", "CEFR", "Topic", "Source"),
        ("Verb", "Tense", "Person", "Answer"), ("Verb", "Tense", "Person"), "conjugation",
        ("type::conjugation",),
    ),
    "listening_dictation": CardTypeSpec(
        "listening_dictation", "Listening Dictation",
        ("Sentence", "Translation", "Note", "Language", "CEFR", "Topic", "Source"),
        ("Sentence",), ("Sentence",), "listening", ("type::listening_dictation",),
    ),
    "listening_comprehension": CardTypeSpec(
        "listening_comprehension", "Listening Comprehension",
        ("Sentence", "Translation", "Note", "Language", "CEFR", "Topic", "Source"),
        ("Sentence",), ("Sentence",), "listening", ("type::listening_comprehension",),
    ),
    "phrase_retrieval": CardTypeSpec(
        "phrase_retrieval", "Phrase Retrieval",
        ("Prompt", "Answer", "Example", "Language", "CEFR", "Topic", "Source"),
        ("Prompt", "Answer"), ("Prompt", "Answer"), "phrase_retrieval", ("type::chunk",),
    ),
}

TYPE_ALIASES = {
    **{key: key for key in CARD_TYPES},
    "vocabulary": "vocabulary_production",
    "collocation": "vocabulary_production",
    "cloze": "grammar_cloze",
    "conjugation": "spanish_conjugation",
    "chunk": "phrase_retrieval",
}

INPUT_FIELDS = {
    "prompt": "Prompt", "answer": "Answer", "example": "Example",
    "translation": "Translation", "target": "Target", "text": "Text",
    "back_extra": "BackExtra", "explanation": "Explanation",
    "original_error": "OriginalError", "verb": "Verb", "tense": "Tense",
    "person": "Person", "sentence": "Sentence", "note": "Note",
}


@dataclass(frozen=True)
class LessonCard:
    index: int
    requested_type: str
    spec: CardTypeSpec
    fields: dict[str, str]
    tags: tuple[str, ...]


@dataclass(frozen=True)
class LessonBatch:
    language: str
    source: str
    date: str
    cards: tuple[LessonCard, ...]
    errors: tuple[dict[str, Any], ...]


def slug(value: str) -> str:
    value = re.sub(r"\s+", "_", value.strip().casefold())
    value = re.sub(r"[^\w-]+", "_", value, flags=re.UNICODE)
    return value.strip("_")


def parse_lesson_batch(payload: dict[str, Any]) -> LessonBatch:
    lesson = payload["lesson"]
    language = str(lesson["language"]).strip().casefold()
    source = slug(str(lesson["source"]))
    lesson_date = str(lesson["date"])
    if language not in {"spanish", "english"}:
        raise ValueError("language must be spanish or english")
    if source not in ALLOWED_SOURCES:
        raise ValueError("unsupported lesson source")
    parsed: list[LessonCard] = []
    errors: list[dict[str, Any]] = []

    for index, raw in enumerate(payload["cards"], start=1):
        try:
            parsed.append(_parse_card(index, raw, language, source))
        except ValueError as exc:
            errors.append({"index": index, "status": "error", "message": str(exc)})
    return LessonBatch(language, source, lesson_date, tuple(parsed), tuple(errors))


def _parse_card(index: int, raw: dict[str, Any], language: str, source: str) -> LessonCard:
    requested_type = str(raw.get("type", "")).strip().casefold()
    key = TYPE_ALIASES.get(requested_type)
    if key is None:
        raise ValueError(f"unsupported card type: {requested_type or '(empty)'}")
    spec = CARD_TYPES[key]
    if spec.key == "spanish_conjugation" and language != "spanish":
        raise ValueError("Spanish Conjugation requires a Spanish lesson")

    cefr = str(raw.get("cefr", "")).strip().upper()
    if cefr not in ALLOWED_CEFR:
        raise ValueError("CEFR must be B1, B2, or C1")
    topic = slug(str(raw.get("topic", "")))
    if not topic:
        raise ValueError("topic is required")

    fields = {name: "" for name in spec.fields}
    allowed_inputs = {name for name, field in INPUT_FIELDS.items() if field in spec.fields}
    unexpected = sorted(
        name for name in INPUT_FIELDS
        if name not in allowed_inputs and raw.get(name) not in (None, "")
    )
    if unexpected:
        raise ValueError(f"field(s) not valid for {spec.key}: {', '.join(unexpected)}")
    for input_name in allowed_inputs:
        value = raw.get(input_name)
        if value is not None:
            fields[INPUT_FIELDS[input_name]] = str(value).strip()
    for name, value in (("Language", language), ("CEFR", cefr), ("Topic", topic), ("Source", source)):
        if name in fields:
            fields[name] = value

    missing = [name for name in spec.required_fields if not fields[name].strip()]
    if missing:
        raise ValueError("missing required field(s): " + ", ".join(missing))
    if spec.cloze and not re.search(r"\{\{c\d+::.+?\}\}", fields["Text"], re.IGNORECASE):
        raise ValueError("Text must contain an Anki cloze such as {{c1::answer}}")
    if any(len(value) > 10_000 for value in fields.values()):
        raise ValueError("card fields must be at most 10,000 characters")

    tags = {
        f"language::{language}", f"cefr::{cefr}", f"topic::{topic}", f"source::{source}",
        *spec.type_tags,
    }
    if requested_type == "collocation":
        tags.add("type::collocation")
    return LessonCard(index, requested_type, spec, fields, tuple(sorted(tags)))
