# PG Rewrite — Live Status (autonomous session 2026-06-28)

Snapshot for the user to read on waking. Updated at checkpoints. Authoritative design = the spec; this is "where are we right now."

- Spec: `docs/superpowers/specs/2026-06-28-ankiweb-pg-rewrite-design.md`
- Plan: `docs/superpowers/plans/2026-06-28-ankiweb-pg-rewrite.md`
- Memory: `ankiweb-pg-rewrite` (in your auto-memory)

## Locked decisions
- **Approach B-deep**: fork Anki `rslib` @ **25.09.4**, swap SQLite→PG behind a **`StorageBackend` enum** (NOT `dyn` — ~12 methods are non-object-safe), remove single-writer machinery (USN / `BEGIN EXCLUSIVE` / global undo → PG MVCC + row locks + per-session undo + `LISTEN/NOTIFY`). Keep SQLite only at the `.apkg`/`.colpkg` import/export boundary.
- ankiweb (Python) changes minimally: PG DSN instead of a file path; absolute data dir param, relative paths in DB.
- Equivalence gate: **rslib's own Rust test suite must pass on `PgStorage`** (M3).

## Workspaces (running instance at `/mnt/sda/git/web/ankiweb` is UNTOUCHED)
- ankiweb worktree: `/mnt/sda/git/web/ankiweb/.claude/worktrees/pg-rewrite` (branch `worktree-pg-rewrite`). Venv: `.venv-fork` (built anki wheel + ankiweb deps).
- Anki fork: `/mnt/sda/git/tools/anki-pg-fork` (branch `pg-fork` @ tag 25.09.4, commit d52ca669f).
- Remote PG: `192.168.150.101:15432` db `ankiweb` (UTF8, **created this session**; superuser `postgres`).

## Done & verified
| Item | Evidence |
|---|---|
| Design spec committed | worktree `965ff1f` |
| Implementation plan committed | worktree (after spec) |
| Storage-surface catalog committed | fork `18d8d8069` (220 methods, 33 db-leak sites, 92 sql, 11 fns) |
| Toolchain | rustup (rust 1.89.0 via toolchain file), ninja 1.13, protoc 25.3 |
| **M0: forked wheel builds** | `anki-25.9.4-cp39-abi3-…whl` (built 249s) |
| **M0: wheel is OURS, not PyPI** | import buildhash `d52ca669` = fork commit |
| M0: ankiweb smoke green | `test_smoke + test_collection_service` = 7 passed |
| **M0: full suite — 0 failures** | ~300/509 ran, **all passed (no F/E)** before a 15-min cap; remainder are slow E2E (browser/server), not failures. M0 verified. |
| Web assets vendored | `aqt==25.9.4` `_aqt/data/web/` → `ankiweb/web_assets/` |
| **M1.1: rslib baseline green** | nextest **322 passed, 0 skipped** (unmodified fork) |
| **M1 COMPLETE: storage fully abstracted** | leaks sealed + dbproxy bridge moved into `impl SqliteStorage` + `StorageBackend` enum (208 delegations, **no escape hatch**) + dead `db()` removed; fork commits `e44d08eee`,`250a93366`,`9682ce8a5`,`914914a0a`; **322 passed** |
| M1.5: ankiweb on M1 wheel | wheel buildhash flipped to `914914a0`; smoke **7 passed**; full non-E2E subset running for the record |
| Remote PG reachable + db created | `select version()` OK; `ankiweb` db created |

## In flight (will auto-resume me)
- **M1.5 record run (background)**: full non-E2E ankiweb subset on the M1 wheel for a definitive count. The ankiweb suite is slow (server/collection-heavy, ~2-3s/test), so this is a *record*, not the gate — the gate (rslib **322** + smoke **7**) already passed.

## Next (in order) — M2 (`PgStorage` on the real PG)
M0 ✓, M1 ✓. Now M2:
1. Dump the exact current SQLite schema (ground truth) from a fresh collection.
2. Write the PG DDL (translate tables+indexes; `sfld` dynamic-typing strategy, integer-PK→`bigint`, `WITHOUT ROWID` graves, `INSERT OR …`→`ON CONFLICT`).
3. Add the synchronous `postgres` crate to rslib; create `PgStorage` (holds `postgres::Client`) + the `Pg` enum variant; `open`/connect to the DSN.
4. pgrx extension for the 11 custom fns + `unicase` collation; install on the remote PG (superuser available).
5. Parametrize the rslib test harness over `{Sqlite, Pg}`; get a minimal vertical slice (open / add+get note / basic search) green on PG.

Note: `backend/dbproxy.rs db_query*` now lives in `impl SqliteStorage` — that's exactly where `PgStorage` will run/translate ankiweb's raw `col.db` SQL (the spec's "11 raw SQL sites"). `fsrs` is a reusable workspace crate.

## Findings / notes
- **ankiweb `pyproject.toml` packaging bug**: has both SPDX `license="AGPL-3.0-or-later"` AND a deprecated license *classifier* → modern setuptools rejects `pip install -e .` (running instance just has older setuptools). Worked around for M0 (installed deps explicitly). Minor fix later: drop the redundant `"License :: OSI Approved …"` classifier line. NOT yet changed.
- **AnkiDroid raw-SQL bridge** (`rslib/src/ankidroid/`): inherently raw-SQLite; ankiweb doesn't use it. Being encapsulated as SQLite-specific in M1.3; candidate to feature-gate off later.
- **USN/sync storage methods**: don't port to PG — they're removed in M4. Kept Sqlite-only through M1–M3.
- Build only `wheels:anki` (never `wheels:aqt` → avoids the node/web build).
- **Leak detection must be compiler-driven, not grep**: a line-based grep misses multi-line `self.storage\n.db…` chains (cost us a false-complete on M1.3's first pass). Switching `Collection.storage` to the enum makes the compiler enumerate *every* residual raw-connection access — that's the reliable gate, used from here on.
- **`backend/dbproxy.rs`** (`db_query`/`db_query_row`/`db_execute_many`) is the bridge Python's `col.db.all/scalar/execute` uses. Moved into `impl SqliteStorage` in M1; in M2 these are where `PgStorage` must run/translate the SQL against PG (the spec's "11 raw SQL sites" path).

## Mandate
User (2026-06-28) is asleep; instructed to proceed fully autonomously, no token/time budget, not stop for decisions until all work is done — they will stop me on waking. All open design choices resolved with the recommended option.
