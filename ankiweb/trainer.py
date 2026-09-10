from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response

from ankiweb.assets import _mime


def build_trainer_router(frontend_dir: Path) -> APIRouter:
    """Serve the built product SPA without exposing arbitrary filesystem paths."""
    router = APIRouter(include_in_schema=False)

    @router.get("/trainer")
    def trainer_redirect() -> Response:
        return RedirectResponse("/trainer/", status_code=307)

    @router.get("/trainer/{path:path}")
    def trainer(path: str) -> Response:
        root = frontend_dir.resolve()
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return PlainTextResponse("forbidden", status_code=403)

        if target.is_file():
            immutable = path.startswith("assets/") and "." in Path(path).name
            headers = {"Cache-Control": "public, max-age=31536000, immutable"} if immutable else {}
            return FileResponse(target, media_type=_mime(path), headers=headers)

        index = root / "index.html"
        if index.is_file():
            return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})
        return PlainTextResponse(
            "Language Trainer frontend is not built. Run: npm --prefix web run build",
            status_code=503,
        )

    return router
