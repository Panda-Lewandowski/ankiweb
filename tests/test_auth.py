from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ankiweb.app import create_app
from ankiweb.auth import AuthConfigurationError, AuthManager, COOKIE, CSRF_COOKIE, hash_password
from ankiweb.config import Settings


SECRET = "independent-test-secret-value-1234567890"


def _settings(tmp_path: Path, password: str = "secret", **overrides) -> Settings:
    values = {
        "collection_path": tmp_path / "c.anki2",
        "password_hash": hash_password(password),
        "app_secret": SECRET,
        "auth_db_path": tmp_path / "auth.sqlite3",
        "cookie_secure": False,
    }
    values.update(overrides)
    return Settings(**values)


def _login(client: TestClient, password: str = "secret"):
    response = client.post("/login", data={"password": password}, follow_redirects=False)
    assert response.status_code == 303
    return response, client.cookies.get(COOKIE), client.cookies.get(CSRF_COOKIE)


def test_open_when_no_password_hash(tmp_path: Path):
    settings = Settings(collection_path=tmp_path / "c.anki2")
    with TestClient(create_app(settings)) as client:
        assert client.get("/deckbrowser", follow_redirects=False).status_code == 200
        assert client.get("/tools", follow_redirects=False).status_code == 200
        assert not (tmp_path / "language-trainer-auth.sqlite3").exists()


def test_from_env_reads_hash_and_rejects_plaintext(monkeypatch, tmp_path: Path):
    encoded = hash_password("hunter2")
    monkeypatch.setenv("ANKIWEB_COLLECTION", str(tmp_path / "c.anki2"))
    monkeypatch.setenv("ANKIWEB_PASSWORD_HASH", encoded)
    monkeypatch.setenv("ANKIWEB_APP_SECRET", SECRET)
    settings = Settings.from_env()
    assert settings.password_hash == encoded
    assert settings.app_secret == SECRET

    monkeypatch.setenv("ANKIWEB_PASSWORD", "hunter2")
    with pytest.raises(ValueError, match="no longer accepted"):
        Settings.from_env()


def test_invalid_auth_configuration_fails_closed(tmp_path: Path):
    with pytest.raises(AuthConfigurationError, match="Argon2id"):
        create_app(Settings(
            collection_path=tmp_path / "c.anki2", password_hash="not-a-hash",
            app_secret=SECRET,
        ))
    with pytest.raises(AuthConfigurationError, match="APP_SECRET"):
        create_app(Settings(
            collection_path=tmp_path / "c.anki2", password_hash=hash_password("secret"),
            app_secret="short",
        ))
    collection = tmp_path / "same.anki2"
    with pytest.raises(AuthConfigurationError, match="separate"):
        create_app(Settings(
            collection_path=collection, password_hash=hash_password("secret"),
            app_secret=SECRET, auth_db_path=collection,
        ))


def test_gate_protects_pages_api_docs_and_detailed_health(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.get("/deckbrowser", follow_redirects=False).status_code == 303
        assert client.get("/docs", follow_redirects=False).status_code == 303
        assert client.get("/openapi.json", follow_redirects=False).status_code == 303
        assert client.get("/api/health").status_code == 401
        assert client.get("/api/lesson-cards/receipts").status_code == 401
        assert client.get("/healthz").json() == {"ok": True}
        assert client.get("/login", follow_redirects=False).status_code == 200


def test_login_uses_opaque_server_session_and_secure_cookie_attributes(tmp_path: Path):
    settings = _settings(tmp_path, cookie_secure=True)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        response, token, csrf = _login(client)
        cookies = response.headers.get_list("set-cookie")
        assert token and csrf and token != csrf and "secret" not in token
        assert any("HttpOnly" in value and "SameSite=strict" in value and "Secure" in value
                   for value in cookies if value.startswith(f"{COOKIE}="))
        assert any("HttpOnly" not in value and "SameSite=strict" in value and "Secure" in value
                   for value in cookies if value.startswith(f"{CSRF_COOKIE}="))
        assert client.get("/deckbrowser", follow_redirects=False).status_code == 200

    raw_database = settings.auth_db_path.read_bytes()
    assert token.encode() not in raw_database
    assert csrf.encode() not in raw_database


def test_wrong_password_rate_limit_and_sanitized_audit(tmp_path: Path):
    settings = _settings(tmp_path)
    manager = AuthManager(settings)
    with TestClient(create_app(settings, auth_manager=manager)) as client:
        for _ in range(settings.login_max_attempts):
            response = client.post("/login", data={"password": "very-secret-wrong-value"})
            assert response.status_code == 401
        blocked = client.post("/login", data={"password": "secret"})
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) >= 1

    events = manager.audit_events()
    assert events[0]["event"] == "login_failure"
    assert "very-secret-wrong-value" not in str(events)
    assert "testclient" not in str(events)


def test_csrf_required_for_mutations_and_cross_origin_rejected(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        _, _, csrf = _login(client)
        missing = client.post("/api/cards/123/suspend")
        assert missing.status_code == 403
        passed_guard = client.post(
            "/api/cards/123/suspend", headers={"X-CSRF-Token": csrf},
        )
        assert passed_guard.status_code == 404
        cross_origin = client.post(
            "/api/cards/123/suspend",
            headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
        )
        assert cross_origin.status_code == 403


def test_form_csrf_is_read_from_body_without_leaking_into_url(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        _, _, csrf = _login(client)
        response = client.post(
            "/notify",
            data={"_csrf": csrf, "enabled": "false", "action": "save"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/notify"


def test_login_rotates_session_and_logout_revokes_it(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        _, first, _ = _login(client)
        _, second, csrf = _login(client)
        assert first != second

        client.cookies.set(COOKIE, first)
        assert client.get("/deckbrowser", follow_redirects=False).status_code == 303

        client.cookies.set(COOKIE, second)
        logout = client.post(
            "/logout", headers={"X-CSRF-Token": csrf}, follow_redirects=False,
        )
        assert logout.status_code == 303
        client.cookies.set(COOKIE, second)
        assert client.get("/deckbrowser", follow_redirects=False).status_code == 303


def test_session_survives_restart_but_password_hash_change_revokes_it(tmp_path: Path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as first_client:
        _, token, _ = _login(first_client)

    with TestClient(create_app(settings)) as reopened:
        reopened.cookies.set(COOKIE, token)
        assert reopened.get("/deckbrowser", follow_redirects=False).status_code == 200

    changed = _settings(tmp_path, password="different-password")
    with TestClient(create_app(changed)) as changed_client:
        changed_client.cookies.set(COOKIE, token)
        assert changed_client.get("/deckbrowser", follow_redirects=False).status_code == 303


def test_idle_and_absolute_expiration(tmp_path: Path):
    now = [1_000.0]
    settings = _settings(tmp_path, session_idle_seconds=10, session_absolute_seconds=30)
    manager = AuthManager(settings, clock=lambda: now[0])
    token, _ = manager.issue_session("testclient")
    assert manager.authenticate(token)
    now[0] = 1_009
    assert manager.authenticate(token)
    now[0] = 1_020
    assert manager.authenticate(token) is None

    token, _ = manager.issue_session("testclient")
    now[0] = 1_051
    assert manager.authenticate(token) is None


def test_ws_requires_valid_origin_and_session(tmp_path: Path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        _login(client)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws?context=browser") as ws:
                ws.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws?context=browser", headers={"Origin": "https://evil.example"},
            ) as ws:
                ws.receive_json()
        with client.websocket_connect(
            "/ws?context=browser", headers={"Origin": "http://testserver"},
        ):
            pass


def test_session_database_is_separate_from_collection(tmp_path: Path):
    settings = _settings(tmp_path)
    manager = AuthManager(settings)
    assert manager.database_path == tmp_path / "auth.sqlite3"
    with sqlite3.connect(manager.database_path) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert {"sessions", "login_attempts", "auth_events"} <= tables
