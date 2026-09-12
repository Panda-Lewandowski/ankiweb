"""Private backup bundles. No direct access to Anki files or database internals."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import shutil
import time
import uuid

from ankiweb.anki_core.migration import file_digest

LOGGER = logging.getLogger("language_trainer.operations")


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def verify_bundle(bundle):
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("version") != 1 or not manifest.get("complete"):
        raise ValueError("incomplete backup")
    if file_digest(bundle / "collection.colpkg") != manifest["sha256"]:
        raise ValueError("backup checksum mismatch")
    if file_digest(bundle / "snapshot.json") != manifest["snapshot_sha256"]:
        raise ValueError("snapshot checksum mismatch")
    for name, checksum in manifest["receipts"].items():
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("invalid receipt name")
        if file_digest(bundle / "receipts" / name) != checksum:
            raise ValueError("receipt checksum mismatch")
    return manifest


async def backup_bundle(adapter, root: Path, *, kind="manual"):
    """Adapter holds its mutation lock throughout export, snapshot and receipts copy."""
    return await adapter.operational_backup(Path(root), kind=kind)


class DailyBackups:
    def __init__(self, adapter, root, *, interval=86400, retry=300):
        self.adapter, self.root = adapter, Path(root)
        self.interval, self.retry = interval, retry
        self.last_success = 0.0
        self.failed = False

    async def run(self):
        # On startup, only complete, checksum-verified bundles count as successful.
        for manifest in sorted(self.root.glob("daily-*/manifest.json"), reverse=True):
            try:
                item = verify_bundle(manifest.parent)
                self.last_success = max(self.last_success, item["created_at"])
                break
            except (ValueError, OSError, KeyError):
                continue
        while True:
            delay = max(0, self.last_success + self.interval - time.time())
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await backup_bundle(self.adapter, self.root, kind="daily")
                self.last_success = result["created_at"]
                self.failed = False
                LOGGER.info('{"event":"backup_complete"}')
            except Exception:
                # No exception messages or stack locals: these may contain card text/paths.
                self.failed = True
                LOGGER.error('{"event":"backup_failed"}')
                await asyncio.sleep(self.retry)

    def healthy(self):
        return not self.failed and 0 <= time.time() - self.last_success < self.interval + 3600


async def create_bundle_locked(adapter, root, *, kind):
    if kind not in {"manual", "daily", "pre-restore"}:
        raise ValueError("unsupported backup kind")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bundle = root / f"{kind}-{label}-{uuid.uuid4().hex}"
    bundle.mkdir(mode=0o700)
    # No automatic pruning: retain ALL successful daily backups (>=30); monitor disk.
    from ankiweb.anki_core.migration import export_package, snapshot
    result = await adapter._service.run(lambda col: export_package(col, bundle / "collection.colpkg"))
    audit = await adapter._service.run(snapshot)
    write_json(bundle / "snapshot.json", audit)
    receipts = bundle / "receipts"
    receipts.mkdir()
    original = adapter._service.settings.collection_path.parent / "lesson-receipts"
    hashes = {}
    if original.exists():
        for path in original.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ValueError("unexpected receipt entry")
            shutil.copyfile(path, receipts / path.name)
            hashes[path.name] = file_digest(receipts / path.name)
    manifest = {"version": 1, "complete": True, "kind": kind, "created_at": time.time(),
                "sha256": result["sha256"], "snapshot_sha256": file_digest(bundle / "snapshot.json"),
                "receipts": hashes, "anki_version": "25.9.4"}
    # Manifest written LAST: interrupted exports never become valid backups.
    write_json(bundle / "manifest.json", manifest)
    verify_bundle(bundle)
    return {**manifest, "bundle": str(bundle)}
