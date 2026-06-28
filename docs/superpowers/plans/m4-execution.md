# M4 Execution Plan — one PG collection, true multi-process concurrency

- **Date:** 2026-06-29
- **Status:** Concrete, sliced, testable execution plan. Read-only analysis; cites `file:line` evidence.
- **Goal:** make ONE PostgreSQL collection safe for **multiple OS processes** (one machine, shared FS for media) writing concurrently. Established context (do not re-derive): ankiweb = one singleton `Collection` per process, serialized in-process by `asyncio.Lock`+`max_workers=1`; target = N such processes on one PG collection; **NO Anki sync** (USN inert); daily limits **are stored**; PG currently has **NO** concurrency control (plain `BEGIN`, no `FOR UPDATE`/advisory locks); `PgStorage { client: RefCell<postgres::Client>, tx_depth, changes, last_rowid }`, one blocking connection per `Collection`/process.
- **Design input:** `docs/superpowers/m4-m5-design.md` (Part 2 = single-writer machinery; Part 3 = M4 design). This document turns that design into ordered, bite-sized, *individually testable* slices.
- **Fork root for all `rslib/...` citations:** `/mnt/sda/git/tools/anki-pg-fork/`.

> **Convention:** every slice is gated by (a) the existing no-DB gate `cargo test -p anki` = **332 passed / 0 / 6** staying green, AND (b) its own new env-gated concurrency test (`ANKI_TEST_PG_DSN=… cargo test … -- --ignored`). Concurrency tests are `#[ignore]`d so the default gate needs no database, exactly like `pg_search_matches_sqlite` (`storage/pg/mod.rs:430`).

---

## 0. Critical findings that shape the slices (read before slicing)

1. **The daily counters are NOT SQL columns — they live inside a protobuf `bytea` blob.** `decks` is `(id, name, mtime_secs, usn, common bytea, kind bytea)` (`storage/pg/schema.sql:111-118`); `new_studied`/`review_studied`/`last_day_studied`/`milliseconds_studied` are fields of the `DeckCommon` protobuf (`proto/anki/decks.proto:59-60`) that is `prost`-encoded into the `common` column (`storage/pg/deck.rs:65-66,144-145`, decoded `:7-9`). **Consequence:** the design doc's "atomic in-SQL increment `UPDATE decks SET new_studied = new_studied + $d`" (m4-m5-design §4.5) is **impossible as written** — there is no such column. Slice M4-5 must therefore use **`SELECT … FOR UPDATE` + the existing app-side decode→mutate→encode RMW**, or promote the counters to real columns (heavier). This is why M4-5 is the highest-correctness-risk slice.

2. **`postgres::Error` currently discards its `SqlState`.** `impl From<postgres::Error> for AnkiError` does `format!("{err:?}")` into `DbErrorKind::Other` (`error/db.rs:98-107`). The retry/locking slices (M4-2/3/4/5) need to *distinguish* serialization-failure (`40001`), deadlock (`40P01`) and unique-violation (`23505`). **Shared prerequisite:** extend that `From` impl to read `err.as_db_error().map(|e| e.code())` and map those three codes to distinct `DbErrorKind` variants — see slice **M4-INFRA**.

3. **The transaction closure is `FnOnce`** (`collection/transact.rs:8-11,59-64`), so a transparent retry wrapper cannot simply re-call it. Retryable hot-paths (e.g. `answer_card`, which re-reads the card fresh each attempt, `scheduler/answering/mod.rs:314-318`) need an `FnMut` variant; non-re-runnable ops fall back to **request-layer retry** in ankiweb (the design notes ops are idempotent at the request layer, m4-m5-design §4.5).

4. **Each `Collection` already has its own `CollectionState`** (caches + undo, `collection/mod.rs:159-174`), so even **two `Collection`s in one test process** have fully independent in-memory caches and undo stacks — they faithfully reproduce cross-process staleness. And **each `postgres::Client` is its own PG backend session**, so two threads/two clients faithfully reproduce two processes for *all* PG-level concurrency (advisory locks, `FOR UPDATE`, MVCC, `LISTEN/NOTIFY`). **Therefore nearly every M4 slice is testable with 2 `Collection`s in one `cargo test` binary — no subprocess needed.**

5. **What SQLite enforced and PG dropped:** SQLite serialized writers via `locking_mode=exclusive` + `begin exclusive` (`storage/sqlite.rs:51-53,394,473`); a 2nd opener got `SQLITE_BUSY`. The PG port replaced these with plain `BEGIN` (`storage/pg/mod.rs:358-364`, the comment at `:359-360` refers to a "connection/advisory-lock layer" that **does not exist**). `grep` confirms **zero** `FOR UPDATE`/`pg_advisory*`/`LISTEN`/`NOTIFY`/`SERIALIZABLE` anywhere in the fork. M4 builds all of it from scratch.

---

## Slice M4-0 — Concurrency test harness (FOUNDATIONAL, blocks everything)

**Scope.** A reusable helper that spins up **N `Collection`s against ONE shared PG schema**, plus a deterministic interleaving primitive. Today's `pg_sqlite_pair()` (`storage/pg/test_support.rs:64-91`) builds *one* PG collection in a *private* schema paired with SQLite — perfect for equivalence, useless for concurrency (each pair gets its own schema, torn down independently).

**Concrete change (`storage/pg/test_support.rs`, new helper — test-only, no production code):**
- `pub(crate) fn pg_shared_schema(n: usize) -> Option<PgShared>` — when `ANKI_TEST_PG_DSN` is set: create ONE schema `m4_<pid>_<ctr>` on the admin connection (reusing the `lock_admin()` pattern from `test_switch.rs:48-62`); build **`n` independent `Collection`s** all pointing at it via `?options=-csearch_path=<schema>%2Cpublic` (the exact DSN form already used at `test_support.rs:82`). The FIRST `build()` bootstraps; the rest take the connect-only open path (`open_or_create` → `is_bootstrapped()` true, `storage/pg/mod.rs:170-177`). Return `PgShared { cols: Vec<Collection>, schema, _guard }`; field order load-bearing (cols drop before `DROP SCHEMA … CASCADE`, mirroring `test_support.rs:27-35`).
- For the **bootstrap-race** slice specifically: a `pg_shared_unbootstrapped(n)` variant that creates the empty schema but does NOT pre-build — it hands back `n` DSN strings so the test drives `open_or_create` concurrently itself.
- `struct Interleave` util: two `std::sync::Barrier`s (or a tiny channel pair) so a test can pin two threads to "both reach point P, then proceed" — needed wherever a slice must *force* the race window (both writers do the racy read, THEN both write).

**Two driving styles the harness must support:**
- **Single-thread, two sessions (deterministic, preferred for lost-update/id/bootstrap):** each `Collection` is its own PG session, so one test thread can `begin` on col A, `begin` on col B, do the racy read on both, write both, commit both — interleaving by hand, no threads, fully reproducible.
- **Two threads + barrier (required where a call BLOCKS):** advisory-lock wait, `FOR UPDATE` wait, `LISTEN` poll. Thread per `Collection`; barrier to align the critical section.

**Dependencies:** none. **Must land first.**

**Test (the harness testing itself):** `pg_shared_schema(2)` → both collections `add_note` to disjoint notes → assert both rows visible to a third admin `SELECT count(*) FROM notes` = 2; teardown leaves no schema (`SELECT … FROM information_schema.schemata`).

---

## Slice M4-INFRA — Surface PG SqlState as typed error kinds (shared prerequisite)

**Scope.** Make `40001`/`40P01`/`23505` matchable in Rust (finding §0.2). Tiny, independent, unblocks M4-2/3/4/5.

**Concrete change:**
- `error/mod.rs` (`DbErrorKind`): add `SerializationFailure`, `Deadlock`, `UniqueViolation` variants.
- `error/db.rs:98-107` (`impl From<postgres::Error>`): before the lossy `format!`, do `let code = err.as_db_error().map(|e| e.code().clone());` and map `SqlState::T_R_SERIALIZATION_FAILURE` → `SerializationFailure`, `SqlState::T_R_DEADLOCK_DETECTED` → `Deadlock`, `SqlState::UNIQUE_VIOLATION` → `UniqueViolation`, else `Other`. (`postgres::error::SqlState` exposes these constants.)
- Add `AnkiError::is_retryable_serialization(&self) -> bool` (true for `SerializationFailure`/`Deadlock`) for the retry wrapper.

**Dependencies:** none. **Test:** force each code on a real connection (e.g. two sessions both `INSERT` the same PK → assert the loser's `AnkiError` carries `UniqueViolation`; a `FOR UPDATE` deadlock pair → `Deadlock`) via `pg_shared_schema(2)`.

---

## Slice M4-1 — USN neutralization + the 2 deferred sync methods (independent, lowest-risk)

**Scope.** ankiweb never syncs; `server=false` stamps `Usn(-1)` on every write (`storage/pg/sync.rs:16-18`); the `col.usn` counter is mutated only by sync (`collection/mod.rs:222-238` `before_upload`, the only caller of `increment_usn`/`clear_*_usns`). The two deferred PG methods `maybe_update_object_usns`/`objects_pending_sync` (`storage/pg/sync.rs:44,46`) are `todo!()` and are called **only** from `sync/collection/{changes,chunks}.rs`.

**Concrete change (PG mechanism: none needed — make dead code unreachable):**
- Confirm by `grep` that `sync/collection/` is the sole caller of the two `todo!()` methods + `before_upload` + `increment_usn`/`set_usn`/`clear_*_usns`. Then **feature-gate `sync/collection/` out of the PG/ankiweb build** (or `#[cfg(not(ankiweb))]`), making the two `todo!()`s unreachable by construction → convert them to `unreachable!("sync disabled in ankiweb PG build")`.
- Leave the `usn` **columns** in the schema at their inert defaults (`schema.sql:30-31` already documents this; 0/-1, zero schema churn). Do NOT remove columns.
- Keep `usn(server)` returning `Usn(-1)` for `server=false` (writes stay stamped -1, harmless without sync). No write-path change.

**Dependencies:** none (can land in parallel with M4-0). **Risk:** trivial — nothing observable changes for ankiweb.

**Test:** `pg_shared_schema(2)` → both processes `add_note`/`answer_card`; assert every non-sync path runs with `sync/collection/` gated off (compile-time) and that no write attempts to read a live USN; a `grep`-backed unit assert that `objects_pending_sync` has no remaining reachable caller. (Concurrency is incidental here; this slice is about removing dead machinery without breaking the non-sync paths.)

---

## Slice M4-2 — Race-safe first-bootstrap (replaces the `mod.rs:181` TODO)

**Scope.** Two processes opening a **fresh** schema both see `is_bootstrapped()==false` (`storage/pg/mod.rs:170,203-239`) and both run `create_schema()`+`insert_initial_col_row()`+stock-notetype bootstrap (`:183-189`) → duplicate `col` row (id=1 PK collision) / duplicate decks/notetypes, or a half-created schema. The `// TODO(M4): race-safe bootstrap via advisory lock` is exactly at `:181`.

**Concrete change (PG mechanism: `pg_advisory_xact_lock`):** wrap the **create branch** of `open_or_create` (`storage/pg/mod.rs:179-189`) in an explicit transaction guarded by a schema-scoped advisory lock, with a **re-check after acquiring**:
```
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended(current_schema(), 0));  -- one waiter at a time, per schema
-- re-test under the lock (the winner may have bootstrapped while we waited):
if is_bootstrapped() { COMMIT; return open-path }
create_schema(); insert_initial_col_row(); populate_default_config(server);
add_default_deck_config(); add_default_deck(); add_stock_notetypes();
COMMIT;   -- releases the xact lock
```
- `current_schema()` resolves to the private schema (first entry of `search_path`), so concurrent bootstraps of *different* collections don't serialize against each other — important because all per-test schemas share one DB and advisory locks are DB-global.
- The whole bootstrap becomes atomic (today the inserts run in autocommit, `storage/pg/mod.rs:183-189` — each statement self-commits, so a crash mid-bootstrap leaves the "partial/aborted bootstrap" state the code already fears at `:221-224`). Wrapping in BEGIN…COMMIT fixes that too.
- `is_bootstrapped` is unchanged but is now also called *inside* the lock.

**Dependencies:** M4-0 (harness), M4-INFRA (to assert the loser did NOT hit a PK error). Independent of M4-3..7.

**Concrete test (`pg_shared_unbootstrapped(2)` + 2 threads + barrier):** create an empty schema; two threads each call `PgStorage::open_or_create(dsn,…)` released simultaneously by a `Barrier`. Assert: **both** return `Ok` and usable (each can `get_deck(DeckId(1))` → the Default deck); an admin `SELECT count(*) FROM col` = **1**, `SELECT count(*) FROM decks WHERE id=1` = 1, stock notetype count = exactly the bootstrap set (not doubled); neither thread observed a `UniqueViolation`. Run it in a loop (×20) to beat the scheduler.

---

## Slice M4-3 — Concurrency-safe id allocation (the `max(id)+1` race)

**Scope.** Every create-path insert keeps SQLite's collision trick verbatim: `(CASE WHEN $1 IN (SELECT id FROM cards) THEN (SELECT max(id)+1 FROM cards) ELSE $1 END)` — at `storage/pg/card.rs:181-182`, `note.rs:82`, `deck.rs:133`, `notetype.rs:178`, `deckconfig.rs:37`, `revlog.rs:71`. Under SQLite's exclusive lock this read-then-insert was serialized; on concurrent PG two sessions inside their own snapshots can both read the same `max(id)` (and `TimestampMillis::now()` gives both the same ms, `card.rs:156`) → both compute the same id → one commits, the other fails PK `23505`, or (worse, if the candidate `$1` is itself free for both) two rows collide. Must preserve **ms-timestamp id semantics** (ids are monotonic, used as `creation`/sort, and apkg-export-compatible).

**Options evaluated:**
- **(a) `pg_advisory_xact_lock(<table key>)` immediately before each create-path INSERT** — serializes id minting *per table* within the already-open `rust` txn (`transact_inner` → `begin_rust_trx`, `transact.rs:16`, `storage/pg/mod.rs:319-330`); auto-released at commit. Preserves the `CASE WHEN` clamp **verbatim**, so id semantics are byte-identical to today; cost is one cheap lock per insert (serializes inserts to that one table — negligible for a single user). **← RECOMMENDED.**
- (b) Retry on `23505`: catch `UniqueViolation` (M4-INFRA) in `add_card`/etc., recompute candidate `= max(now_ms, max(id)+1)`, retry. Works under READ COMMITTED (retry sees the committed max) but needs per-method retry plumbing and re-running the encode.
- (c) A PG sequence clamped to `GREATEST(now_ms, nextval)`. Lock-free, monotonic, but introduces sequence gaps and diverges from the "pure ms-timestamp" shape — rejected (semantics drift, apkg-export risk).

**Concrete change:** add `self.client … execute("SELECT pg_advisory_xact_lock($1)", &[&TABLE_KEY])` (a distinct constant `bigint` per table) just before each of the 6 create-path INSERTs. Keep the `CASE WHEN` exactly as-is.

**Dependencies:** M4-0, M4-INFRA. Shares nothing structural with M4-5/6/7; can run in parallel with M4-2.

**Concrete test (`pg_shared_schema(2)`, two threads + barrier, OR single-thread two-session):** monkeypatch / freeze `TimestampMillis::now()` to return the SAME value to both writers (or just hammer `add_note` in a tight loop on both). Each thread adds K notes (and their cards). Assert: **zero** `UniqueViolation`; total notes = 2K, total cards = 2K×templates; all ids **distinct** and **monotonic non-decreasing**; ids still ≥ the frozen ms stamp (semantics preserved). Without the lock, the loop reliably produces a `23505`.

---

## Slice M4-4 — Transaction isolation level + retry-on-serialization-failure wrapper

**Scope.** Pick the isolation level and add a bounded retry around write transactions. Default PG is **READ COMMITTED**; combined with the targeted row locks (M4-5) and advisory locks (M4-2/3) that is sufficient and avoids the broad `40001` storms that SERIALIZABLE would create. The retry is mainly for **deadlocks (`40P01`)** from inconsistent `FOR UPDATE` ordering on the deck parent-chain (M4-5) and any future SERIALIZABLE paths.

**Concrete change:**
- **Keep READ COMMITTED** (do not switch `begin_rust_trx`/`begin_trx` to SERIALIZABLE). Document the decision in the `begin_trx` comment that currently mis-promises an "advisory-lock layer" (`storage/pg/mod.rs:358-360`).
- Add `Collection::transact_retry<F>(op, f)` where `F: FnMut(&mut Collection) -> Result<R>` (note: `FnMut`, not `FnOnce` — finding §0.3), looping `transact(op, &mut f)` up to N=5 times, retrying only when `err.is_retryable_serialization()` (M4-INFRA), with jittered backoff; on the Nth failure, return the error. Place beside `transact` (`collection/transact.rs:59`).
- Route the **answer hot-path** through it: `answer_card` (`scheduler/answering/mod.rs:310-312`) already re-reads the card fresh each attempt (`:314-318`), so its closure is naturally re-runnable as `FnMut`. Ops whose closures can't be made `FnMut` cheaply keep plain `transact` and rely on **ankiweb request-layer retry** (re-issue the whole `run_op`, idempotent per m4-m5-design §4.5).

**Dependencies:** M4-INFRA (error kinds). Should land **before** M4-5 (M4-5's `FOR UPDATE` on the parent chain is the main deadlock source the retry covers).

**Concrete test (`pg_shared_schema(2)`, two threads + barrier):** construct a deterministic deadlock: thread A locks deck X then (after barrier) deck Y `FOR UPDATE`; thread B locks Y then X — one gets `40P01`. Wrap both in `transact_retry`; assert both ultimately succeed (the victim retries and wins on its second pass) and the final state is consistent (no lost write). Assert N is bounded (a permanent conflict fails after 5, not forever).

---

## Slice M4-5 — Daily-limit counters: `SELECT … FOR UPDATE` + atomic RMW (HIGHEST correctness risk)

**Scope.** Every card answer read-modify-writes the per-deck `new_studied`/`review_studied`/`milliseconds_studied` counters up the **entire parent chain** (`scheduler/answering/mod.rs:337,414-438` → `decks/stats.rs:22-41`). `update_deck_stats_single` (`decks/stats.rs:74-89`) is `get_deck → reset_stats_if_day_changed → mutator → set_modified → update_single_deck_undoable` — a pure app-side RMW. Because the **root deck is an ancestor of every deck**, two concurrent answers both read `studied=N` on the root, both write `N+1`, one increment lost → daily limit drifts (over/under-study). The limit is *enforced* by subtracting these stored counters from the cap at queue build (`decks/limits.rs:98-105`), so the drift is user-visible. This is the one genuine shared-mutable hot-spot.

**The wrinkle that kills the "atomic in-SQL increment" plan (finding §0.1):** the counters are fields of the `DeckCommon` **protobuf** serialized into `decks.common bytea` (`decks.proto:59-60`, `storage/pg/deck.rs:65-66,144`). There is **no `new_studied` column** to `UPDATE … SET new_studied = new_studied + $d`.

**Options:**
- **(a) `SELECT … FOR UPDATE` the deck + parent rows, then the EXISTING decode→mutate→encode→UPDATE RMW.** The row lock serializes the RMW across processes (READ COMMITTED + `FOR UPDATE` re-reads the latest committed row), so no lost update. Faithful, zero schema change, minimal churn — the only new code is acquiring the lock and ordering it. **← RECOMMENDED.** Mechanism: change `update_deck_stats` (`decks/stats.rs:34-38`) so the deck + `parent_decks` rows are fetched `FOR UPDATE` (a new `get_decks_for_update(&[DeckId])` PG method issuing `SELECT … FROM decks WHERE id = ANY($1) ORDER BY id FOR UPDATE` — **`ORDER BY id` is load-bearing**: it gives a global lock order so two answers in overlapping subtrees can't deadlock). Then the existing `update_deck_stats_single` RMW runs against the locked rows. `reset_stats_if_day_changed` (`decks/stats.rs:7-16`) stays app-side and is now race-free (it runs while holding the lock).
- (b) Promote `new_studied`/`review_studied`/`learning_studied`/`milliseconds_studied`/`last_day_studied` to real `bigint` columns on `decks`, enabling a true lock-free `UPDATE decks SET new_studied = CASE WHEN last_day_studied=$today THEN new_studied+$d ELSE $d END, last_day_studied=$today, … WHERE id = ANY($ancestors)`. Higher throughput, but a schema change + dual-write to keep the protobuf and columns consistent (or stop storing them in the protobuf, diverging from SQLite's shape and from apkg export). Hold as the escalation if (a)'s root-row serialization ever bites (it won't for one user).
- (c) Drop the stored counters, derive "done today" from `revlog` at queue build — lock-free but a core-scheduler behavior change (m4-m5-design §4.5b / decision #3). Out of scope unless the user picks it.

**Same fix applies to `extend_limits`** (`decks/stats.rs:48-70`, the custom-study limit adjust) — it does the identical parent-chain RMW and must use the same `FOR UPDATE` path.

**Dependencies:** M4-0, M4-INFRA, **M4-4** (the `FOR UPDATE` ordering deadlock is what the retry wrapper backstops). This is the coupled core: M4-INFRA → M4-4 → M4-5.

**Concrete test (`pg_shared_schema(2)`, two threads + barrier):** build a deck subtree `root → parent → {A, B}`; seed cards in A and B. Both threads answer one card simultaneously (barrier-aligned at the `update_deck_stats` entry). Assert the **root** and **parent** `new_studied`/`review_studied` summed correctly = **2** (no lost update), and A's and B's own counters each = 1. Run ×50 to beat MVCC. Add a second test that the limit is *enforced*: cap root `new=1`, two concurrent new-card answers → exactly one is allowed past the cap on the next queue build (counters can't both think they were first). Without `FOR UPDATE`, the root counter lands on 1 (one increment lost) and the limit over-admits.

---

## Slice M4-6 — Cross-process cache invalidation (HIGHEST overall risk — novel subsystem)

**Scope.** Four caches go stale when *another* process writes, and **no remote-invalidation hook exists** — every invalidation today fires only on *this* process's own `transact`/`undo` (m4-m5-design §D):
- `deck_cache` — read-through, hands out a stale `Arc<Deck>` (`decks/mod.rs:174-185`); populated `:180`, cleared only by local `clear_caches` (`collection/mod.rs:240-243`) / `decks/undo.rs`.
- `notetype_cache` — same shape (`notetype/mod.rs:208-213`).
- `scheduler_info` — populated `scheduler/mod.rs:46`, cleared locally `:132` + on config change.
- `card_queues` — populated `scheduler/queue/mod.rs:250`, cleared by `clear_study_queues` (`:207-209`) + `maybe_clear_study_queues_after_op` (`:211-215`).

(There is **no config cache and no tag cache** — config/tags read through to storage each time, `collection/mod.rs` CollectionState has no such field — so the coherence surface is exactly these 4.)

**Constraint (verified):** `PgStorage` holds **one synchronous blocking `postgres::Client`** (`storage/pg/mod.rs:62`, `NoTls`) used under ankiweb's `asyncio.Lock` — it **cannot also block on `LISTEN`**. So any `LISTEN` poll needs a **second connection on a background thread**.

**Concrete change — epoch-stamp primary, `LISTEN/NOTIFY` optional optimization:**

1. **`card_queues` → rebuild-on-mismatch (cheapest, do first).** The queue already **self-detects** staleness: `get_queued_cards` asserts `card.mtime == entry.mtime()` and **hard-errors** otherwise (`scheduler/queue/mod.rs:111-117`). Under multi-process, process A reviewing while B edits a queued card trips this `require!`. Change it from "error" to "**drop the cached queue (`self.state.card_queues = None`) and rebuild**" — turning a remote edit into a transparent rebuild. `card_queues` is cheap to rebuild and `which cards are due` is recomputed live every build (`scheduler/queue/builder/gathering.rs`, m4-m5-design §E), so this is safe.

2. **Metadata (`deck_cache`/`notetype_cache`/`scheduler_info`) → epoch table (primary, robust).** New table `meta_epoch (kind text PRIMARY KEY, epoch bigint NOT NULL)` seeded with `deck`/`notetype`/`sched`. Every writer that mutates a deck/notetype/config bumps the matching kind **in the same `rust` txn** (so the bump commits atomically with the write): `INSERT INTO meta_epoch(kind,epoch) VALUES($1,1) ON CONFLICT(kind) DO UPDATE SET epoch = meta_epoch.epoch + 1`. Hook points = the existing storage writers (`storage/pg/deck.rs` add/update/remove, `storage/pg/notetype.rs`, config writers). Each `PgStorage` keeps a `Cell<i64>` of last-seen epoch per kind; at op start (cheapest place: the top of `transact_inner`, `collection/transact.rs:16`, or lazily inside `get_deck`/`get_notetype`) it does ONE indexed `SELECT kind,epoch FROM meta_epoch`; any advanced kind → call the matching existing invalidator (`clear_caches()` for deck+notetype, `scheduler_info=None` for sched). **Survives missed notifications, needs no extra connection** — this is the source of truth.

3. **`LISTEN/NOTIFY` (optional optimization, only if epoch-poll latency bites).** Writer `NOTIFY anki_cache, '<kind>'` after commit; a **dedicated second `postgres::Client`** owned by a per-process background thread blocks on `client.notifications()` and flips an `Arc<AtomicU64>` "remote epoch" the main thread reads (instead of the `SELECT`) at op start. The main thread (under the asyncio lock) NEVER blocks on LISTEN — it only reads the atomic. The epoch `SELECT` remains the fallback/source-of-truth. **Where the poll lives:** a 2nd connection + thread spun up in `PgStorage::open` (or by ankiweb's `CollectionService.open`), torn down on close. Defer this sub-step behind the epoch mechanism; ship epoch first.

**Dependencies:** M4-0. Largely independent of M4-2/3/5 (touches reads/caches, not the id/counter write mechanics), but **logically pairs with M4-5/M4-7** (an undo or a counter RMW wants fresh rows). The `card_queues` sub-step (1) is independent and can land alone.

**Concrete test (`pg_shared_schema(2)`, single-thread two-session is enough — independent caches):**
- *Metadata:* A `get_deck(d)` (populates A's `deck_cache`, `decks/mod.rs:175-180`). B `rename_deck(d, "new")` + commit (bumps `meta_epoch('deck')`). A `get_deck(d)` again → with the epoch check A clears its cache and returns name "new". Assert A observes B's change **without restart**. Baseline (assert-the-bug, pre-fix): A returns the stale name.
- *Queues:* A `get_queued_cards` (caches a queue with card c at mtime M). B answers/edits c (mtime → M'). A `get_queued_cards` again → asserts it **rebuilds** transparently (no `require!` panic, returns c at M'). Pre-fix this is the hard error at `scheduler/queue/mod.rs:111-117`.

---

## Slice M4-7 — Per-process undo + staleness guard

**Scope.** The `UndoManager` is already per-process (in-memory `VecDeque` on `CollectionState.undo`, `collection/mod.rs:161`, `undo/mod.rs:58-68`, never persisted). A second process has its own empty stack and cannot see this one's steps — that's acceptable for single-user (decision #2). The real risk: `undo()` replays a *logical inverse* against **whatever the DB currently holds** (`undo/mod.rs:285-304`: `transact(undone_op, |col| for change in changes.rev() { change.undo(col)? })`), with **no detection** if another process changed those rows since the step was recorded → silent clobber of the other process's edit.

**Concrete change (staleness guard, refuse-don't-corrupt):**
- The undo steps already capture the **full previous object** (e.g. `UndoableChange::Card(Card)` holds the pre-image; `undo/changes.rs`). Add a **precondition check** in `undo_inner` (or per-`UndoableChange::undo`): before applying an inverse, re-read the target row's current `mtime`/`usn` and compare against the mtime the undo step expects to overwrite (captured at record time). If the row moved underneath us (another process wrote it), **abort the whole undo** with a clear `AnkiError` (a new `UndoTargetChanged` kind) → no-op + UI notice, rather than overwrite. Because `undo_inner` wraps the replay in a fresh `transact` (`undo/mod.rs:291`), the abort rolls the whole undo back cleanly.
- Optionally take `FOR UPDATE` on the target rows during the undo `transact` so the re-read + apply is atomic (reuses M4-5's `get_*_for_update`).

**Dependencies:** M4-0; benefits from M4-6 (so the undo path sees fresh, non-cached rows) and M4-5's `FOR UPDATE` helpers. Lowest urgency of the substantive slices (single-user rarely undoes across a concurrent edit), but cheap once M4-5/6 exist.

**Concrete test (`pg_shared_schema(2)`):** A answers card c (records an undo step capturing c's pre-image, mtime M). B edits c (mtime → M'). A `undo()` → assert it **refuses** with `UndoTargetChanged` and leaves B's edit intact (c still at M', not clobbered back to the pre-image). Control: if B did NOT touch c, A `undo()` succeeds and restores the pre-image. Add the inverse for redo.

---

## Recommended slice order (dependencies + parallelism)

```
M4-0  Test harness ........................ FOUNDATIONAL — must land first
M4-INFRA  Typed SqlState error kinds ...... tiny; unblocks 2/3/4/5
            │
   ┌────────┼─────────────────────────────┐
   │ (independent, parallelizable once 0+INFRA exist)
M4-1  USN neutralization .................. fully independent (no PG-concurrency dep; can land any time after 0)
M4-2  Race-safe bootstrap ................. advisory lock; independent of 3..7
M4-3  Id allocation ....................... advisory lock; independent of 2,5,6,7
   └──────────────────────────────────────┘
M4-4  Isolation + retry wrapper ........... before 5 (backstops its FOR UPDATE deadlocks)
M4-5  Daily-limit counters (FOR UPDATE) ... coupled core: INFRA → 4 → 5
M4-6  Cross-process cache invalidation .... mostly independent; pairs with 5/7; sub-step (1) lands alone
M4-7  Undo staleness guard ................ last; reuses 5's FOR UPDATE + benefits from 6
```

- **The coupled critical path** is `M4-INFRA → M4-4 → M4-5` (error kinds → retry → counters).
- **Fully independent, parallelizable** after M4-0/INFRA: **M4-1** (USN), **M4-2** (bootstrap), **M4-3** (id alloc), and **M4-6 sub-step (1)** (the `card_queues` require!→rebuild). These touch disjoint files and can be done in any order / by separate agents.
- **M4-6** (metadata epoch + LISTEN/NOTIFY) and **M4-7** (undo guard) come after the write-safety core so their tests run against a system that no longer loses updates.

---

## Concurrency test harness (one-paragraph proposal)

Extend `storage/pg/test_support.rs` with `pg_shared_schema(n) -> Option<PgShared>`: when `ANKI_TEST_PG_DSN` is set, create **one** schema `m4_<pid>_<ctr>` on the shared admin connection (the `lock_admin()`/`OnceLock<Mutex<Client>>` pattern already in `test_switch.rs:48-62`), then build **n independent `Collection`s** all pointed at it via the existing `?options=-csearch_path=<schema>%2Cpublic` DSN (`test_support.rs:82`) — the first `build()` bootstraps, the rest hit `is_bootstrapped()` and connect-only (`storage/pg/mod.rs:170-177`); return them in a `PgShared { cols, schema, _guard }` whose RAII `_guard` (declared last) drops the schema `CASCADE` after every collection's session closes (same teardown invariant as `PgSqlitePair`, `test_support.rs:27-52`). Because **each `Collection` owns its own `CollectionState`** (independent caches/undo, `collection/mod.rs:159-174`) **and its own `postgres::Client`** (independent PG backend session), two collections *in one test process* faithfully reproduce two OS processes for every PG-level race AND for cross-process cache staleness — so the entire M4 suite runs in `cargo test` with **no subprocess**. Tests use one of two styles: **single-thread two-session** (deterministic; `begin`/racy-read/write/commit interleaved by hand — for lost-update, id-race, bootstrap, cache) or **two threads + a `std::sync::Barrier`** (where a call must block — advisory-lock wait, `FOR UPDATE` wait, deadlock, `LISTEN` poll). All concurrency tests are `#[ignore]`d (run only with `ANKI_TEST_PG_DSN … -- --ignored`), keeping the default `cargo test -p anki` = **332/0/6** gate database-free; each race test loops ×20–50 to beat the scheduler, and asserts via an independent admin `SELECT` so the assertion doesn't go through a possibly-stale cache.

---

## Top risks

1. **M4-6 (cross-process cache invalidation) is the single highest-risk slice.** It is genuinely novel (fork `grep` = 0 prior art), it is forced into an awkward architecture by the **one blocking `postgres::Client` under the asyncio lock** (`storage/pg/mod.rs:62`) — `LISTEN` needs a *second* connection + background thread per process, interacting with the `RefCell`/single-thread model — and its failure mode is **silent staleness** (a wrong `Arc<Deck>`/`Arc<Notetype>` handed across the whole app). Mitigation: ship the **epoch-stamp** mechanism (one indexed `SELECT` at op start, no extra connection, survives missed notifications) as the robust primary and treat `LISTEN/NOTIFY` as a *later* latency optimization — this removes the architectural risk from the critical path.

2. **M4-5 (daily-limit counters) is the highest-CORRECTNESS-risk hot path.** The counters are buried in a protobuf `bytea` (finding §0.1), so the design's "atomic in-SQL increment" is impossible — the realistic fix is `SELECT … FOR UPDATE` on the deck **parent chain**, whose lock ordering must be globally consistent (`ORDER BY id`) or it deadlocks, and whose **root-deck row is a serialization point for every answer**. Lost updates here silently corrupt the user's daily study limits. Mitigation: `ORDER BY id FOR UPDATE` + the M4-4 retry backstop; escalate to column-promotion (option b) only if root-row contention ever matters (it won't for one user).

3. **Retry vs. `FnOnce` closures (finding §0.3).** `transact` is `FnOnce` (`collection/transact.rs:8-11`); a transparent Rust-side retry needs `FnMut`, which only some ops support. Risk: silently non-retried ops surface deadlocks to the user. Mitigation: `transact_retry` (`FnMut`) for the answer hot-path (already re-reads fresh), **plus** ankiweb request-layer retry for the rest.

4. **Advisory-lock key collisions across per-test schemas.** Advisory locks are **DB-global**, but every per-test schema shares one DB. Keying bootstrap/id locks on a *constant* would serialize unrelated tests (or, worse, let a leaked lock wedge them). Mitigation: key on `hashtextextended(current_schema(), 0)` (M4-2) / a per-table constant *namespaced under nothing schema-global* — verify in the harness that two different schemas never block each other.

5. **USN dead-code removal breadth (M4-1).** Feature-gating `sync/collection/` off must not amputate a non-sync caller. Mitigation: the `grep`-backed caller audit in M4-1 before gating, and the no-DB 332/0/6 gate catching any newly-dead reference at compile time.
