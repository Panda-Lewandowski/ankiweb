# ankiweb → PostgreSQL Rewrite — Design Spec

- **Date:** 2026-06-28
- **Branch / worktree:** `worktree-pg-rewrite` at `/mnt/sda/git/web/ankiweb/.claude/worktrees/pg-rewrite`
- **Anki fork:** `pg-fork` branch of the anki repo, checked out at tag **25.09.4** (commit `d52ca669f`) under `/mnt/sda/git/tools/anki-pg-fork`
- **Status:** Approved direction (user approved §Architecture skeleton + version alignment; delegated all remaining decisions to the recommended option).

---

## 0. One-paragraph summary

Replace ankiweb's storage engine from **SQLite to PostgreSQL** to support **true concurrent writes and multiple worker processes** against a single user's collection, while **perfectly replicating current ankiweb functionality**. We do this by **forking Anki's Rust backend (`rslib`) at 25.09.4** and swapping its storage layer SQLite→PG behind a new `Storage` trait, removing the single-writer machinery (USN-as-sync counter, the global `BEGIN EXCLUSIVE` lock, and the global linear undo), getting concurrency from **PostgreSQL MVCC + row-level locks**, and solving the cross-process metadata-cache problem with **`LISTEN/NOTIFY`**. ankiweb (Python) consumes the rebuilt forked `anki` wheel and changes minimally (a PG DSN instead of a file path, plus a configurable absolute data dir). Feature parity and "identical I/O vs. anki core" are **structural** here, because the core *is* Anki's code.

---

## 1. Goals & non-goals

### Goals
- PostgreSQL as the live operational store (DSN provided at startup).
- **Concurrent writes** to one collection across **multiple processes** (the user's case: a single user operating on multiple decks at once).
- **Perfect functional parity** with current ankiweb: 129 canonical AnkiConnect actions + 5 extra actions, 18 web screens, the vendored SvelteKit SPAs (graphs / deck-options / change-notetype / card-info / import dialogs / image-occlusion), the WebSocket bridge, `.apkg`/`.colpkg`/CSV import-export, media, stats, scheduler v3, FSRS, and Anki search semantics.
- **Provable equivalence** to upstream Anki core behavior via tests (see §12).
- Files saved by **relative path** in the DB; an **absolute base dir passed at startup** (per user instruction).

### Non-goals
- AnkiWeb server sync (already deliberately disabled in ankiweb; we remove its machinery, not re-add it).
- Multi-tenant / multi-user. One logical collection (one user), though the schema stays tenant-namable for the future.
- Changing the frontend, the vendored SvelteKit bundles, or ankiweb's HTTP/WS API surface.
- Upgrading Anki to 26.05. We pin to **25.09.4** to match ankiweb's `anki==25.9.4` and its vendored 25.09.4 frontends.
- Collaborative real-time multi-user editing of one deck (would need CRDT/OT; out of scope).

---

## 2. Approach decision (locked)

**Chosen: "B-deep" — fork rslib, swap storage to PG, remove single-writer, true concurrent multi-process.**

Rationale:
- The ankiweb frontend is **not** a thin caller of a few high-level functions. It speaks Anki's **protobuf backend API** directly: `anki_rpc/` does `POST /_anki/{method}` protobuf passthrough; the vendored SvelteKit SPAs are compiled Anki frontends; `col._backend.*_raw()` is used for FSRS/stats/progress. Replacing the backend therefore means reproducing Anki's **entire protobuf service surface**, not just the scheduler/search/cardgen algorithms.
- Reusing Anki's actual backend code makes parity and differential-equivalence **structural** rather than a multi-year reimplementation with a perpetual drift-tracking burden.
- The single-writer limitation is an **implementation choice** for offline-first desktop + occasional sync, **not** a property of the spaced-repetition domain. Evidence: per-deck daily limits are *computed from queries at queue-build time, not stored mutable counters* (`rslib/src/decks/limits.rs`, `scheduler/queue/builder/gathering.rs`); per-card scheduling state lives in disjoint rows; FSRS is a reusable workspace crate. So concurrency reduces to a normal RDBMS row-locking problem once the offline-sync scaffolding is removed.

### Rejected alternatives
- **A — pure-Python reimplementation on PG.** Would mean rewriting ≈ all of `rslib` (the scheduler ~13.4 kLOC, card rendering ~3.1 kLOC, search, stats, import/export) **plus** the protobuf service surface the frontends depend on. Concurrency-native but enormous, high fidelity risk, and the differential tests become a permanent maintenance tax.
- **B-light — single writer + PG, multi-process reads only.** Lower risk and faster, but does **not** deliver concurrent writes across decks, which is the stated requirement.

---

## 3. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│ vendored SvelteKit frontends (25.09.4) — UNCHANGED               │
│   graphs / deck-options / change-notetype / card-info / import…  │
└───────────────▲────────────────────────────────────────────────┘
                │ HTTP / WS  (POST /_anki/{method} protobuf passthrough)
┌───────────────┴────────────────────────────────────────────────┐
│ ankiweb (FastAPI / Python) — MINIMAL CHANGES                     │
│   • DSN instead of collection file path                          │
│   • absolute data/media dir param; relative paths in DB          │
│   • CollectionService: per-process collection handle(s)          │
│   • run with N uvicorn workers / N containers                    │
└───────────────▲────────────────────────────────────────────────┘
                │ import anki   (PyO3 _rsbridge)
┌───────────────┴────────────────────────────────────────────────┐
│ forked anki wheel  (rslib + pylib)  — MAIN WORK, fork @25.09.4   │
│   • trait Storage  ── PgStorage (sync `postgres` crate)  [live]  │
│                     └ SqliteStorage [import/export file boundary]│
│   • single-writer machinery removed (USN/exclusive/global undo)  │
│   • PG MVCC + row locks; LISTEN/NOTIFY cache invalidation        │
│   • pgrx extension: 11 custom fns + unicase collation (exact)    │
└───────────────▲────────────────────────────────────────────────┘
                │ libpq
        ┌───────┴────────┐
        │  PostgreSQL    │  postgresql://…@192.168.150.101:15432/ankiweb
        └────────────────┘
```

**Version alignment.** Fork from the **25.09.4** tag (not the local 26.05 checkout). The installed `anki` Python package is `25.09.4`; ankiweb's vendored frontends and `pylib` all match. Building from 26.05 would desync the protobuf surface and pylib from the frontends.

**Build.** Use the repo's own toolchain (rustup pinned by `rust-toolchain.toml`, ninja/n2, protoc, hatchling + PyO3). Build **only the `pylib/anki` wheel target** (rslib + `_rsbridge` cdylib + protobuf-generated Python) — *not* the aqt/web frontend (ankiweb vendors its own). ankiweb installs this locally-built wheel in place of PyPI `anki==25.9.4`.

---

## 4. Storage abstraction (`trait Storage`)

Today `SqliteStorage { db: rusqlite::Connection }` is hard-wired; rusqlite types leak into scheduler/search/notes. Counts to bound the work: **92 `.sql` files**, **137 `prepare_cached` sites**, **585 `.storage.` call sites**, **22 raw `storage.db.` accesses**. The core request path is **fully synchronous** (tokio only for networking) — so we can use the **synchronous `postgres` crate**, with **no async ripple** through the call stack.

Plan:
1. Define `trait Storage` capturing the operations currently exposed as `SqliteStorage` methods (card/note/deck/notetype/revlog/tag/graves/config CRUD, search scaffolding, transaction control). Aim for a method-level trait (the existing methods are already the natural seam), not a raw-SQL passthrough.
2. Encapsulate the **22 raw `storage.db.`** accesses into trait methods so no higher layer touches a concrete connection type.
3. Two implementations:
   - **`PgStorage`** (sync `postgres` crate) — the live store. Port the 92 SQL assets to PG dialect (`$1` params, explicit casts, `RETURNING`, `ON CONFLICT`).
   - **`SqliteStorage`** (retained) — used solely at the **import/export file boundary** (`.apkg`/`.colpkg` are SQLite files; see §10).
4. `Collection::open` accepts a **PG DSN** (scheme-detected, e.g. `postgresql://…`) where it currently accepts a file path. Each `Collection` owns a PG connection (or a small per-collection pool).

---

## 5. PostgreSQL schema mapping

| SQLite idiom | PG mapping |
|---|---|
| `integer PRIMARY KEY` aliasing rowid | `BIGINT PRIMARY KEY`; IDs assigned app-side |
| `last_insert_rowid()` | `INSERT … RETURNING id` / app-assigned id |
| `sfld integer` storing text (dynamic typing for numeric sort) | `sfld text` + numeric-sort strategy: companion `sfld_num numeric NULL` (populated when the sort field parses as a number) and `ORDER BY sfld_num NULLS LAST, sfld` — preserves Anki's "numbers sort numerically" behavior |
| `INSERT OR REPLACE` / `INSERT OR IGNORE` | `INSERT … ON CONFLICT … DO UPDATE` / `DO NOTHING` |
| `graves … WITHOUT ROWID` (composite PK) | plain table, composite `PRIMARY KEY (oid, type)` |
| temp tables ordered by implicit `rowid` | temp tables with an explicit `ord bigserial` (or `ROW_NUMBER()`) for insertion order |
| `unicase` collation (case-insensitive Unicode) | reuse exact Rust `unicase` via the pgrx extension (see §6); ICU `und-u-ks-level2` as fallback |

**Schema source of truth.** Maintain one canonical PG DDL representing Anki's *latest* schema state (no downgrade/schema-version negotiation is needed because we don't sync). The existing `schema11.sql` / `schema18` upgrades inform the target shape.

**IDs.** Keep Anki's millisecond-timestamp IDs (needed for `.apkg` export semantics and stable identity). Make uniqueness **concurrency-safe** (today's `CASE WHEN ?1 IN (SELECT id …) THEN max(id)+1` collision trick races under concurrency): use a dedicated allocator — an advisory-locked monotonic clamp per table, or a sequence whose values are clamped to `max(now_ms, last+1)`. Decision recorded in §8.

---

## 6. Custom functions & search

Anki search is **not** FTS5 — it parses its query syntax to an AST and emits SQL using **11 custom scalar functions + a `unicase` collation** registered per-connection (`rslib/src/storage/sqlite.rs:53–91`): `field_at_index`, `regexp`, `regexp_fields`, `regexp_tags`, `process_text`, `fnvhash`, `extract_original_position`, `extract_custom_data`, `extract_fsrs_variable`, `extract_fsrs_retrievability`, `extract_fsrs_relative_retrievability`. **All are pure/deterministic** and close over no collection state (verified).

Plan: ship a **pgrx PostgreSQL extension that reuses the exact same Rust function bodies**. Because it's the identical Rust code, search/sort results are **byte-for-byte equivalent** to SQLite — which is exactly what the differential tests assert. The search `sqlwriter` then needs little to no change (it keeps emitting `regexp_fields(...)` etc., now resolved by the extension). The remote PG connects as superuser (`postgres`), so installing an extension is possible.

**Fallback** (if deploying a native extension to the PG server is undesirable): move these predicates into the Rust app layer (fetch candidate rows, filter in Rust). This preserves exactness but loses index/pushdown efficiency; noted as a perf trade, not a correctness one.

---

## 7. Concurrency & multi-process model

- **Process model.** N `uvicorn` workers (or N containers), each embeds the forked rslib via PyO3 and holds its own PG connection(s). Parallelism comes from **multiple processes** (idiomatic for CPython/GIL); PG is the single shared source of truth. Within a process, `Collection` is `&mut self` (not thread-safe), so a process holds a single or small **pool** of collection handles and is effectively serial per handle — fine, because scaling is by process.
- **Locking.** Replace `BEGIN EXCLUSIVE` (`rslib/src/storage/sqlite.rs:589–629`) with ordinary PG transactions under **MVCC**. Per-card / per-note writes touch **disjoint rows** → naturally concurrent. **Structural** operations (deck tree, notetype, deck config) take explicit `SELECT … FOR UPDATE` row locks on the affected metadata rows.
- **Remove single-writer scaffolding:**
  - **USN** (single monotonic stream, `rslib/src/storage/sync.rs`) → **dropped**; it existed only for sync. PG transactions are authoritative.
  - **Global linear undo** (`rslib/src/undo/`) → **per-session** (see §8).
  - `mod` / `scm` timestamps → kept as plain change markers for the WebSocket bridge; no longer gate full-sync.
- **Isolation level.** Default **READ COMMITTED**; evaluate **REPEATABLE READ** (or a per-deck-tree advisory lock) for queue-building consistency.

---

## 8. Concurrency edge cases (decisions)

- **Undo → per-session.** Undo/redo stacks are keyed by a session id (per browser session / connection). An operation records undo entries scoped to its session; `undo()` reverts only that session's most recent op. If a later op from another session has invalidated the target, detect via row version/epoch and **refuse gracefully** (no-op + UI notice) rather than corrupt. The WS bridge broadcasts undo state per session.
- **ID generation.** Keep ms-timestamp IDs; guarantee uniqueness under concurrency with a **dedicated allocator**: take a short PG **advisory lock** per table during id assignment and compute `id = max(now_ms, last_id + 1)`. Cheap, monotonic, collision-free, preserves Anki semantics and export compatibility.
- **Daily-limit overshoot.** Limits are computed, not stored, so two concurrent answers may each independently see "N new left" and slightly overshoot (e.g., +1 new card). This is **benign and bounded**; documented. If exactness is ever required, wrap queue-build + answer in a per-deck-tree advisory lock.
- **Sibling burying.** When answering, lock the note's sibling card rows (`FOR UPDATE`) so concurrent sibling answers serialize correctly.
- **Media.** Filesystem, content-addressed; `write_data` already handles name collisions. Concurrent writes safe. Unchanged except the base dir becomes an absolute startup param.

---

## 9. Cross-process cache coherence (the genuinely novel piece)

rslib caches deck tree, notetypes, deck config, tags, and card queues in **per-process** `Collection.state`. With multiple processes these go stale (Anki never had to solve this — it was single-process).

Strategy:
1. **Cheap caches** (card queues) → rebuilt per request.
2. **Expensive shared metadata** (notetypes, decks, deck config, tags) → invalidated via **PG `LISTEN/NOTIFY`**: a writer `NOTIFY`s a per-kind channel on commit; every worker `LISTEN`s and drops the affected cache entry, then lazy-reloads.
3. **Epoch fallback.** A small monotonic `epoch` per metadata kind (a row in a `meta_epoch` table) checked cheaply; if a process's cached epoch is stale, it reloads. This guards against missed NOTIFYs (e.g., a worker that wasn't listening during a blip).

This subsystem is the highest-risk area and gets dedicated concurrency tests (§12).

---

## 10. Import / export / media boundary

`.apkg` and `.colpkg` **are SQLite database files** zipped with media — a format-level coupling that doesn't go away. We keep **`SqliteStorage` for the file side**:
- **Import:** open the package's SQLite via `SqliteStorage`, run Anki's existing import logic, and **copy rows into `PgStorage`**.
- **Export:** materialize the PG collection into a **temp SQLite collection** via `SqliteStorage`, then zip (reusing Anki's existing `.apkg`/`.colpkg` writers).
- **CSV** import/export operate on rows and are storage-agnostic.

Net: PG is the live store; SQLite survives only as the interchange format at the edges, preserving full ecosystem interop.

---

## 11. ankiweb-side changes (minimal)

- **Config.** Add a PG DSN (e.g. `ANKIWEB_PG_DSN`, default = the provided `postgresql://postgres:…@192.168.150.101:15432/ankiweb`), passed at startup. The "collection path" concept becomes the DSN.
- **Files.** Media/data live under a **configurable absolute base dir** passed at startup; **relative paths stored in the DB** (per user instruction). Generalize `config.py`'s `collection_path.parent`-derived dirs to an explicit `data_dir`.
- **CollectionService.** Open the collection with the DSN. Phase 1 keeps the existing `asyncio.Lock` + executor for parity; phase 2 relaxes it to a per-process pool to exploit concurrency. The aux read-only executor stays.
- **Deployment.** Run `uvicorn` with N workers / multiple containers, all pointing at the same PG.

---

## 12. Differential test strategy (the equivalence guarantee)

Three layers, strongest first:

1. **Reuse rslib's own Rust test suite against `PgStorage`.** Anki ships hundreds of backend tests that today run on `SqliteStorage`. Parametrize the storage backend so the suite also runs on `PgStorage`. **If Anki's own tests pass on PG, unit-level behavioral equivalence is guaranteed.** This is the centerpiece.
2. **Black-box differential harness.** Run identical operation sequences — add/update notes, answer cards through the v3 scheduler (incl. learning/lapse/leech/fuzz), search queries, stats, cardgen, FSRS — against **(a) stock anki 25.09.4 on SQLite** and **(b) forked anki on PG**. Snapshot and diff: normalized table state, **search result ordering**, scheduler **next-states**, **stats** outputs, **cardgen** outputs.
3. **ankiweb's existing 90-test suite** must stay green against the forked wheel (web screens, AnkiConnect actions, bridge, media, type-answer).

CI: dockerized PostgreSQL; `cargo test` (both backends) + `pytest`. Concurrency/load tests for §9 (multi-process writers + LISTEN/NOTIFY coherence).

---

## 13. Build & packaging

- Fork at **25.09.4** (worktree `pg-fork`). Toolchain: rustup (pinned `rust-toolchain.toml`), ninja/n2, protoc (have 25.3), python + hatchling.
- Build target: **`pylib/anki` wheel only** (rslib + `_rsbridge` cdylib + protobuf codegen). Skip aqt/web.
- New crates: `postgres` (sync driver) in rslib; `pgrx` for the separate custom-function extension. Pin `Cargo.lock`.
- ankiweb installs the locally-built wheel (path/editable) instead of PyPI `anki`.

---

## 14. Phasing / milestones (each independently verifiable)

- **M0 — prove fork→build→run loop (stock 25.09.4).** Build the unmodified 25.09.4 wheel locally; install into the ankiweb worktree; run ankiweb's 90 tests. *De-risks the whole project.*
- **M1 — `Storage` trait, no behavior change.** Abstract `SqliteStorage` behind `Storage`; it stays the sole impl; rslib + ankiweb tests stay green.
- **M2 — `PgStorage` skeleton + PG DDL + pgrx extension.** Get a minimal vertical slice (open collection, add/get note, basic search) green on PG via the parametrized rslib tests.
- **M3 — full port.** Translate all 92 `.sql` / 137 sites; the **entire rslib test suite passes on `PgStorage`**.
- **M4 — remove single-writer.** USN/exclusive/global-undo → per-session + row locks + LISTEN/NOTIFY; concurrency tests.
- **M5 — boundary + ankiweb wiring.** SQLite↔PG copier for import/export; ankiweb DSN + file-path config; ankiweb 90 tests green on the forked wheel.
- **M6 — multi-process + differential CI.** Multi-worker deployment; concurrency/load tests; differential harness in CI.

---

## 15. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Build toolchain skew | Pin `rust-toolchain.toml` + `Cargo.lock`; dockerize CI |
| pgrx extension deploy on remote PG | Superuser available; fallback = app-side filtering (§6) |
| Cross-process cache coherence bugs | LISTEN/NOTIFY + epoch checks + dedicated tests; can fall back to a single-writer-process phase if needed |
| Search/sort drift (PG regex/collation ≠ Rust) | pgrx reuses the **exact** Rust impls → no drift |
| ID uniqueness under concurrency | Advisory-locked monotonic allocator + tests |
| Scope (~1–2 months Rust) | Phased milestones, each verifiable; differential tests guard fidelity continuously |
| Undo semantics change (global→per-session) | Documented behavior change; acceptable for single-user/multi-process |

---

## 16. Notes

- Upstream Anki evidence cited above comes from a read of `/mnt/sda/git/tools/anki` (26.05) and is re-validated against the 25.09.4 fork during M1–M3; any line references are indicative.
- This spec resolves all previously-open design questions with the recommended option, per the user's delegation ("proceed with your recommendations").
