"""Production-capable authentication for the web application.

Authentication is optional for loopback development. When enabled, the configured
credential is an Argon2id hash and browser sessions are opaque, revocable records in a
small SQLite database beside the collection. This database never contains Anki data.
"""
from __future__ import annotations

from dataclasses import dataclass
import argparse
import getpass
import hashlib
import hmac
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


LOGGER = logging.getLogger("ankiweb.auth")
COOKIE = "ankiweb_session"
CSRF_COOKIE = "ankiweb_csrf"
OPEN_PATHS = frozenset({"/login", "/healthz"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_HASHER = PasswordHasher(type=Type.ID)


class AuthConfigurationError(ValueError):
    """Raised when production authentication is only partially configured."""


@dataclass(frozen=True)
class AuthSession:
    token_hash: str
    csrf_hash: str
    created_at: int
    last_seen_at: int
    expires_at: int


def hash_password(password: str) -> str:
    """Return an Argon2id encoded hash suitable for ``ANKIWEB_PASSWORD_HASH``."""
    if not password:
        raise ValueError("password must not be empty")
    return _HASHER.hash(password)


def _origin_allowed(origin: str | None, host: str, allowed_origins: tuple[str, ...]) -> bool:
    """Validate a browser Origin against the request Host or an explicit allow-list."""
    if not origin:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        return False
    normalized = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    explicit = {value.rstrip("/") for value in allowed_origins}
    return normalized in explicit or parsed.netloc.casefold() == host.casefold()


class AuthManager:
    """Own password verification, rate limits, audit events, and browser sessions."""

    def __init__(self, settings, *, clock: Callable[[], float] = time.time) -> None:
        self.enabled = bool(settings.password_hash)
        self._password_hash = settings.password_hash
        self._clock = clock
        self._idle_seconds = int(settings.session_idle_seconds)
        self._touch_interval = max(1, min(60, self._idle_seconds // 2))
        self._absolute_seconds = int(settings.session_absolute_seconds)
        self._max_attempts = int(settings.login_max_attempts)
        self._base_delay = int(settings.login_base_delay_seconds)
        self._max_delay = int(settings.login_max_delay_seconds)
        self._attempt_window = int(settings.login_attempt_window_seconds)
        self._allowed_origins = tuple(settings.auth_allowed_origins)
        self._lock = threading.RLock()

        if not self.enabled:
            self._secret = b""
            self._database = None
            return
        try:
            parameters = extract_parameters(self._password_hash)
        except InvalidHashError as exc:
            raise AuthConfigurationError(
                "ANKIWEB_PASSWORD_HASH must be an Argon2id encoded hash"
            ) from exc
        if parameters.type is not Type.ID:
            raise AuthConfigurationError("ANKIWEB_PASSWORD_HASH must be an Argon2id encoded hash")
        if len(settings.app_secret) < 32:
            raise AuthConfigurationError(
                "ANKIWEB_APP_SECRET must be an independent random value of at least 32 characters"
            )
        if self._idle_seconds <= 0 or self._absolute_seconds <= 0:
            raise AuthConfigurationError("session lifetimes must be positive")
        if self._idle_seconds > self._absolute_seconds:
            raise AuthConfigurationError("session idle lifetime cannot exceed absolute lifetime")
        if (self._max_attempts < 1 or self._base_delay < 1 or self._max_delay < self._base_delay
                or self._attempt_window < 1):
            raise AuthConfigurationError("login rate-limit settings are invalid")
        self._secret = settings.app_secret.encode("utf-8")
        self._database = Path(settings.auth_db_path or (
            settings.collection_path.parent / "language-trainer-auth.sqlite3"
        ))
        if self._database.resolve() == Path(settings.collection_path).resolve():
            raise AuthConfigurationError("the auth database must be separate from the Anki collection")
        self._initialize()
        self._revoke_changed_password_sessions()

    @property
    def database_path(self) -> Path | None:
        return self._database

    def password_ok(self, submitted: str) -> bool:
        if not self.enabled:
            return False
        try:
            return bool(_HASHER.verify(self._password_hash, submitted or ""))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def login_allowed(self, remote: str) -> tuple[bool, int]:
        now = self._now()
        remote_hash = self._digest("remote", remote)
        with self._connect() as db:
            row = db.execute(
                "SELECT blocked_until FROM login_attempts WHERE remote_hash = ?",
                (remote_hash,),
            ).fetchone()
        if row and int(row[0]) > now:
            return False, max(1, int(row[0]) - now)
        return True, 0

    def record_login_failure(self, remote: str) -> int:
        now = self._now()
        remote_hash = self._digest("remote", remote)
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT failures, last_failure FROM login_attempts WHERE remote_hash = ?",
                (remote_hash,),
            ).fetchone()
            failures = 1
            if row and now - int(row[1]) <= self._attempt_window:
                failures = int(row[0]) + 1
            delay = 0
            if failures >= self._max_attempts:
                exponent = min(failures - self._max_attempts, 16)
                delay = min(self._max_delay, self._base_delay * (2 ** exponent))
            db.execute(
                "INSERT INTO login_attempts(remote_hash, failures, last_failure, blocked_until) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(remote_hash) DO UPDATE SET "
                "failures=excluded.failures, last_failure=excluded.last_failure, "
                "blocked_until=excluded.blocked_until",
                (remote_hash, failures, now, now + delay),
            )
            self._audit(db, "login_failure", remote_hash, f"attempt={failures}")
        LOGGER.warning("authentication failure remote=%s attempt=%d", remote_hash[:12], failures)
        return delay

    def issue_session(self, remote: str, previous_token: str | None = None) -> tuple[str, str]:
        now = self._now()
        remote_hash = self._digest("remote", remote)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        token_hash = self._digest("session", token)
        csrf_hash = self._digest("csrf", csrf)
        password_fingerprint = self._digest("password", self._password_hash)
        with self._lock, self._connect() as db:
            if previous_token:
                db.execute(
                    "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                    (now, self._digest("session", previous_token)),
                )
            db.execute("DELETE FROM login_attempts WHERE remote_hash = ?", (remote_hash,))
            db.execute(
                "INSERT INTO sessions(token_hash, csrf_hash, password_fingerprint, created_at, "
                "last_seen_at, expires_at, revoked_at) VALUES(?, ?, ?, ?, ?, ?, NULL)",
                (token_hash, csrf_hash, password_fingerprint, now, now, now + self._absolute_seconds),
            )
            self._audit(db, "login_success", remote_hash, "session_issued")
        return token, csrf

    def authenticate(self, token: str | None) -> AuthSession | None:
        if not self.enabled or not token:
            return None
        now = self._now()
        token_hash = self._digest("session", token)
        fingerprint = self._digest("password", self._password_hash)
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT csrf_hash, password_fingerprint, created_at, last_seen_at, expires_at, "
                "revoked_at FROM sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if not row:
                return None
            csrf_hash, stored_fingerprint, created_at, last_seen_at, expires_at, revoked_at = row
            expired = now >= int(expires_at) or now - int(last_seen_at) >= self._idle_seconds
            invalid = revoked_at is not None or not hmac.compare_digest(
                str(stored_fingerprint), fingerprint
            )
            if expired or invalid:
                if revoked_at is None:
                    db.execute(
                        "UPDATE sessions SET revoked_at = ? WHERE token_hash = ?", (now, token_hash)
                    )
                return None
            effective_last_seen = int(last_seen_at)
            if now - effective_last_seen >= self._touch_interval:
                db.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now, token_hash)
                )
                effective_last_seen = now
        return AuthSession(
            token_hash=token_hash,
            csrf_hash=str(csrf_hash),
            created_at=int(created_at),
            last_seen_at=effective_last_seen,
            expires_at=int(expires_at),
        )

    def csrf_ok(self, session: AuthSession, supplied: str | None) -> bool:
        if not supplied:
            return False
        return hmac.compare_digest(session.csrf_hash, self._digest("csrf", supplied))

    def origin_ok(self, origin: str | None, host: str) -> bool:
        return _origin_allowed(origin, host, self._allowed_origins)

    def revoke_session(self, token: str | None, remote: str) -> None:
        if not self.enabled or not token:
            return
        now = self._now()
        remote_hash = self._digest("remote", remote)
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (now, self._digest("session", token)),
            )
            self._audit(db, "logout", remote_hash, "session_revoked")

    def audit_events(self, limit: int = 100) -> list[dict[str, str | int]]:
        """Read sanitized audit metadata; intended for tests and future admin diagnostics."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT occurred_at, event, remote_hash, detail FROM auth_events "
                "ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [
            {"occurred_at": int(row[0]), "event": str(row[1]),
             "remote_hash": str(row[2]), "detail": str(row[3])}
            for row in rows
        ]

    def _now(self) -> int:
        return int(self._clock())

    def _digest(self, purpose: str, value: str) -> str:
        return hmac.new(
            self._secret, purpose.encode("ascii") + b"\0" + value.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        if self._database is None:
            raise RuntimeError("authentication is disabled")
        db = sqlite3.connect(self._database, timeout=5)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _initialize(self) -> None:
        assert self._database is not None
        self._database.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    csrf_hash TEXT NOT NULL,
                    password_fingerprint TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS login_attempts (
                    remote_hash TEXT PRIMARY KEY,
                    failures INTEGER NOT NULL,
                    last_failure INTEGER NOT NULL,
                    blocked_until INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    remote_hash TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                """
            )
        try:
            os.chmod(self._database, 0o600)
        except OSError:
            pass

    def _revoke_changed_password_sessions(self) -> None:
        now = self._now()
        fingerprint = self._digest("password", self._password_hash)
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE sessions SET revoked_at = ? WHERE revoked_at IS NULL "
                "AND password_fingerprint != ?",
                (now, fingerprint),
            )
            if cursor.rowcount:
                self._audit(
                    db, "password_change", self._digest("remote", "system"),
                    f"sessions_revoked={cursor.rowcount}",
                )

    def _audit(self, db: sqlite3.Connection, event: str, remote_hash: str, detail: str) -> None:
        db.execute(
            "INSERT INTO auth_events(occurred_at, event, remote_hash, detail) VALUES(?, ?, ?, ?)",
            (self._now(), event, remote_hash, detail),
        )
        db.execute(
            "DELETE FROM auth_events WHERE id NOT IN "
            "(SELECT id FROM auth_events ORDER BY id DESC LIMIT 5000)"
        )


def _main() -> None:
    parser = argparse.ArgumentParser(description="Language Trainer authentication utilities")
    parser.add_argument("command", choices=("hash-password",))
    args = parser.parse_args()
    if args.command == "hash-password":
        first = getpass.getpass("Password: ")
        second = getpass.getpass("Confirm password: ")
        if first != second:
            raise SystemExit("passwords do not match")
        print(hash_password(first))


if __name__ == "__main__":
    _main()
