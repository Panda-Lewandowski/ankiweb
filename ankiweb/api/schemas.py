from typing import Literal

from pydantic import BaseModel, Field


class CheckRequest(BaseModel):
    typed_answer: str = Field(default="", max_length=10_000)
    case_sensitive: bool = True
    punctuation_sensitive: bool = True


class AnswerRequest(BaseModel):
    rating: Literal["again", "hard", "good", "easy"]
