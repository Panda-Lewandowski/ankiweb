from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Response
from anki.errors import NotFoundError

from ankiweb.anki_core.adapter import AnkiAdapter
from ankiweb.anki_core.review_sessions import ReviewSessionError
from ankiweb.api.schemas import AnswerRequest, CheckRequest, LessonBatchRequest
from ankiweb.language.card_types import parse_lesson_batch
from ankiweb.tts import TTSUnavailable, synthesize_tts


def build_router(
    get_adapter: Callable[[], AnkiAdapter],
    tts_synthesizer: Callable[[str, str], bytes] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["language-trainer"])
    synthesizer = tts_synthesizer or synthesize_tts

    async def invoke(awaitable):
        try:
            return await awaitable
        except ReviewSessionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except (KeyError, NotFoundError) as exc:
            raise HTTPException(status_code=404, detail="card not found") from exc

    @router.get("/health")
    async def health():
        return await invoke(get_adapter().health())

    @router.get("/today")
    async def today(language: Literal["spanish", "english"]):
        return await invoke(get_adapter().today(language))

    @router.get("/review/next")
    async def next_review(
        response: Response,
        language: Literal["spanish", "english"],
        client_id: str = Header(default="anonymous", alias="X-Review-Client", max_length=128),
    ):
        result = await invoke(get_adapter().next_review(language, client_id))
        if result is None:
            response.status_code = 204
        return result

    @router.post("/review/{token}/check")
    async def check(
        token: str,
        request: CheckRequest,
        client_id: str = Header(default="anonymous", alias="X-Review-Client", max_length=128),
    ):
        return await invoke(get_adapter().check(
            token, client_id, request.typed_answer,
            case_sensitive=request.case_sensitive,
            punctuation_sensitive=request.punctuation_sensitive,
        ))

    @router.post("/review/{token}/answer")
    async def answer(
        token: str,
        request: AnswerRequest,
        client_id: str = Header(default="anonymous", alias="X-Review-Client", max_length=128),
    ):
        return await invoke(get_adapter().answer(
            token, client_id, request.rating, continue_session=request.continue_session))

    @router.get("/review/{token}/audio")
    async def listening_audio(
        token: str,
        client_id: str = Header(default="anonymous", alias="X-Review-Client", max_length=128),
    ):
        spec = await invoke(get_adapter().tts_spec(token, client_id))
        try:
            audio = await asyncio.to_thread(synthesizer, spec["text"], spec["locale"])
        except TTSUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={"Cache-Control": "private, no-store"},
        )

    @router.post("/cards/{card_id}/suspend")
    async def suspend(card_id: int):
        return await invoke(get_adapter().suspend(card_id))

    @router.post("/cards/{card_id}/unsuspend")
    async def unsuspend(card_id: int):
        return await invoke(get_adapter().unsuspend(card_id))

    @router.post("/cards/{card_id}/bury")
    async def bury(card_id: int):
        return await invoke(get_adapter().bury(card_id))

    @router.post("/lesson-cards/batch")
    async def lesson_cards_batch(request: LessonBatchRequest):
        payload = request.model_dump(mode="json", exclude={"commit"})
        batch = parse_lesson_batch(payload)
        return await invoke(get_adapter().lesson_cards_batch(batch, commit=request.commit))

    @router.get("/lesson-cards/receipts")
    async def lesson_import_receipts(limit: int = 20):
        return await invoke(get_adapter().lesson_receipts(max(1, min(limit, 50))))

    return router
