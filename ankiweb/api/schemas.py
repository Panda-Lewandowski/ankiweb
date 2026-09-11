from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CheckRequest(BaseModel):
    typed_answer: str = Field(default="", max_length=10_000)
    case_sensitive: bool = True
    punctuation_sensitive: bool = True


class AnswerRequest(BaseModel):
    rating: Literal["again", "hard", "good", "easy"]
    continue_session: bool = True


class LessonContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Literal["spanish", "english"]
    source: Literal[
        "chatgpt_lesson", "teacher_lesson", "speaking", "writing", "listening",
    ]
    date: date


class LessonCardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=64)
    cefr: Literal["B1", "B2", "C1"]
    topic: str = Field(min_length=1, max_length=100)
    prompt: str | None = Field(default=None, max_length=10_000)
    answer: str | None = Field(default=None, max_length=10_000)
    example: str | None = Field(default=None, max_length=10_000)
    translation: str | None = Field(default=None, max_length=10_000)
    target: str | None = Field(default=None, max_length=10_000)
    text: str | None = Field(default=None, max_length=10_000)
    back_extra: str | None = Field(default=None, max_length=10_000)
    explanation: str | None = Field(default=None, max_length=10_000)
    original_error: str | None = Field(default=None, max_length=10_000)
    verb: str | None = Field(default=None, max_length=10_000)
    tense: str | None = Field(default=None, max_length=10_000)
    person: str | None = Field(default=None, max_length=10_000)
    sentence: str | None = Field(default=None, max_length=10_000)
    note: str | None = Field(default=None, max_length=10_000)

    @field_validator("type", "topic")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class LessonBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lesson: LessonContextRequest
    cards: list[LessonCardRequest] = Field(min_length=1, max_length=50)
    commit: bool = False
