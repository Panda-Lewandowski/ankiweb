from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ankiweb.trainer import build_trainer_router


def test_trainer_serves_spa_and_static_assets(tmp_path: Path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<main>Language Trainer</main>")
    (tmp_path / "assets" / "app.js").write_text("console.log('trainer')")
    app = FastAPI()
    app.include_router(build_trainer_router(tmp_path))
    client = TestClient(app)

    assert client.get("/trainer").history[0].status_code == 307
    assert "Language Trainer" in client.get("/trainer/").text
    asset = client.get("/trainer/assets/app.js")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "Language Trainer" in client.get("/trainer/review/spanish").text


def test_trainer_returns_actionable_error_before_build(tmp_path: Path):
    app = FastAPI()
    app.include_router(build_trainer_router(tmp_path))
    response = TestClient(app).get("/trainer/")
    assert response.status_code == 503
    assert "npm --prefix web run build" in response.text
