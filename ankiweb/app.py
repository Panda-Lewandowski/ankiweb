from __future__ import annotations
import html
import asyncio
import shutil
from collections.abc import Callable
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from ankiweb.config import Settings, host_allowed
from ankiweb.auth import AuthManager, COOKIE, CSRF_COOKIE, OPEN_PATHS, SAFE_METHODS


def _login_html(error: str = "") -> str:
    """Self-contained login page (no /_anki assets, so it works before authentication)."""
    err = (f"<p style='color:#c0392b;margin:0 0 12px'>{html.escape(error)}</p>"
           if error else "")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Language Trainer</title><style>"
        "body{font-family:system-ui,sans-serif;margin:0;min-height:100vh;display:flex;"
        "align-items:center;justify-content:center;background:#f0f0f0}"
        "form{background:#fff;padding:28px 34px;border-radius:10px;text-align:center;"
        "box-shadow:0 2px 10px rgba(0,0,0,.12)}h1{font-size:18px;margin:0 0 18px}"
        "input{font-size:16px;padding:9px 10px;width:220px;box-sizing:border-box}"
        "button{font-size:16px;padding:9px 22px;margin-top:14px;cursor:pointer;"
        "border:0;border-radius:6px;background:#2d7dd2;color:#fff}</style></head><body>"
        "<form method='post' action='/login'><h1>Language Trainer</h1>"
        f"{err}"
        "<input type='password' name='password' autofocus placeholder='密码 / Password'><br>"
        "<button type='submit'>进入 / Enter</button></form></body></html>"
    )
from ankiweb.collection_service import CollectionService
from ankiweb.bridge.hub import BridgeHub
from ankiweb.assets import build_router as build_assets_router, build_media_router, build_sveltekit_router
from ankiweb.anki_rpc import build_router as build_rpc_router
from ankiweb.bridge.ws import build_router as build_ws_router
from ankiweb.screens.routes import build_screen_router, register_screen_handlers
from ankiweb.notifier import NotifierState
from ankiweb.trainer import build_trainer_router
from ankiweb.anki_core import AnkiAdapter
from ankiweb.api.routes import build_router as build_language_api_router


def create_app(settings: Settings | None = None, service: CollectionService | None = None,
               hub: BridgeHub | None = None, notifier=None,
               tts_synthesizer: Callable[[str, str], bytes] | None = None,
               auth_manager: AuthManager | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.production_mode:
        from ankiweb.production import validate
        validate(settings)
    auth_manager = auth_manager or AuthManager(settings)
    owns = service is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = service
        if owns:
            svc = CollectionService(settings)
            await svc.open()
        h = hub if hub is not None else BridgeHub()
        svc.subscribe(lambda flags, initiator: h.broadcast_opchanges(flags, initiator))
        app.state.settings = settings
        app.state.auth = auth_manager
        app.state.service = svc
        app.state.anki_adapter = AnkiAdapter(svc)
        backup_task = None
        if settings.production_mode:
            from ankiweb.operations import DailyBackups
            app.state.backups = DailyBackups(app.state.anki_adapter, settings.backups_dir)
            backup_task = asyncio.create_task(app.state.backups.run())
        app.state.hub = h
        app.state.notifier = notifier if notifier is not None else NotifierState(
            settings.collection_path.parent / "notify.json")
        if not settings.production_mode:
            register_screen_handlers(svc, h)
        try:
            yield
        finally:
            if backup_task is not None:
                backup_task.cancel()
                try:
                    await backup_task
                except asyncio.CancelledError:
                    pass
            if owns:
                await svc.close()

    app = FastAPI(title="Language Trainer", lifespan=lifespan,
                  docs_url=None if settings.production_mode else "/docs",
                  redoc_url=None if settings.production_mode else "/redoc",
                  openapi_url=None if settings.production_mode else "/openapi.json")

    async def security_guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if not host_allowed(host, settings.allowed_hosts):
            return PlainTextResponse("forbidden host", status_code=403)
        if settings.production_mode and request.method not in SAFE_METHODS:
            if request.headers.get("origin") and request.headers["origin"] not in settings.auth_allowed_origins:
                return PlainTextResponse("forbidden origin", status_code=403)
            # Bound bodies before parsing (also covers chunked requests with no length).
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.max_request_bytes:
                    return PlainTextResponse("request too large", status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        if auth_manager.enabled and request.url.path not in OPEN_PATHS:
            session = auth_manager.authenticate(request.cookies.get(COOKIE))
            if session is None:
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"detail": "authentication required"}, status_code=401)
                return RedirectResponse("/login", status_code=303)
            request.state.auth_session = session
            if request.method.upper() not in SAFE_METHODS:
                origin = request.headers.get("origin")
                if origin and not auth_manager.origin_ok(origin, host):
                    return PlainTextResponse("forbidden origin", status_code=403)
                supplied = request.headers.get("x-csrf-token")
                content_type = request.headers.get("content-type", "").casefold()
                if not supplied and content_type.startswith("application/x-www-form-urlencoded"):
                    supplied = str((await request.form()).get("_csrf", ""))
                if not auth_manager.csrf_ok(session, supplied):
                    if request.url.path.startswith("/api/"):
                        return JSONResponse({"detail": "invalid CSRF token"}, status_code=403)
                    return PlainTextResponse("invalid CSRF token", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if settings.production_mode:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if auth_manager.enabled and request.url.path.startswith(("/api/", "/_anki/")):
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=security_guard)

    # --- specific routes FIRST, media catch-all LAST (Starlette matches in order) ---
    @app.get("/healthz")
    async def healthz():
        if settings.production_mode:
            try:
                await asyncio.wait_for(app.state.anki_adapter.health(), timeout=5)
                paths = (settings.collection_path.parent, settings.backups_dir)
                if not app.state.backups.healthy() or any(
                    shutil.disk_usage(path).free < settings.minimum_free_bytes for path in paths
                ):
                    raise RuntimeError("not ready")
            except Exception:
                return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if not auth_manager.enabled or auth_manager.authenticate(request.cookies.get(COOKIE)):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(_login_html(), headers={"Cache-Control": "no-store"})

    @app.post("/login")
    async def login_submit(request: Request):
        if not auth_manager.enabled:
            return RedirectResponse("/", status_code=303)
        remote = request.client.host if request.client else "unknown"
        allowed, retry_after = auth_manager.login_allowed(remote)
        if not allowed:
            return HTMLResponse(
                _login_html("Too many attempts. Try again later."),
                status_code=429,
                headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
            )
        form = await request.form()
        if auth_manager.password_ok(str(form.get("password", ""))):
            token, csrf = auth_manager.issue_session(remote, request.cookies.get(COOKIE))
            resp = RedirectResponse("/", status_code=303)
            common = {
                "secure": settings.cookie_secure,
                "samesite": "strict",
                "max_age": settings.session_absolute_seconds,
                "path": "/",
            }
            resp.set_cookie(COOKIE, token, httponly=True, **common)
            resp.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)
            return resp
        delay = auth_manager.record_login_failure(remote)
        headers = {"Cache-Control": "no-store"}
        if delay:
            headers["Retry-After"] = str(delay)
        return HTMLResponse(
            _login_html("Wrong password"), status_code=401, headers=headers,
        )

    @app.post("/logout")
    def logout(request: Request):
        remote = request.client.host if request.client else "unknown"
        auth_manager.revoke_session(request.cookies.get(COOKIE), remote)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/", secure=settings.cookie_secure, samesite="strict")
        resp.delete_cookie(CSRF_COOKIE, path="/", secure=settings.cookie_secure,
                           samesite="strict")
        return resp

    static_dir = settings.shell_dir / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/shell/static", StaticFiles(directory=str(static_dir), check_dir=False), name="shell")

    if not settings.production_mode:
        app.include_router(build_assets_router(settings.assets_dir))
        app.include_router(build_rpc_router(lambda: app.state.service, lambda: app.state.hub))
        app.include_router(build_ws_router(
            lambda: app.state.hub, settings.allowed_hosts, lambda: app.state.auth,
        ))
        app.include_router(build_screen_router(lambda: app.state.service, lambda: app.state.notifier))
    else:
        @app.get("/about", response_class=HTMLResponse)
        def production_about():
            return ("<h1>Language Trainer</h1><p>Unofficial; not affiliated with Anki/Ankitects.</p>"
                    "<p>AGPL-3.0-or-later · <a href='" + html.escape(settings.source_url, quote=True)
                    + "'>Complete Corresponding Source</a></p>")
    language_api = build_language_api_router(lambda: app.state.anki_adapter, tts_synthesizer)
    app.include_router(language_api)  # stable /api product boundary
    app.include_router(build_trainer_router(settings.trainer_dir))       # GET /trainer/ product SPA
    if not settings.production_mode:
        app.include_router(build_sveltekit_router(settings.assets_dir))
    app.include_router(build_media_router(lambda: app.state.service))  # GET  /{path} — LAST

    return app
