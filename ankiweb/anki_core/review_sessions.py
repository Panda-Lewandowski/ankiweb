from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


class ReviewSessionError(Exception):
    status_code = 409


class ReviewTokenUnknown(ReviewSessionError):
    status_code = 404


class ReviewTokenExpired(ReviewSessionError):
    status_code = 410


class ReviewTokenConsumed(ReviewSessionError):
    status_code = 409


class ReviewTokenNotRevealed(ReviewSessionError):
    status_code = 409


class ReviewConflict(ReviewSessionError):
    status_code = 409


@dataclass
class ReviewSession:
    token: str
    client_id: str
    language: str
    card_id: int
    note_id: int
    card: object
    states: object
    context: object
    collection_generation: int
    fingerprint: tuple
    question: dict
    back: dict
    expected: str | None
    combining: bool
    created_at: float
    expires_at: float
    revealed: bool = False


class ReviewSessionStore:
    """In-memory leases prevent two clients from answering the same queued card."""

    def __init__(self, ttl_seconds: int = 20 * 60) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, ReviewSession] = {}
        self._card_tokens: dict[int, str] = {}
        self._terminal: dict[str, str] = {}

    def _expire(self) -> None:
        now = time.monotonic()
        for session in list(self._sessions.values()):
            if session.expires_at <= now:
                self._remove(session, terminal="expired")

    def create(self, **kwargs) -> ReviewSession:
        self._expire()
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        session = ReviewSession(
            token=token,
            created_at=now,
            expires_at=now + self.ttl_seconds,
            **kwargs,
        )
        self._sessions[token] = session
        self._card_tokens[session.card_id] = token
        return session

    def get(self, token: str) -> ReviewSession:
        self._expire()
        session = self._sessions.get(token)
        if session:
            return session
        terminal = self._terminal.get(token)
        if terminal == "expired":
            raise ReviewTokenExpired("review token expired")
        if terminal == "consumed":
            raise ReviewTokenConsumed("review token was already answered")
        if terminal == "stale":
            raise ReviewConflict("review token is stale")
        raise ReviewTokenUnknown("review token not found")

    def for_client(self, client_id: str, language: str) -> ReviewSession | None:
        self._expire()
        return next(
            (s for s in self._sessions.values()
             if s.client_id == client_id and s.language == language),
            None,
        )

    def card_is_leased(self, card_id: int) -> bool:
        self._expire()
        return card_id in self._card_tokens

    def consume(self, session: ReviewSession) -> None:
        self._remove(session, terminal="consumed")

    def stale(self, session: ReviewSession) -> None:
        self._remove(session, terminal="stale")

    def release_card(self, card_id: int) -> None:
        token = self._card_tokens.get(card_id)
        if token and (session := self._sessions.get(token)):
            self._remove(session, terminal="stale")

    def _remove(self, session: ReviewSession, terminal: str) -> None:
        self._sessions.pop(session.token, None)
        if self._card_tokens.get(session.card_id) == session.token:
            self._card_tokens.pop(session.card_id, None)
        self._terminal[session.token] = terminal
        if len(self._terminal) > 4096:
            for token in list(self._terminal)[:1024]:
                self._terminal.pop(token, None)
