# Phase 6: local migration rehearsal — 2026-09-12

Status: **local Phase 6 rehearsal COMPLETE using the user-supplied full COLPKG**.
The verified package is ready as an input to a separately approved migration into an
empty server collection; production deployment and existing-collection merge are not tested.
The user authorized isolated local rehearsal, not replacement/merge of an existing
server or changes to desktop scheduling. All live access was through AnkiConnect.

## Final successful run

Source: outer-workspace `коллекция-2026-09-12@11-26-04.colpkg`.
Source SHA-256: `e3c16e8f3efe410f9f6b19991e795a5cf0850f8361a87a9904309f2e02fb5b85`.

`data/migration-20260912-full-rehearsal/report.json` has `passed: true` and
`transfer_ready: true`. Every required import/restart/full-collection round-trip check
has `equal: true`, with no differences. Both language decks retain **10 new / 20 reviews
per day**, and **FSRS remains enabled**. No manual configuration repairs were made.

Verified candidate: `data/migration-20260912-full-rehearsal/transfer.colpkg`.
SHA-256: `d0af4735266c779a2554b45da86e5ee2c90358f57936fd33d74c78667976aa98`.

The fresh desktop inventories before/after the run match, including deck settings.
The full-export Chromium smoke also passed at both viewport sizes, with the scratch
collection unchanged and zero ratings. All **9 migration tests** passed again; the
preceding full code verification passed **586 backend + 8 frontend tests**. No product
code changed for this successful run, only artifacts and checkpoint documentation.

The diagnostic APKG round-trip still loses preset settings and the FSRS switch and is
NOT an approved transfer route. Use only the verified full COLPKG above. If the desktop
is studied/edited after this snapshot, export and rehearse again before the actual transfer.

## Results

- Desktop: 86 notes, 87 new cards; English 40, Spanish 47. Three note types:
  Grammar Cloze (34 notes), Vocabulary Production (30), Phrase Retrieval (22).
- Desktop card/note IDs, field hashes, tags, templates/CSS and exposed scheduling
  fields matched the exported package. Desktop remained unchanged across capture
  and the final read-only check. No deletions, suspensions, ratings or scheduling edits.
- Package → empty scratch collection → new process → official export → fresh
  collection preserved the audited note/card state, GUIDs and rendering hashes.
  All 87 cards passed product renderer preparation.
- Real Chromium loaded both language decks, questions and checked answers at
  1440×1000 and 390×844. No JS/API errors, horizontal overflow or submitted ratings;
  before/after collection snapshots matched. Four sampled UI cards were Grammar Cloze;
  this is not an exhaustive browser test of every card.
- Real data has **zero reviews and zero media**. Separate synthetic fixtures use
  real Anki Again/Easy ratings, non-empty FSRS memory/history and referenced media.
  Full COLPKG preserves them and the FSRS switch across independent processes.

## Earlier APKG blocker (resolved by full desktop COLPKG)

Both language decks share the modified built-in preset. AnkiConnect's legacy
`exportPackage(includeSched=True)` does not preserve its customizations:

| Setting (both decks) | Live desktop | Exported APKG |
| --- | ---: | ---: |
| New cards/day | 10 | 20 |
| Reviews/day | 20 | 200 |

Separately, the built-in preset label becomes “Default” instead of “По умолчанию” on
English import; this is reported as a non-scheduling label difference. All other
setting differences are blocking, never normalized away or silently repaired.

APKG also omits the collection-wide FSRS enable switch in the non-empty fixture.
Card memory/history can remain intact while the destination scheduler mode differs.
The current desktop AnkiConnect API does not expose that switch or a full COLPKG
export action. A COLPKG generated *from the already lossy APKG* cannot recover it.

The full-collection path uses official Anki export/import, serialized by
`CollectionService` behind `AnkiAdapter`, and rejects a non-empty target. Full
collection import replaces a collection; it is **not a merge operation**. See the
[official Anki export manual](https://docs.ankiweb.net/exporting.html) and the
[AnkiConnect export implementation](https://github.com/FooSoft/anki-connect/blob/master/plugin/__init__.py).

## Private evidence (ignored by Git)

- `data/migration-20260912-full-desktop/desktop.json` and
  `data/migration-20260912-full-desktop-final/desktop.json`: matching live snapshots.
- `data/migration-20260912-full-rehearsal/`: authoritative successful full-export
  audit, pre-import backups, lifecycle snapshots, and the verified `transfer.colpkg`.
- `data/migration-20260912-full-ui/report.json`: successful Chromium smoke for the
  full export, with unchanged scratch collection snapshots.

- `data/migration-20260912-desktop-audit/`: new desktop inventory, including deck
  settings, plus unchanged official APKG capture.
- `data/migration-20260912-final-audit/report.json`: historical failed APKG gate;
  `import-plan.json` and `source.json` show the mismatch. No target import performed.
- `data/migration-20260912-rehearsal-02/`: earlier successful **package-only** round
  trip, superseded by desktop-settings validation. Its `transfer.colpkg` is NOT a
  faithful desktop transfer candidate; do not deploy it.
- `data/migration-20260912-ui-02/report.json`: successful read-only Chromium smoke.

Packages contain private study content. Do not commit/upload these artifacts.
The desktop capture script lives at repository-root `scripts/capture_migration.py`,
outside the nested fork's Git working tree.

## Repeat safely for a fresh export

1. In desktop Anki, use File → Export → Anki collection package (.colpkg), including
   media. Save into the outer workspace. Do not copy/open the live profile database.
2. Keep the desktop unchanged during export and audit. Capture a new inventory:

   ```sh
   python3 scripts/capture_migration.py --snapshot --output upstream/ankiweb/data/desktop-full-audit-NEW
   ```

3. From `upstream/ankiweb`, preview the user-exported file, then authorize only NEW
   scratch directories (replace paths/NEW with actual paths/unused directory names):

   ```sh
   .venv/bin/python tools/rehearse_migration.py /absolute/path/export.colpkg --dry-run
   .venv/bin/python tools/rehearse_migration.py /absolute/path/export.colpkg --desktop-manifest data/desktop-full-audit-NEW/desktop.json --out data/full-rehearsal-NEW --run
   npm --prefix web run build
   .venv/bin/python tools/smoke_migration_ui.py data/full-rehearsal-NEW --out data/full-ui-NEW
   ```

4. Require both `passed` and `transfer_ready`, a green UI smoke, and a final
   unchanged desktop check. Full COLPKG includes all decks; unexpected non-language
   cards must be reviewed, never automatically deleted. Only after this checkpoint
   request approval for the actual server target and its backup/restore plan.

Tools refuse existing output directories/files, changed package hashes, and
non-empty destination collections. They create pre-import official backups, use
one owner at a time and launch each lifecycle step in a fresh process. Dry run
inspects the archive only; detailed source inspection occurs in a disposable copy.

Regression commands:

```sh
.venv/bin/pytest -q
npm --prefix web test
```
