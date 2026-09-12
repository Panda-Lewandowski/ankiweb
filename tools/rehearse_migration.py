"""Rehearse a desktop package in new private directories, never a live collection.

Default: archive preview only. --run explicitly authorizes import into NEW scratch
collections. Each operation uses a fresh process and one CollectionService owner.
Run from the fork root: .venv/bin/python tools/rehearse_migration.py PACKAGE ...
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ankiweb.anki_core import AnkiAdapter
from ankiweb.anki_core.migration import compare, file_digest
from ankiweb.collection_service import CollectionService
from ankiweb.config import Settings


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def inspect_package(package):
    with zipfile.ZipFile(package) as archive:
        if sum(item.file_size for item in archive.infolist()) > 2_000_000_000:
            raise ValueError("package exceeds rehearsal size limit")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("duplicate archive entries")
        if package.suffix == ".colpkg":
            # Modern COLPKG uses compressed/protobuf members: let official Anki read it.
            if not any(name in names for name in ("collection.anki2", "collection.anki21", "collection.anki21b")):
                raise ValueError("missing collection in COLPKG")
            return {"sha256": file_digest(package), "format": "colpkg",
                    "uncompressed_bytes": sum(item.file_size for item in archive.infolist())}
        # Desktop AnkiConnect exports the official legacy APKG format. Do not mistake
        # the compatibility placeholder collection.anki2 for the actual collection.
        source = next((name for name in ("collection.anki21", "collection.anki2")
                       if name in names), None)
        if "collection.anki21b" in names or source is None:
            raise ValueError("source preview requires an AnkiConnect legacy APKG export")
        media = json.loads(archive.read("media"))
        for member, filename in media.items():
            if member not in names or not filename or Path(filename).name != filename:
                raise ValueError("invalid media manifest")
            if filename in {".", ".."} or "\\" in filename:
                raise ValueError("unsafe media path")
        return {"sha256": file_digest(package), "format": "apkg", "collection_member": source,
                "media_files": len(media), "uncompressed_bytes": sum(
                    item.file_size for item in archive.infolist())}


def unpack_export(package, out, preview):
    """Unpack only the exported package; never read a desktop profile or use SQLite."""
    out.mkdir(mode=0o700)
    media_dir = out / "collection.media"
    media_dir.mkdir()
    with zipfile.ZipFile(package) as archive:
        with (out / "collection.anki2").open("xb") as handle:
            handle.write(archive.read(preview["collection_member"]))
        for member, filename in json.loads(archive.read("media")).items():
            with (media_dir / filename).open("xb") as handle:
                handle.write(archive.read(member))


async def worker(args):
    service = CollectionService(Settings(collection_path=Path(args.collection)))
    await service.open()
    try:
        adapter = AnkiAdapter(service)
        if args.operation == "import":
            await adapter.migration_import(args.package, args.sha256, args.backup, apply=True)
        elif args.operation == "export":
            await adapter.migration_export(args.package)
        elif args.operation == "api-smoke":
            # Exercise the stable product API on the same serialized owner. No ratings.
            import httpx
            from ankiweb.app import create_app
            app = create_app(service.settings, service=service)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                            base_url="http://testserver") as client:
                    for language in ("english", "spanish"):
                        response = await client.get("/api/today", params={"language": language})
                        response.raise_for_status()
                        response = await client.get("/api/review/next", params={"language": language})
                        response.raise_for_status()
                        if response.status_code != 204:
                            token = response.json()["token"]
                            (await client.post(f"/api/review/{token}/check",
                                               json={"typed_answer": ""})).raise_for_status()
        save(args.result, await adapter.migration_snapshot())
    finally:
        await service.close()


def run_worker(operation, collection, result, **kwargs):
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", operation,
               "--collection", str(collection), "--result", str(result)]
    for key, value in kwargs.items():
        command += ["--" + key, str(value)]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    return json.loads(Path(result).read_text())


def compare_desktop(manifest, source):
    desktop = manifest["desktop"]
    checks = {
        "export_did_not_change_desktop": manifest["desktop_unchanged"],
        "note_ids": desktop["notes"].keys() == source["notes"].keys(),
        "card_ids": desktop["cards"].keys() == source["cards"].keys(),
        "fields_tags_types": all(
            all(source["notes"].get(nid, {}).get(key) == value for key, value in note.items())
            for nid, note in desktop["notes"].items()),
        "templates_css_fields": all(
            all(source["models"].get(name, {}).get(key) == value for key, value in model.items())
            for name, model in desktop["models"].items()),
        "review_counts": all(len(rows) == source["cards"].get(cid, {}).get("review_count")
                             for cid, rows in desktop["reviews"].items()),
    }
    mapping = {"cardId": "id", "note": "nid", "deckName": "deck", "interval": "ivl"}
    checks["card_state"] = all(
        all(source["cards"].get(cid, {}).get(mapping.get(key, key)) == value
            for key, value in card.items()) for cid, card in desktop["cards"].items())
    # The legacy AnkiConnect exporter drops customizations of preset id=1.
    # Older manifests without this audit must not silently pass the transfer gate.
    configs = desktop.get("deck_configs", {})
    checks["deck_settings"] = bool(configs) and all(
        {key: value for key, value in config.items() if key not in {"id", "mod", "usn", "name"}}
        == {key: value for key, value in source["decks"].get(deck, {}).items() if key != "name"}
        for deck, config in configs.items())
    # AnkiConnect omits GUID and FSRS memory, so those comparisons start at the
    # package snapshot. Never claim desktop-to-package verification of those fields.
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--desktop-manifest", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    preview = inspect_package(args.package)
    print(json.dumps(preview, indent=2))
    if args.dry_run or not args.run:
        return
    if not args.out or not args.desktop_manifest:
        parser.error("--run requires a NEW --out directory and --desktop-manifest")
    manifest = json.loads(args.desktop_manifest.read_text())
    if not manifest["desktop_unchanged"] or manifest["package_sha256"] not in (None, preview["sha256"]):
        raise ValueError("desktop manifest does not match an unchanged export")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    save(out / "preview.json", preview)
    if preview["format"] == "colpkg":
        source = run_worker("import", out / "source/collection.anki2", out / "source.json",
                            package=args.package.resolve(), sha256=preview["sha256"],
                            backup=out / "pre-source.colpkg")
    else:
        unpack_export(args.package, out / "source", preview)
        source = run_worker("snapshot", out / "source/collection.anki2", out / "source.json")
    desktop_checks = compare_desktop(manifest, source)
    # Exact import plan is recorded before applying to an empty scratch target.
    save(out / "import-plan.json", {"source": source["summary"], "desktop_checks": desktop_checks,
                                   "target": str(out / "target/collection.anki2"),
                                   "target_policy": "new empty scratch collection only"})
    if not all(desktop_checks.values()):
        save(out / "report.json", {"passed": False, "blocked_at": "desktop_export_validation",
                                    "desktop_checks": desktop_checks, "summary": source["summary"],
                                    "source_sha256": preview["sha256"],
                                    "next_step": "Export a full COLPKG from desktop Anki; do not repair scheduling manually"})
        raise ValueError("desktop/export mismatch; see import-plan.json")
    imported = run_worker("import", out / "target/collection.anki2", out / "imported.json",
                          package=args.package.resolve(), sha256=preview["sha256"],
                          backup=out / "pre-import.colpkg")
    reopened = run_worker("snapshot", out / "target/collection.anki2", out / "reopened.json")
    smoke = run_worker("api-smoke", out / "target/collection.anki2", out / "api-smoke.json")
    exported = run_worker("export", out / "target/collection.anki2", out / "exported.json",
                          package=out / "roundtrip.apkg")
    roundtrip = run_worker("import", out / "roundtrip/collection.anki2", out / "roundtrip.json",
                           package=out / "roundtrip.apkg", sha256=file_digest(out / "roundtrip.apkg"),
                           backup=out / "pre-roundtrip.colpkg")
    collection_export = run_worker("export", out / "target/collection.anki2", out / "collection-export.json",
                                   package=out / "transfer.colpkg")
    collection_import = run_worker("import", out / "collection-roundtrip/collection.anki2",
                                   out / "collection-roundtrip.json", package=out / "transfer.colpkg",
                                   sha256=file_digest(out / "transfer.colpkg"), backup=out / "pre-collection.colpkg")
    checks = {"desktop": desktop_checks, "import": compare(source, imported),
              "restart": compare(imported, reopened), "api_read_only": compare(reopened, smoke),
              "export_read_only": compare(smoke, exported),
              "collection_export_read_only": compare(exported, collection_export),
              "collection_roundtrip": compare(collection_export, collection_import)}
    passed = all(desktop_checks.values()) and all(checks[key]["preserved"] for key in checks if key != "desktop")
    passed = passed and source["summary"]["rendered"] == source["summary"]["cards"]
    passed = passed and source["summary"]["duplicate_guids"] == 0
    report = {"passed": passed, "summary": source["summary"], "checks": checks,
              "transfer_ready": passed and preview["format"] == "colpkg",
              "diagnostics": {"apkg_roundtrip": compare(exported, roundtrip)},
              "transfer_package": "transfer.colpkg", "transfer_sha256": file_digest(out / "transfer.colpkg"),
              "source_sha256": preview["sha256"], "roundtrip_sha256": file_digest(out / "roundtrip.apkg"),
              "limitations": ["Desktop AnkiConnect does not expose GUID/FSRS memory directly",
                              "Empty scratch destinations only; no existing-server merge tested",
                              "Renderer preparation tested for all cards; browser UI is a separate check"]}
    save(out / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    if "--worker" in sys.argv:
        parser = argparse.ArgumentParser()
        parser.add_argument("--worker", dest="operation")
        for name in ("collection", "result", "package", "sha256", "backup"):
            parser.add_argument("--" + name)
        asyncio.run(worker(parser.parse_args()))
    else:
        main()
