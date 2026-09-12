import asyncio
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from ankiweb.anki_core import AnkiAdapter
from ankiweb.anki_core.migration import compare
from ankiweb.app import create_app
from ankiweb.auth import CSRF_COOKIE, hash_password
from ankiweb.collection_service import CollectionService
from ankiweb.config import Settings
from ankiweb.operations import DailyBackups, backup_bundle, verify_bundle
from ankiweb.production import validate

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("migration_fixture", Path(__file__).with_name("test_migration.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def production_settings(tmp_path):
    return Settings(collection_path=tmp_path / "anki/current/collection.anki2",
                    backups_dir=tmp_path / "backups", production_mode=True,
                    password_hash=hash_password("fixture-password"),
                    app_secret="independent-fixture-secret-0123456789",
                    allowed_hosts=("testserver",), auth_allowed_origins=("https://testserver",),
                    source_url="https://example.test/source/" + "a" * 40)


@pytest.mark.parametrize("overrides", [
    {"password_hash": ""}, {"cookie_secure": False}, {"app_secret": "short"},
    {"allowed_hosts": ("*",)}, {"auth_allowed_origins": ("http://testserver",)},
    {"source_url": "https://example.test/main"}, {"backups_dir": None},
])
def test_production_fails_closed(tmp_path, overrides):
    with pytest.raises(ValueError):
        validate(replace(production_settings(tmp_path), **overrides))


def test_multiple_workers_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(ValueError, match="one application worker"):
        validate(production_settings(tmp_path))


async def test_lifetime_lock_blocks_other_process_and_releases(tmp_path):
    service = CollectionService(Settings(collection_path=tmp_path / "collection.anki2"))
    await service.open()
    code = """
import asyncio, sys
from pathlib import Path
from ankiweb.collection_service import CollectionService
from ankiweb.config import Settings
async def main():
    s = CollectionService(Settings(collection_path=Path(sys.argv[1])))
    try:
        await s.open()
    except RuntimeError:
        sys.exit(7)
    await s.close()
asyncio.run(main())
"""
    try:
        command = [sys.executable, "-c", code, str(service.settings.collection_path)]
        assert subprocess.run(command, timeout=10).returncode == 7
        await AnkiAdapter(service).migration_export(tmp_path / "export.colpkg")
        assert subprocess.run(command, timeout=10).returncode == 7
    finally:
        await service.close()
    assert subprocess.run(command, timeout=10).returncode == 0


async def test_backup_restore_preview_fsrs_receipts_and_current_untouched(tmp_path):
    collection = tmp_path / "current/collection.anki2"
    service = CollectionService(Settings(collection_path=collection))
    await service.open()
    try:
        await service.run(fixture.seed)
        adapter = AnkiAdapter(service)
        card = await adapter.next_review("spanish", "fixture")
        await adapter.check(card["token"], "fixture", "x", case_sensitive=False, punctuation_sensitive=False)
        await adapter.answer(card["token"], "fixture", "easy", continue_session=False)
        receipt_dir = collection.parent / "lesson-receipts"
        receipt_dir.mkdir(exist_ok=True)
        (receipt_dir / "fixture.json").write_text('{"fixture":true}')
        before = await adapter.migration_snapshot()
        assert before["summary"]["fsrs_cards"] == 1 and before["summary"]["reviews"] == 1
        bundle = await backup_bundle(adapter, tmp_path / "backups")
        assert compare(before, await adapter.migration_snapshot())["equal"]
        verify_bundle(bundle["bundle"])
    finally:
        await service.close()
    target = tmp_path / "restored/collection.anki2"
    command = [sys.executable, str(ROOT / "tools/maintenance.py"), "restore", "--collection", str(collection),
               "--backups", str(tmp_path / "pre-backups"), "--bundle", bundle["bundle"], "--target", str(target)]
    assert subprocess.run(command, capture_output=True, timeout=15).returncode == 0
    assert not target.parent.exists() and not (tmp_path / "pre-backups").exists()
    assert subprocess.run(command + ["--apply"], capture_output=True, timeout=15).returncode != 0
    assert not target.parent.exists()
    applied = subprocess.run(command + ["--apply", "--sha256", bundle["sha256"],
                                        "--confirm-target", str(target)], capture_output=True, timeout=30)
    assert applied.returncode == 0, applied.stderr.decode()
    report = json.loads((target.parent / "restore-report.json").read_text())
    assert report["passed"] and not report["activated"]
    assert (target.parent / "lesson-receipts/fixture.json").read_text() == '{"fixture":true}'
    assert len(list((tmp_path / "pre-backups").glob("*/manifest.json"))) == 1
    for name, path in (("current", collection), ("restored", target)):
        state = fixture.run_worker("snapshot", path, tmp_path / f"{name}.json")
        assert compare(before, state)["equal"]
    # Existing restore destinations are refused even during preview.
    assert subprocess.run(command, capture_output=True, timeout=15).returncode != 0
    (Path(bundle["bundle"]) / "collection.colpkg").write_bytes(b"corrupt fixture")
    with pytest.raises(ValueError, match="checksum"):
        verify_bundle(bundle["bundle"])


async def test_daily_backup_and_failure_health(tmp_path, monkeypatch):
    service = CollectionService(Settings(collection_path=tmp_path / "current/collection.anki2"))
    await service.open()
    scheduler = DailyBackups(AnkiAdapter(service), tmp_path / "backups", interval=86400)
    task = asyncio.create_task(scheduler.run())
    try:
        for _ in range(100):
            if scheduler.last_success:
                break
            await asyncio.sleep(.02)
        assert scheduler.healthy()
        assert len(list((tmp_path / "backups").glob("daily-*/manifest.json"))) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await service.close()
    scheduler.failed = True
    assert not scheduler.healthy()


async def test_failed_backup_is_not_success_and_logs_no_exception_text(tmp_path, caplog):
    class BrokenAdapter:
        async def operational_backup(self, *args, **kwargs):
            raise OSError("private-card-text-and-secret")
    scheduler = DailyBackups(BrokenAdapter(), tmp_path, retry=3600)
    task = asyncio.create_task(scheduler.run())
    try:
        await asyncio.sleep(.02)
        assert scheduler.failed and not scheduler.healthy() and scheduler.last_success == 0
        assert "backup_failed" in caplog.text
        assert "private-card-text-and-secret" not in caplog.text
        assert not list(tmp_path.glob("*/manifest.json"))
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_cancelled_open_holds_lock_until_handle_closed(tmp_path, monkeypatch):
    import threading
    import ankiweb.collection_service as module
    real = module.Collection
    started, release = threading.Event(), threading.Event()
    def slow_open(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        return real(*args, **kwargs)
    monkeypatch.setattr(module, "Collection", slow_open)
    settings = Settings(collection_path=tmp_path / "collection.anki2")
    owner, contender = CollectionService(settings), CollectionService(settings)
    opening = asyncio.create_task(owner.open())
    await asyncio.to_thread(started.wait, 5)
    opening.cancel()
    with pytest.raises(RuntimeError, match="writable owner"):
        await contender.open()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await opening
    await contender.open()
    await contender.close()


def test_production_surface_auth_body_limit_and_health(tmp_path):
    settings = production_settings(tmp_path)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        assert client.get("/api/health").status_code == 401
        assert client.post("/login", data={"password": "fixture-password"},
                           headers={"Origin": "http://testserver"}).status_code == 403
        assert client.post("/login", content=b"x" * (settings.max_request_bytes + 1)).status_code == 413
        assert client.post("/login", data={"password": "fixture-password"}, follow_redirects=False).status_code == 303
        headers = {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE)}
        for url in ("/docs", "/openapi.json", "/deckbrowser", "/settings", "/export"):
            assert client.get(url, follow_redirects=False).status_code == 404
        assert client.post("/_anki/getDeckNames", headers=headers, content=b"").status_code in {404, 405}
        assert "Complete Corresponding Source" in client.get("/about").text
        assert client.get("/api/health").status_code == 200
        client.app.state.backups.failed = True
        assert client.get("/healthz").status_code == 503
        assert client.get("/healthz").json() == {"ok": False}
        assert not any(getattr(route, "path", "") == "/ws" for route in client.app.routes)
