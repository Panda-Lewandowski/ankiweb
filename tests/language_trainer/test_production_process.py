"""Native production process: startup backup, single owner, SIGTERM and reopen."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from ankiweb.anki_core import AnkiAdapter
from ankiweb.anki_core.migration import compare
from ankiweb.auth import hash_password
from ankiweb.collection_service import CollectionService
from ankiweb.config import Settings


def test_production_sigterm_backup_and_restart(tmp_path):
    collection = tmp_path / "current/collection.anki2"

    async def seed_and_audit():
        from test_operations import fixture
        service = CollectionService(Settings(collection_path=collection))
        await service.open()
        try:
            await service.run(fixture.seed)
            adapter = AnkiAdapter(service)
            card = await adapter.next_review("spanish", "fixture")
            await adapter.check(card["token"], "fixture", "x", case_sensitive=False, punctuation_sensitive=False)
            await adapter.answer(card["token"], "fixture", "easy", continue_session=False)
            return await adapter.migration_snapshot()
        finally:
            await service.close()

    before = asyncio.run(seed_and_audit())
    assert before["summary"]["reviews"] == 1 and before["summary"]["fsrs_cards"] == 1
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ANKIWEB_", "UVICORN_"))}
    env.update(ANKIWEB_PRODUCTION="true", ANKIWEB_COLLECTION=str(collection),
               ANKIWEB_BACKUPS=str(tmp_path / "backups"), ANKIWEB_HOST="127.0.0.1", ANKIWEB_PORT=str(port),
               ANKIWEB_ALLOWED_HOSTS="fixture.test", ANKIWEB_AUTH_ALLOWED_ORIGINS="https://fixture.test",
               ANKIWEB_SOURCE_URL="https://example.test/source/" + "a" * 40,
               ANKIWEB_PASSWORD_HASH=hash_password("private-fixture"),
               ANKIWEB_APP_SECRET="independent-test-secret-value-1234567890", WEB_CONCURRENCY="1")
    command = [sys.executable, "-m", "ankiweb.production"]
    for iteration in range(2):
        process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while True:
                assert process.poll() is None, process.stderr.read().decode()
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                        if json.load(response) == {"ok": True}:
                            break
                except (urllib.error.URLError, TimeoutError):
                    pass
                assert time.monotonic() < deadline, "production readiness timeout"
                time.sleep(.05)
            duplicate = subprocess.run(command, env=env, capture_output=True, timeout=15)
            assert duplicate.returncode != 0
            assert b"writable owner" in duplicate.stderr
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise AssertionError("SIGTERM did not stop production gracefully")
        assert process.returncode in (0, -15)
        from test_operations import fixture
        after = fixture.run_worker("snapshot", collection, tmp_path / f"reopen-{iteration}.json")
        assert compare(before, after)["equal"]
    assert len(list((tmp_path / "backups").glob("daily-*/manifest.json"))) == 1
