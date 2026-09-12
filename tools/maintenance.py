"""Offline backup / side-by-side restore. Stop the app first; owner lock enforces it.

Default is a read-only preview. Restore NEVER replaces existing files/directories.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ankiweb.anki_core import AnkiAdapter
from ankiweb.anki_core.migration import compare
from ankiweb.collection_service import CollectionService
from ankiweb.config import Settings
from ankiweb.operations import backup_bundle, verify_bundle, write_json


async def run(args):
    current = args.collection.resolve()
    if not current.is_file():
        raise ValueError("current collection must already exist")
    plan = {"operation": args.operation, "current": str(current), "mode": "preview"}
    if args.operation == "restore":
        manifest = verify_bundle(args.bundle)
        target = args.target.resolve()
        if target.parent.exists():
            raise ValueError("restore requires a NEW target directory; nothing is overwritten")
        plan.update(target=str(target), sha256=manifest["sha256"],
                    policy="pre-restore backup, then fresh destination; current remains untouched")
        if args.apply and (args.sha256 != manifest["sha256"] or args.confirm_target != str(target)):
            raise ValueError("apply requires preview SHA256 and exact absolute --confirm-target")
    print(json.dumps(plan, indent=2))
    if args.dry_run or not args.apply:
        return
    service = CollectionService(Settings(collection_path=current))
    await service.open()  # Fails before touching Anki if another owner is running.
    try:
        backup = await backup_bundle(AnkiAdapter(service), args.backups,
                                     kind="pre-restore" if args.operation == "restore" else "manual")
    finally:
        await service.close()
    if args.operation == "backup":
        print(json.dumps(backup, indent=2))
        return
    service = CollectionService(Settings(collection_path=target))
    await service.open()
    try:
        adapter = AnkiAdapter(service)
        await adapter.migration_import(args.bundle / "collection.colpkg", manifest["sha256"],
                                       target.parent / "pre-empty.colpkg", apply=True)
        after = await adapter.migration_snapshot()
        expected = json.loads((args.bundle / "snapshot.json").read_text())
        result = compare(expected, after)
        if not result["equal"]:
            write_json(target.parent / "restore-failed.json", result)
            raise ValueError("restored state differs; do not activate this destination")
        receipts = target.parent / "lesson-receipts"
        receipts.mkdir(exist_ok=True)
        for name in manifest["receipts"]:
            shutil.copyfile(args.bundle / "receipts" / name, receipts / name)
        report = {"passed": True, "comparison": result, "target": str(target),
                  "pre_restore_backup": backup["bundle"], "sha256": manifest["sha256"],
                  "activated": False}
        write_json(target.parent / "restore-report.json", report)
        print(json.dumps(report, indent=2))
    finally:
        await service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["backup", "restore"])
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--backups", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--confirm-target")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.operation == "restore" and (not args.bundle or not args.target):
        parser.error("restore requires --bundle and --target")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
