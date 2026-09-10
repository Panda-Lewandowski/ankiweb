from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response

from ankiweb.assets import _mime


def build_trainer_router(frontend_dir: Path) -> APIRouter:
    """Serve the built product SPA without exposing arbitrary filesystem paths."""
    router = APIRouter(include_in_schema=False)

    def static_file(path: str) -> Response:
        root = frontend_dir.resolve()
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return PlainTextResponse("forbidden", status_code=403)

        if not target.is_file():
            return PlainTextResponse("not found", status_code=404)
        immutable = path.startswith("assets/") and "." in Path(path).name
        headers = {"Cache-Control": "public, max-age=31536000, immutable"} if immutable else {}
        return FileResponse(target, media_type=_mime(path), headers=headers)

    @router.get("/")
    @router.get("/index.html")
    def trainer() -> Response:
        root = frontend_dir.resolve()
        index = root / "index.html"
        if index.is_file():
            return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})
        return PlainTextResponse(
            "Language Trainer frontend is not built. Run: npm --prefix web run build",
            status_code=503,
        )

    @router.get("/assets/{path:path}")
    def trainer_asset(path: str) -> Response:
        return static_file(f"assets/{path}")

    @router.get("/manifest.webmanifest")
    @router.get("/registerSW.js")
    @router.get("/sw.js")
    @router.get("/icon.svg")
    def trainer_public_file(request: Request) -> Response:
        return static_file(request.url.path.removeprefix("/"))

    @router.get("/workbox-{digest}.js")
    def trainer_workbox(digest: str) -> Response:
        return static_file(f"workbox-{digest}.js")

    @router.get("/trainer")
    @router.get("/trainer/{path:path}")
    def old_trainer_url(path: str = "") -> Response:
        return RedirectResponse("/", status_code=307)

    return router
