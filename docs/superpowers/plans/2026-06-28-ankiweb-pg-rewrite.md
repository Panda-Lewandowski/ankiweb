# ankiweb → PostgreSQL Rewrite — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ankiweb's storage engine SQLite→PostgreSQL by forking Anki's Rust backend (`rslib`) at 25.09.4, enabling true concurrent multi-process writes against one collection while perfectly replicating current ankiweb functionality.

**Architecture:** Fork `rslib`, abstract its hard-wired `SqliteStorage` behind a `Storage` trait, add a `PgStorage` impl (sync `postgres` crate, no async ripple), remove the single-writer machinery (USN/exclusive-lock/global-undo), get concurrency from PG MVCC + row locks, keep SQLite only at the `.apkg`/`.colpkg` import/export boundary. ankiweb consumes the rebuilt wheel with minimal change (DSN + data dir).

**Tech Stack:** Rust (rslib, sync `postgres` crate, `pgrx` extension), PyO3/`_rsbridge`, Anki's ninja/n2 build (`./ninja wheels:anki`), Python 3.12 / FastAPI (ankiweb), PostgreSQL 15+ (`postgresql://…@192.168.150.101:15432/ankiweb`), pytest.

See companion spec: `docs/superpowers/specs/2026-06-28-ankiweb-pg-rewrite-design.md`.

## Global Constraints

- Anki fork pinned to tag **25.09.4** (commit `d52ca669f`); never bump to 26.05.
- Rust toolchain pinned by fork's `rust-toolchain.toml` = **1.89.0** (rustup auto-fetches).
- Build only **`wheels:anki`** (pylib), never `wheels:aqt` (avoids node/web build).
- ankiweb HTTP/WS API surface, the 134 AnkiConnect actions, the 18 screens, and vendored SvelteKit bundles must remain **functionally unchanged**.
- Equivalence to upstream Anki core is verified by tests (see spec §12); the strongest gate is **rslib's own Rust test suite passing on `PgStorage`**.
- Work happens in the worktree `worktree-pg-rewrite` (ankiweb) and the `pg-fork` branch (anki). Never disturb the running instance at `/mnt/sda/git/web/ankiweb`.
- Local commits on feature branches only; **do not push** to any remote without explicit instruction.

---

## Roadmap (subsystems → their own spec→plan→implement cycle)

| Milestone | Deliverable (working, testable) | Detailed plan |
|---|---|---|
| **M0** | Stock 25.09.4 `anki` wheel builds locally; ankiweb 90-test suite green against it | **this doc** |
| **M1** | `Storage` trait introduced; `SqliteStorage` sole impl; rslib + ankiweb tests green (no behavior change) | **this doc** |
| M2 | `PgStorage` skeleton + PG DDL + pgrx custom-fn extension; minimal vertical slice (open / add+get note / basic search) green on PG via parametrized rslib tests | authored at M2 start |
| M3 | All 92 `.sql` / 137 sites ported; **entire rslib test suite green on `PgStorage`** | authored at M3 start |
| M4 | Single-writer removed (USN/exclusive/global-undo → per-session + row locks + LISTEN/NOTIFY); concurrency tests | authored at M4 start |
| M5 | SQLite↔PG copier for import/export; ankiweb DSN + data-dir config; ankiweb 90 tests green on forked wheel | authored at M5 start |
| M6 | Multi-process deployment; concurrency/load tests; differential harness in CI | authored at M6 start |

M2–M6 are scoped in the spec; each gets a bite-sized plan when reached because its step detail depends on the 25.09.4 storage surface catalogued in **Task M1.0**.

---

## M0 — Prove the fork → build → run loop (stock 25.09.4)

**Purpose:** De-risk the entire project: confirm we can build the forked wheel locally and that ankiweb runs against a locally-built (vs PyPI) `anki`. No code changes to anki yet.

**Files:**
- Use: `/mnt/sda/git/tools/anki-pg-fork` (fork worktree @25.09.4)
- Use: `/mnt/sda/git/web/ankiweb/.claude/worktrees/pg-rewrite` (ankiweb worktree)
- Create: `out/wheels/anki-*.whl` (build artifact, in fork)

- [ ] **Step 1: Build the stock pylib wheel** *(in flight — background build `wheels:anki`)*

```bash
cd /mnt/sda/git/tools/anki-pg-fork && source "$HOME/.cargo/env"
RELEASE=2 ./ninja wheels:anki
```
Expected: exit 0; `out/wheels/anki-25.9.4-*.whl` produced.

- [ ] **Step 2: Create an isolated venv for the forked wheel** (don't pollute the anaconda base the running instance uses)

```bash
cd /mnt/sda/git/web/ankiweb/.claude/worktrees/pg-rewrite
uv venv .venv-fork --python 3.12 && source .venv-fork/bin/activate
uv pip install -e . --reinstall-package anki  # install ankiweb deps
uv pip install out_wheel_path  # the just-built anki wheel (overrides PyPI anki)
```
Expected: `python -c "import anki; from anki.buildinfo import version; print(version)"` → `25.09.4`, imported from the local wheel (not site-packages PyPI copy).

- [ ] **Step 3: Fetch web assets** (ankiweb needs vendored frontend assets to run its tests)

```bash
python tools/fetch_web_assets.py   # or per UPSTREAM.md; assets are gitignored
```
Expected: `ankiweb/web_assets/` populated.

- [ ] **Step 4: Run ankiweb's test suite against the forked wheel**

```bash
python -m pytest -q   # full suite is 90 files incl. playwright E2E
```
Expected: same pass/fail profile as the running instance on PyPI anki. If playwright E2E needs browsers/servers and is flaky in this env, run the non-E2E subset and record which suites were skipped and why (no silent omission).

- [ ] **Step 5: Record the M0 result** in the status report; mark M0 done only if the wheel builds AND the (non-environmental) tests pass.

---

## M1 — Introduce a `Storage` trait (no behavior change)

**Purpose:** Create the seam `PgStorage` will plug into, without changing behavior. The safety net is the existing test suite staying green. This is a large but mechanical refactor; do it module-group by module-group with green gates and frequent commits.

**Files (in the fork `/mnt/sda/git/tools/anki-pg-fork`):**
- Read/catalog: `rslib/src/storage/**` (esp. `mod.rs`, `sqlite.rs`, per-entity `*/mod.rs`)
- Create: `rslib/src/storage/traits.rs` (the `Storage` trait)
- Create: `docs/pg-rewrite/storage-surface.md` (inventory, Task M1.0 output)
- Modify: `rslib/src/storage/mod.rs` (export trait), `rslib/src/collection/mod.rs` (the `storage` field type), and the `.storage.`/`storage.db` consumers
- Test: rslib's own suite (`./ninja check:rust` / `cargo test -p anki`) + ankiweb suite

### Task M1.0: Catalog the 25.09.4 storage surface

- [ ] **Step 1:** Read `rslib/src/storage/` in the fork. Catalog into `docs/pg-rewrite/storage-surface.md`:
  - Every public method on `SqliteStorage`, grouped by entity (card / note / deck / notetype / deckconfig / revlog / tag / graves / config / meta+timestamps / search scaffolding / transaction control).
  - The **22 raw `storage.db.` access sites** (file:line) — these are the leaks to encapsulate first.
  - The **137 `prepare_cached` sites** and the **92 `.sql` files** (path list).
  - Each custom function + the `unicase` collation registration site.
- [ ] **Step 2:** Confirm exact counts for 25.09.4 (they were measured on 26.05; re-verify).
- [ ] **Step 3: Commit** the inventory doc.

```bash
git -C /mnt/sda/git/tools/anki-pg-fork add docs/pg-rewrite/storage-surface.md
git -C /mnt/sda/git/tools/anki-pg-fork commit -m "docs(pg): catalog 25.09.4 storage surface"
```

### Task M1.1: Establish the green rslib baseline

- [ ] **Step 1:** Build and run rslib's own Rust tests on the unmodified fork:

```bash
cd /mnt/sda/git/tools/anki-pg-fork && source "$HOME/.cargo/env"
./ninja check:rust   # or: cargo test -p anki --release
```
- [ ] **Step 2:** Record the baseline (N passed). This is the refactor's safety net. Expected: all green (it's an unmodified release tag).

### Task M1.2: Define `StorageBackend` enum dispatch (decided via M1.0 — NOT `dyn`)

The M1.0 catalog found ~12 non-object-safe methods (generic closures like `for_each_card_in_search<F: FnMut>`, plus `<T: FromSql>`/`<I: ToSql>` sync methods), so `Box<dyn Storage>` is **out**. Use a concrete enum (generic/closure methods are fine on a concrete type; static dispatch = no runtime cost).

- [ ] **Step 1:** Add `pub enum StorageBackend { Sqlite(SqliteStorage), Pg(PgStorage) }` in `rslib/src/storage/mod.rs`. Until M2 the `Pg` variant is omitted (added in M2). Generate one delegating method per surface method (`match self { Self::Sqlite(s) => s.method(args), … }`) via a `macro_rules!` over the catalog's §2 method list.
- [ ] **Step 2:** USN/sync methods (catalog `*`) are NOT delegated to `Pg` — they're removed in M4; keep them Sqlite-only.
- [ ] **Step 3:** `./ninja check:rust` → still green (enum added, not yet consumed). **Commit.**

### Task M1.3: Encapsulate the 22 raw `storage.db` accesses

- [ ] For each of the 22 sites (from M1.0): add a named method to the `Storage` trait + `SqliteStorage` impl that performs that exact query, and replace the raw `storage.db.…` call with the trait method. Group commits by subsystem. Green gate (`./ninja check:rust`) after each group. **Commit per group.**

### Task M1.4: Switch `Collection.storage` to `StorageBackend`

- [ ] **Step 1:** Change the `storage` field type on `Collection` (`rslib/src/collection/mod.rs`) from `SqliteStorage` to `StorageBackend`, wrapping the opened storage in `StorageBackend::Sqlite(...)`.
- [ ] **Step 2:** Fix the resulting type errors across the **526 `.storage.` call sites** mechanically (they resolve to the enum's same-signature delegating methods, so most sites compile unchanged). Work module-group by module-group; `./ninja check:rust` green after each; **commit per group**.
- [ ] **Step 3:** Full rslib suite green.

### Task M1.5: Rebuild wheel, re-verify ankiweb (no behavior change)

- [ ] **Step 1:** `RELEASE=2 ./ninja wheels:anki` in the fork → new wheel.
- [ ] **Step 2:** Reinstall into `.venv-fork`; run ankiweb's suite. Expected: **same green profile as M0** (the refactor changed no behavior).
- [ ] **Step 3: Commit**; tag M1 complete in the status report.

---

## Notes on execution

- **Equivalence-first:** never "fix" a behavior to make a test pass on the new backend; the differential tests (M3+) exist to catch drift. If stock and forked diverge, that's a bug in the port, not the oracle.
- **Frequent commits** at each green gate; the refactor must be bisectable.
- **No silent caps:** if any test subset is skipped (E2E/browser/env), say so explicitly in the status report.
- M2 begins by authoring its own bite-sized plan from the M1.0 catalog (PG DDL per entity, per-`.sql` translation tasks, the pgrx extension, and parametrizing the rslib test suite over `{SqliteStorage, PgStorage}`).
