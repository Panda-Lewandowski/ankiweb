from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ankiweb.trainer import build_trainer_router


def test_trainer_serves_spa_and_static_assets(tmp_path: Path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<main>Language Trainer</main>")
    (tmp_path / "assets" / "app.js").write_text("console.log('trainer')")
    (tmp_path / "manifest.webmanifest").write_text("{}")
    app = FastAPI()
    app.include_router(build_trainer_router(tmp_path))
    client = TestClient(app)

    assert "Language Trainer" in client.get("/").text
    assert client.get("/trainer", follow_redirects=False).headers["location"] == "/"
    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/missing.js").status_code == 404


def test_trainer_returns_actionable_error_before_build(tmp_path: Path):
    app = FastAPI()
    app.include_router(build_trainer_router(tmp_path))
    response = TestClient(app).get("/")
    assert response.status_code == 503
    assert "npm --prefix web run build" in response.text
