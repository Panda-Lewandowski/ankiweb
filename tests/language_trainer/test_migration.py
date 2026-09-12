"""Non-vacuous migration invariants using real Anki scheduling and private fixtures."""
from pathlib import Path

import pytest

from ankiweb.anki_core import AnkiAdapter
from ankiweb.anki_core.migration import compare, file_digest
from ankiweb.collection_service import CollectionService
from ankiweb.config import Settings
from importlib.util import module_from_spec, spec_from_file_location

_spec = spec_from_file_location("rehearse_migration", Path(__file__).parents[2] / "tools/rehearse_migration.py")
_runner = module_from_spec(_spec)
_spec.loader.exec_module(_runner)
inspect_package, run_worker = _runner.inspect_package, _runner.run_worker


def seed(col):
    model = col.models.new("Vocabulary Production")
    for name in ("Prompt", "Answer", "Example", "Language"):
        col.models.add_field(model, col.models.new_field(name))
    template = col.models.new_template("Production")
    template["qfmt"] = "{{Prompt}} {{type:Answer}}"
    template["afmt"] = "{{FrontSide}}<hr>{{Answer}} {{Example}}"
    col.models.add_template(model, template)
    col.models.add_dict(model)
    model = col.models.by_name("Vocabulary Production")
    col.set_config("fsrs", True)  # Synthetic fixture ONLY; never touch user settings.
    media = col.media.write_data("fixture.txt", b"migration media byte preservation")
    for language in ("spanish", "english"):
        did = col.decks.id(f"Languages::{language.title()}")
        for number in range(3):
            note = col.new_note(model)
            note["Prompt"] = f"fixture-{language}-{number}"
            note["Answer"] = f"answer-{number}"
            note["Language"] = language
            note["Example"] = f"[sound:{media}]"
            note.tags = [f"language::{language}", "source::migration-test"]
            assert not col.find_notes(f'"Prompt:{note["Prompt"]}"')
            col.add_note(note, did)


@pytest.mark.parametrize("extension,legacy", [("apkg", True), ("apkg", False), ("colpkg", False)])
async def test_package_roundtrip_real_reviews_fsrs_media_restart(tmp_path: Path, extension, legacy):
    source_path = tmp_path / "source/collection.anki2"
    service = CollectionService(Settings(collection_path=source_path))
    await service.open()
    try:
        await service.run(seed)
        adapter = AnkiAdapter(service)
        # Distinct languages ensure the learning card does not preempt the second rating.
        for language, rating in (("spanish", "again"), ("english", "easy")):
            card = await adapter.next_review(language, "fixture")
            await adapter.check(card["token"], "fixture", "answer-0",
                                case_sensitive=False, punctuation_sensitive=False)
            await adapter.answer(card["token"], "fixture", rating, continue_session=False)
        before = await adapter.migration_snapshot()
        assert before["summary"]["reviews"] == 2
        assert before["summary"]["fsrs_cards"] == 2
        assert before["summary"]["media"] == 1
        assert {card["type"] for card in before["cards"].values()} == {0, 1, 2}
        package = tmp_path / f"source.{extension}"
        await adapter.migration_export(package, legacy=legacy)
        assert compare(before, await adapter.migration_snapshot())["equal"]
    finally:
        await service.close()

    # Every step below is a separate OS process; no concurrent writable owners.
    reopened = run_worker("snapshot", source_path, tmp_path / "source-reopened.json")
    assert compare(before, reopened)["equal"]
    target = tmp_path / "target/collection.anki2"
    imported = run_worker("import", target, tmp_path / "imported.json", package=package,
                          sha256=file_digest(package), backup=tmp_path / "pre-import.apkg")
    if extension == "apkg":
        # APKG preserves card memory, but NOT the collection-wide FSRS switch.
        # This is a regression guard for a known unsafe whole-collection transfer.
        assert before["fsrs_enabled"] is True and imported["fsrs_enabled"] is False
        assert compare(before, imported)["differences"] == [
            {"section": "collection", "id": "fsrs_enabled", "fields": ["value"]}]
    else:
        assert compare(before, imported)["equal"], compare(before, imported)
    restarted = run_worker("snapshot", target, tmp_path / "restarted.json")
    assert compare(imported, restarted)["equal"]
    second_package = tmp_path / f"roundtrip.{extension}"
    exported = run_worker("export", target, tmp_path / "exported.json", package=second_package)
    assert compare(imported, exported)["equal"]
    roundtrip = run_worker("import", tmp_path / "roundtrip/collection.anki2",
                           tmp_path / "roundtrip.json", package=second_package,
                           sha256=file_digest(second_package), backup=tmp_path / "pre-roundtrip.apkg")
    assert compare(exported, roundtrip)["preserved"], compare(exported, roundtrip)
    if extension == "colpkg":
        import json
        import subprocess
        import sys
        manifest = tmp_path / "desktop.json"
        manifest.write_text(json.dumps({"desktop_unchanged": True, "package_sha256": None,
            "desktop": {
                "cards": {cid: {"cardId": state["id"], "due": state["due"], "reps": state["reps"]}
                          for cid, state in before["cards"].items()},
                "notes": {nid: {key: value for key, value in note.items() if key != "guid"}
                          for nid, note in before["notes"].items()},
                "models": before["models"], "deck_configs": before["decks"],
                "reviews": {cid: [{}] * state["review_count"] for cid, state in before["cards"].items()},
            }}))
        subprocess.run([sys.executable, _runner.__file__, str(package), "--desktop-manifest",
                        str(manifest), "--out", str(tmp_path / "full-cli"), "--run"],
                       check=True, stdout=subprocess.DEVNULL, timeout=60)
        report = json.loads((tmp_path / "full-cli/report.json").read_text())
        assert report["passed"] and report["transfer_ready"]
        assert not report["diagnostics"]["apkg_roundtrip"]["preserved"]


@pytest.mark.parametrize("extension", ["apkg", "colpkg"])
async def test_import_preview_hash_empty_target_and_no_overwrites(tmp_path, extension):
    source = CollectionService(Settings(collection_path=tmp_path / "source/collection.anki2"))
    await source.open()
    package = tmp_path / f"source.{extension}"
    try:
        await source.run(seed)
        await AnkiAdapter(source).migration_export(package)
    finally:
        await source.close()
    target = CollectionService(Settings(collection_path=tmp_path / "target/collection.anki2"))
    await target.open()
    try:
        adapter = AnkiAdapter(target)
        before = await adapter.migration_snapshot()
        backup = tmp_path / "backup.apkg"
        checksum = file_digest(package)
        preview = await adapter.migration_import(package, checksum, backup)
        assert preview["mode"] == "preview" and not backup.exists()
        assert compare(before, await adapter.migration_snapshot())["equal"]
        with pytest.raises(ValueError, match="changed"):
            await adapter.migration_import(package, "incorrect", backup, apply=True)
        assert not backup.exists()
        backup.write_bytes(b"existing backup must not be overwritten")
        with pytest.raises(FileExistsError):
            await adapter.migration_import(package, checksum, backup, apply=True)
        assert compare(before, await adapter.migration_snapshot())["equal"]
        await adapter.migration_import(package, checksum, tmp_path / "new-backup.apkg", apply=True)
        after = await adapter.migration_snapshot()
        with pytest.raises(ValueError, match="empty target"):
            await adapter.migration_import(package, checksum, tmp_path / "never.apkg", apply=True)
        assert not (tmp_path / "never.apkg").exists()
        assert compare(after, await adapter.migration_snapshot())["equal"]
        with pytest.raises(FileExistsError):
            await adapter.migration_export(package)
        assert file_digest(package) == checksum
    finally:
        await target.close()


def test_compare_never_ignores_scheduler_or_fsrs_changes():
    from copy import deepcopy
    baseline = {key: {} for key in ("notes", "cards", "models", "media", "rendering")}
    baseline.update(decks={"Languages::Spanish": {"name": "По умолчанию", "maxIvl": 36500}},
                    fsrs_enabled=True)
    after = deepcopy(baseline)
    after["decks"]["Languages::Spanish"]["name"] = "Default"
    assert compare(baseline, after)["preserved"]
    assert not compare(baseline, after)["equal"]
    after["decks"]["Languages::Spanish"]["maxIvl"] = 100
    assert not compare(baseline, after)["preserved"]
    after = deepcopy(baseline)
    after["fsrs_enabled"] = False
    assert not compare(baseline, after)["preserved"]


def test_archive_preview_rejects_unsafe_media(tmp_path):
    import zipfile
    package = tmp_path / "bad.apkg"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("collection.anki21", b"unused")
        archive.writestr("media", '{"0":"../escape"}')
        archive.writestr("0", b"unused")
    with pytest.raises(ValueError, match="invalid media"):
        inspect_package(package)


async def test_legacy_desktop_export_loses_modified_default_preset(tmp_path):
    from anki.exporting import AnkiPackageExporter
    service = CollectionService(Settings(collection_path=tmp_path / "source/collection.anki2"))
    await service.open()
    package = tmp_path / "desktop.apkg"
    try:
        await service.run(seed)

        def fixture_export(col):
            config = col.decks.config_dict_for_deck_id(col.decks.id_for_name("Languages::Spanish"))
            config["new"]["perDay"] = 10
            config["rev"]["perDay"] = 20
            col.decks.update_config(config)  # Fixture ONLY.
            exporter = AnkiPackageExporter(col)
            exporter.did = col.decks.id_for_name("Languages")
            exporter.includeSched = True
            exporter.exportInto(str(package))

        await service.run(fixture_export)
        before = await AnkiAdapter(service).migration_snapshot()
    finally:
        await service.close()
    imported = run_worker("import", tmp_path / "target/collection.anki2", tmp_path / "imported.json",
                          package=package, sha256=file_digest(package), backup=tmp_path / "pre.colpkg")
    assert before["decks"]["Languages::Spanish"]["new"]["perDay"] == 10
    assert imported["decks"]["Languages::Spanish"]["new"]["perDay"] == 20
    assert not compare(before, imported)["preserved"]


def test_desktop_gate_requires_matching_settings():
    from copy import deepcopy
    source = {key: {} for key in ("notes", "cards", "models")}
    source["decks"] = {"Languages::Spanish": {"name": "Default", "new": {"perDay": 20}}}
    desktop = {**deepcopy(source), "reviews": {}}
    manifest = {"desktop_unchanged": True, "desktop": desktop}
    assert not _runner.compare_desktop(manifest, source)["deck_settings"]
    desktop["deck_configs"] = deepcopy(source["decks"])
    assert all(_runner.compare_desktop(manifest, source).values())
    desktop["deck_configs"]["Languages::Spanish"]["new"]["perDay"] = 10
    assert not _runner.compare_desktop(manifest, source)["deck_settings"]
