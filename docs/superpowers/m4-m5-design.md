# M4 (remove single-writer) + M5 (ankiweb on PG) — Design

- **Date:** 2026-06-28
- **Status:** Design for the next phase. Read-only analysis; cites `file:line` evidence.
- **Inputs:** approved spec `docs/superpowers/specs/2026-06-28-ankiweb-pg-rewrite-design.md`, plan `…/plans/2026-06-28-ankiweb-pg-rewrite.md`, live `docs/superpowers/STATUS-pg-rewrite.md`.
- **Where we are:** M3 is essentially done — the fork's PG storage backend (`PgStorage`) is behaviorally identical to SQLite and rslib's own suite passes on PG (332/0/6). The collection still **replicates** Anki's single-writer model. M4 removes that machinery; M5 makes ankiweb actually run on PG.
- **Roots:** ankiweb worktree `/mnt/sda/git/web/ankiweb/.claude/worktrees/pg-rewrite/` (app pkg `ankiweb/`); fork `/mnt/sda/git/tools/anki-pg-fork/` (`rslib/src/`).

---

## 0. Executive summary

**Part-1 answers (how ankiweb uses the collection today):**

| Question | Answer | Key evidence |
|---|---|---|
| **Lifecycle** | One long-lived **singleton** `Collection`, opened once at startup, shared by both servers. | `collection_service.py:34,47`; opened `__main__.py:16-17`; shared into web + AnkiConnect apps `__main__.py:20-24` |
| **Process/worker model** | **One** OS process, one asyncio loop, two in-loop uvicorn servers (8000 + 8765), one collection worker thread (+4 aux threads for FSRS/progress only). No gunicorn, no `workers=`. | `__main__.py:44` (`asyncio.run`), `:33` (`asyncio.gather`); `collection_service.py:28,32` |
| **Concurrency today** | `asyncio.Lock` + `max_workers=1` ThreadPoolExecutor serialize **all** collection access; a lock-free 4-thread aux pool runs only an allow-listed set of read-only FSRS/progress backend calls. | `collection_service.py:33,28,78-84`; aux `:101-114`; allow-list `anki_rpc/passthrough.py:22-26` |
| **Sync** | **NO.** Anki's sync protocol is never invoked; the AnkiConnect `sync` action is stubbed to raise. This collection is the sole canonical store. | grep = 0 hits; `ankiconnect/actions/meta.py:60-62` (`raise`) |
| **Undo** | **Global** — one in-memory undo stack on the single shared Collection; zero per-session/per-connection isolation. | reviewer `screens/reviewer.py:388-398`; `guiUndo` `ankiconnect/actions/gui.py:141-149` |
| **Caches across requests** | Warm singleton Collection (deck/notetype/queue caches) + one `ReviewerSession` (in-flight card/states) + one `BridgeHub.ui_state`. **No reload hook** (`reloadCollection` is a no-op). A 2nd writer makes these stale. | `collection_service.py:34`; `screens/reviewer.py:32-44`; `bridge/ui_state.py:5-18`; no-op `ankiconnect/actions/meta.py:37-41` |
| **`db_scalar_with_args`** | **No ankiweb path calls it.** ankiweb's 3 scalar-with-args sites route through pylib `db_query` (ported). The fork's `db_scalar_with_args` has **zero production callers** — its only use is an apkg-import test helper. | `pg/dbproxy.rs:252-258` (`todo!`); sole caller `import_export/.../apkg/import/notes.rs:701` inside `#[cfg(test)]` (`:621`) |

**Sequencing recommendation (one line):** **Do M5 first** — it ships a low-risk, end-to-end-testable single-process ankiweb on the already-equivalence-proven PG backend, and is a hard prerequisite for even *exercising* M4 (you cannot launch a 2nd process against PG until ankiweb can point at PG at all); then M4 hardens that running system for multi-process.

**Top 3 open decisions for the user** (code alone can't answer):

1. **Multi-process, or one process on PG?** The whole rewrite's premise is "concurrent multi-process writes," but ankiweb today is one process and the project is explicitly single-user. If the real goal is "the single user can work several decks at once," a **single process on PG with the in-process `asyncio.Lock` relaxed to a small connection pool** may satisfy it — which makes M4's hardest piece (cross-process cache coherence via `LISTEN/NOTIFY`) **unnecessary**. Decide the deployment shape; it determines half of M4's scope.
2. **Undo under concurrency.** Is **per-process/per-session undo** (you can only undo ops from your own session, with a staleness guard that refuses if the target row changed) acceptable, or must undo be globally coherent across all processes? Given single-user, per-process-with-guard is likely fine — and if decision #1 is single-process, undo is unchanged from today.
3. **Daily-limit exactness vs. throughput.** Per-deck `new_studied`/`review_studied` "done today" counters are **stored** and read-modify-written up the **entire parent chain** on every answer (`decks/stats.rs:22-41`), so every concurrent answer contends on the shared ancestor/root deck rows. Pick: (a) atomic in-SQL increment (correct; root row becomes a trivial serialization point — fine for one user), (b) drop the stored counters and derive "done today" from `revlog` at queue-build (lock-free, but a core-scheduler behavior/perf change), or (c) accept bounded daily-limit drift.

---

## Part 1 — How ankiweb actually uses the collection

### 1.1 Lifecycle & process model — strictly single-writer, single-process

`python -m ankiweb` is `asyncio.run(_serve())` (`__main__.py:43-44`) — **one** OS process, one event loop. `_serve()` creates exactly one service and shares it:

```python
service = CollectionService(settings)        # __main__.py:16
await service.open()                          # __main__.py:17
web = create_app(settings, service=service, hub=hub, …)              # :20
api = create_ankiconnect_app(settings, service=service, …)           # :23
await asyncio.gather(web_server.serve(), api_server.serve())          # :33
```

Both apps receive a non-None `service`, so their lifespans set `owns=False` and neither opens its own (`app.py:44,49-51,63-64`; AnkiConnect app analogously). The web server (:8000) and AnkiConnect server (:8765) therefore share **the exact same `Collection`**. The collection is a process-wide singleton (`collection_service.py:34`), opened once via `Collection(str(path), server=False)` (`:47`), closed at shutdown (`:74`, `__main__.py:40`). There is **no** uvicorn `workers=` / gunicorn anywhere.

### 1.2 Concurrency control — `asyncio.Lock` + single worker thread

Every collection touch funnels through `run()`:

```python
async def run(self, fn):
    async with self._lock:                                  # collection_service.py:79
        ...
        return await loop.run_in_executor(self._executor, lambda: fn(col))   # :84  (max_workers=1)
```

`run_op()` wraps `run()` and broadcasts OpChanges after the lock releases (`:86-94`); `backend_raw()` wraps `run()` (`:96-99`). The only lock-free path is `backend_raw_concurrent()` (`:101-114`) on the 4-thread aux pool, restricted to an explicit allow-list — `latest_progress`, `set_wants_abort`, and the FSRS compute/simulate family (`anki_rpc/passthrough.py:22-26`) — which the docstring notes are thread-safe and don't mutate Python-side collection state. The serialization exists because "pylib objects are not thread-safe" (`collection_service.py:23-24`); without it, concurrent writes would race the in-memory caches, the single undo manager, and the in-flight `ReviewerSession` (test `tests/test_collection_service.py:30` fires 20 concurrent adds to prove serialization).

**Implication:** ankiweb today is the textbook single-writer the rewrite targets. For **one** process, plain `BEGIN` + one lock + one worker is correct and safe — so swapping SQLite→PG (M5) needs **no** single-writer removal. M4 only matters once there are 2+ processes.

### 1.3 Sync — never invoked → USN is dead weight

A whole-worktree grep for `full_upload|full_download|sync_collection|sync_login|SyncAuth|normal_sync|before_upload|media_sync|abort_sync` returns **zero** real call sites. The AnkiConnect `sync` action is a deliberate stub: `raise Exception("sync is not supported by ankiweb")` (`ankiconnect/actions/meta.py:60-62`); `reloadCollection` is a no-op (`:37-41`). All other `sync`-substring hits are the local deck-notifier's "resync baseline" (`notifier.py`) or UI prose. **Conclusion: this collection is the sole canonical store; Anki's USN exists only for sync and is inert here.**

### 1.4 Undo — one global in-memory stack, no session scoping

The reviewer undo command runs `await service.run_op(lambda col: col.undo(), …)` (`screens/reviewer.py:388-398`, bound to a button `:133` and `u`/`U` `:216`); `guiUndo` checks `col.undo_status().undo` then `col.undo()` (`ankiconnect/actions/gui.py:141-149`). Both operate on the single shared `col`. There is exactly one `ReviewerSession` for the whole process (`screens/routes.py:287` via the single `register_screen_handlers`, `app.py:59`), and it holds card state only — it does not scope undo. Writes mix recorded undo (`reviewer.py:313` answer/mark) and `skip_undo_entry=True` (`editor.py:33,141`, `ankiconnect/actions/notes.py:135`). **Undo is global: whatever the shared Collection's `UndoManager` last recorded, regardless of which web session triggered it.**

### 1.5 Caches / state across requests — stale under a 2nd writer, with no reload hook

| Cross-request state | Where | Stale under a 2nd-process write? |
|---|---|---|
| Warm singleton `Collection` (its `decks`/`models`/`sched` caches) | `collection_service.py:34` | **Yes** — and `reloadCollection` is a no-op (`meta.py:37-41`), so **no refresh hook exists** today. Central staleness risk. |
| One `ReviewerSession` (`card`, `states`, typed-answer) | `screens/reviewer.py:32-44` | **Yes** — if another process answers/edits/deletes that card, the in-flight `Card`/states are wrong. |
| One `BridgeHub.ui_state` (matched/selected card/note ids, current card) | `bridge/ui_state.py:5-18` | **Partly** — cached ids can point at rows another process deleted; `changeNotetype` reads `selected_note_ids` (`anki_rpc/handlers.py:50`). |
| `NotifierState` baseline | `notifier.py:78,176` | Benign — designed to re-push on change. |

### 1.6 Raw-SQL surface — confirms `docs/pg-rewrite/ankiweb-raw-sql.md`

11 sites across 5 files, all on the serialized worker, all routing through pylib `DBProxy` → `db_query` (scalar/all/list/execute) or `db_execute_many` (`dbproxy.py:57-66,86-105`). The doc's table is accurate. Notable:

- **3 scalar-with-args** (`decks.py:42`, `stats.py:17`, `stats.py:77`) — these go through `db_query`/`db_query_row` (**ported** on PG: `pg/dbproxy.rs:191,210`), **not** the rslib-internal `db_scalar_with_args`.
- **2 raw writes** bypass the op/OpChanges/undo machinery: `relearnCards` UPDATE (`cards.py:197`, via `execute`→`db_query`) and `insertReviews` INSERT (`stats.py:89`, via `db_execute_many`). Both touch disjoint rows (specific card ids / append-only revlog) → MVCC-safe.
- **SQLite-isms** to translate (already catalogued): `count()`→`count(*)`, `date(…,'unixepoch','localtime')` rewrite (`stats.py:27`), `"left"`/`"lastIvl"` quoting, `?`→`$n`.

**`db_scalar_with_args` (the one dbproxy method left `todo!()`):** its sole caller in all of rslib is `note_id_for_guid` at `import_export/package/apkg/import/notes.rs:699-703`, which sits inside `#[cfg(test)] mod test` (starts `:621`). The **production** apkg-import path uses `note_guid_map()` (`:338`) instead. So the PG `todo!()` (`pg/dbproxy.rs:257`) is unreachable in production, unreachable in the PG suite (the `import_export::` test module is harness-routed to SQLite), and **never reached by ankiweb**. Resolution: implement trivially for completeness (reuse the `db_scalar` FromSql bridge + `?`→`$1`) or leave it — lowest priority.

---

## Part 2 — Single-writer machinery remaining in the fork

`CollectionState` is the per-process state hub (`collection/mod.rs:159-174`):

```rust
pub struct CollectionState {
    pub(crate) undo: UndoManager,                                 // :161  global in-mem undo
    pub(crate) notetype_cache: HashMap<NotetypeId, Arc<Notetype>>,// :162
    pub(crate) deck_cache: HashMap<DeckId, Arc<Deck>>,            // :163
    pub(crate) scheduler_info: Option<SchedulerInfo>,            // :164
    pub(crate) card_queues: Option<CardQueues>,                  // :165  v3 queue
    pub(crate) active_browser_columns: …,                        // :166  per-session UI
    pub(crate) modified_by_dbproxy: bool, …
}
```
Note: there is **no config cache and no tag cache** — config and tags read through to storage each time, so they stay live (smaller coherence surface than the spec assumed).

### A. Transaction model — single-writer lock already gone on PG, nothing in its place
`transact_inner` (`collection/transact.rs:8-55`) opens `SAVEPOINT "rust"`, runs the op, bumps mtime + `RELEASE`, else `ROLLBACK`. SQLite enforced single-writer via `locking_mode = exclusive` (`storage/sqlite.rs:53`) and `begin exclusive` (`:473`). The PG port replaced these with plain statements and tracks depth by hand (`storage/pg/mod.rs:250-295`): `begin_rust_trx` = `BEGIN; SAVEPOINT rust`, `begin_trx` = plain `BEGIN`. The `begin_trx` comment refers to a "connection/advisory-lock layer" that **does not exist** (`:290-291`). **There is zero row locking** anywhere — no `SELECT … FOR UPDATE`, no `pg_advisory_lock` (grep confirms; `schema.sql:12` "advisory-locked monotonic clamp" is an aspirational comment). So concurrency control on PG is currently *absent*, not merely relaxed.

### B. USN / sync — load-bearing only for sync
`Collection::usn()` (`collection/mod.rs:215-219`) → `storage.usn(server)`; for ankiweb's `server=false` it returns `Usn(-1)` (`storage/pg/sync.rs:16-18`), the "pending sync" sentinel, stamped onto every write (`card/mod.rs:340`, `notes/mod.rs:137,226`, deck/tag ops…). The `col.usn` counter is mutated **only** by sync: `increment_usn` callers are `before_upload` (`collection/mod.rs:232`) + `sync/collection/{finish,download}.rs`; `set_usn` only `finish.rs:26`. `before_upload` (`collection/mod.rs:222-238`) — the only thing that clears the pending USNs — runs solely from `sync/collection/upload.rs:43`. The two deferred PG methods `objects_pending_sync<T: FromSql>` / `maybe_update_object_usns<I: ToSql>` (`storage/pg/sync.rs:44,46`, `rusqlite` bounds) are called **exclusively** from `sync/collection/{changes,chunks}.rs`. **Nothing outside sync depends on USN.**

### C. Global undo — one in-memory stack on the single Collection
`UndoManager` (`undo/mod.rs:58-68`): `undo_steps: VecDeque<UndoableOp>` capped at 30 (`:15`), `redo_steps`, `current_step`. Lives at `CollectionState.undo` (`collection/mod.rs:161`), **in-memory only, never persisted**. `transact_inner` drives `begin_step`/`save_undo`/`end_step` (`undo/mod.rs:77-107,244-246`); `undo()` replays *logical inverses* inside a fresh `transact()` (`:285-304`, `for change in …rev() { change.undo(col)? }`). A second process has an independent empty stack and cannot see this one's steps; an undo replays the inverse against whatever the DB currently holds, with **no detection** if another process changed those rows meanwhile.

### D. In-memory caches — invalidated only by *this* process's writes
Populate/invalidate sites (all local):
- `deck_cache` — populate `decks/mod.rs:175-180`; clear `collection/mod.rs:241` (`clear_caches`), `decks/undo.rs:38,57,71,83`.
- `notetype_cache` — populate `notetype/mod.rs:208-213`; clear `collection/mod.rs:242`, `notetype/mod.rs:778`, `notetype/undo.rs`.
- `scheduler_info` — populate `scheduler/mod.rs:35-47`; self-expires at `next_day_at`; cleared on config change `config/mod.rs:165,181,191,227`.
- `card_queues` — populate `scheduler/queue/mod.rs:246-254`; clear `clear_study_queues` (`:207-209`) + `maybe_clear_study_queues_after_op` (`transact.rs:31`) + `decks/current.rs:34`.

**Every** invalidation hook fires only on this process's own `transact`/`undo`. A second process committing a write triggers **none** of them → `deck_cache`/`notetype_cache` hand out stale `Arc`s; `card_queues` goes stale (see E). The hooks exist (`clear_caches`, `clear_study_queues`); M4 just needs to *trigger* them on remote writes.

### E. Card queues / limits — **the spec's "computed-not-stored" claim is only half-true**
*Which* cards are due **is** recomputed live every build — `gather_*` re-queries `for_each_due_card_in_active_decks` (`scheduler/queue/builder/gathering.rs:39`) and decrements an ephemeral in-memory `LimitTreeMap` (`decks/limits.rs:218-398`) that is discarded after build. **Concurrency-safe to read.** ✔

**But the daily-progress accounting is stored mutable state.** `new_for_normal_deck_v3` subtracts stored counters from the cap (`decks/limits.rs:98-105`): `review_limit -= review_today_count` where `(…, review_today_count) = deck.new_rev_counts(today)`. Answering writes those counters via `update_deck_stats` (`decks/stats.rs:22-41`):

```rust
let mutator = |c: &mut DeckCommon| { c.new_studied += …; c.review_studied += …; };  // :29-33
if let Some(mut deck) = self.storage.get_deck(did)? {
    self.update_deck_stats_single(today, usn, &mut deck, mutator)?;                  // :35  read-modify-write
    for mut deck in self.storage.parent_decks(&deck)? {                             // :36  …AND every parent
        self.update_deck_stats_single(today, usn, &mut deck, mutator)?; } }
```
`update_deck_stats_single` (`:74-89`) is a `get → mutator → update_single_deck_undoable` read-modify-write. Since the **root deck is an ancestor of every deck**, every concurrent answer read-modify-writes the shared ancestor/root rows → **lost update** under naive MVCC: two answers read `studied=N`, both write `N+1`, one increment lost → the daily limit drifts and the user over/under-studies. This is a **real shared-mutable hot-spot**, not the spec's "benign overshoot."

**Stale-queue self-detection:** `get_queued_cards` asserts each cached entry's mtime still matches the card (`scheduler/queue/mod.rs:111-117`): `require!(card.mtime == entry.mtime(), "bug: card modified without updating queue …")`. So a stale queue does not silently corrupt — it **hard-errors**. Under naive multi-process, process A reviewing while process B edits a queued card → A throws this error mid-review.

### F. Open/close — SQLite had a file lock, PG has nothing
SQLite: `busy_timeout(0)` + `locking_mode = exclusive` (`storage/sqlite.rs:51-53`) → a 2nd opener gets `SQLITE_BUSY`; this was the *de-facto* single-writer enforcement. PG: `PgStorage::open` just `postgres::Client::connect` (`storage/pg/mod.rs:114-124`); `open_or_create` (`:161-170`) has **no lock / advisory lock / single-opener check**. Any number of processes can connect concurrently. The `RefCell` design (`:50-60`) serializes threads *within* one process only.

### G. ID generation — `max(id)+1` races on PG
The collision trick (ms-timestamp id; bump on collision) is preserved verbatim, e.g. `pg/card.rs:181-182`: `(CASE WHEN $1 IN (SELECT id FROM cards) THEN (SELECT max(id)+1 FROM cards) ELSE $1 END)` — same at `pg/note.rs:81-82`, `pg/deck.rs:132-133`, `pg/notetype.rs:177-178`, `pg/deckconfig.rs:36-37`, `pg/revlog.rs:70-71`. Under SQLite's exclusive lock this read-then-insert was serialized; on concurrent PG two inserts can read the same `max(id)` → PK collision (one fails). The intended "advisory-locked monotonic clamp" is unimplemented.

### H. Write hot-paths — disjoint vs. shared
1. **Answer a card** (`scheduler/answering/mod.rs:314-387`): revlog INSERT (`:335`, disjoint row but id race [ID]) + **`update_deck_stats` deck+parents read-modify-write** (`:337` → [QUEUE], the lost-update hot-spot) + sibling bury (`:338`, disjoint per note) + answered card UPDATE (`:353`, disjoint). **Hot-spot = the deck-stats rows + the revlog id race.**
2. **Deck create/rename/reparent** (`decks/addupdate.rs:27`, `decks/reparent.rs:17-61`): per-deck read-modify-write, **renames all child decks**, enforces a unique name. The deck tree is shared structure keyed by name strings + unique-name index → overlapping reparents/renames race.
3. **Notetype edit** (`notetype/mod.rs:756-780`): notetype row UPDATE then **regenerates cards across all notes of that type** — shared row + a large write set overlapping other processes' queues.
4. **Tag bulk add/remove** (`tags/bulkadd.rs`, `tags/register.rs:95`): note rows disjoint per nid; the `tags` registry table is shared (upsert mostly idempotent, but USN stamping + `clear_unused_tags` race).

---

## Part 3 — M4 design (remove single-writer for multi-process PG)

> Scope note: items 4.1–4.3 are **trivially safe** and unblock-able (can land during M5). Items 4.4–4.7 are the substantive concurrency work and are **only needed if decision #1 chooses true multi-process.**

### 4.1 USN → remove (sync-only; already inert)
ankiweb never syncs (§1.3) and `server=false` makes every write stamp `Usn(-1)` (§B). Plan:
- Stop computing/stamping USN on writes (or hard-wire `Usn(0)`); the `usn` columns can stay (harmless default) — no schema change required.
- The **2 deferred sync methods** (`objects_pending_sync`/`maybe_update_object_usns`) and their callers live entirely in `sync/collection/` (§B). Resolution: feature-gate the `sync/collection/` subsystem off for the PG build (or delete it). Then the PG `todo!()`s at `pg/sync.rs:44,46` are unreachable by construction — leave them as `unreachable!()`/`todo!()`. `before_upload`, `increment_usn`, `set_usn`, and the `clear_*_usns` family become dead and can be removed with the sync module.
- This is the lowest-risk M4 item; it can land during M5 since it changes nothing observable for ankiweb.

### 4.2 `db_scalar_with_args` → trivial or leave (test-only; §1.6)
Zero production/ankiweb callers. Either implement via the existing `db_scalar` FromSql bridge + `?`→`$1`, or leave `todo!()`. Not on any critical path.

### 4.3 Undo → per-process, with a staleness guard (or disable)
The single in-memory stack is already per-process (each process has its own `CollectionState.undo`). Decision #2 governs:
- **Recommended (single-user):** keep undo **per-process/per-session**; before replaying an inverse, check the target rows' `mtime`/version against what the undo step captured and **refuse gracefully** (no-op + UI notice) if another process moved them — rather than silently corrupt (§C). The `UndoableChange::undo` path (`undo/mod.rs:285-304`) is the natural place to add the guard.
- If decision #1 is **single-process**, undo is unchanged from today (one stack shared by all web sessions — already the status quo, acceptable for one user).

### 4.4 Cache coherence — the genuinely novel piece (multi-process only)
The coherence surface is just **4 caches**: `deck_cache`, `notetype_cache`, `scheduler_info`, `card_queues` (§D; no config/tag cache exists). Strategy, cheapest-first:
- **`card_queues`** → **rebuild per request / on mtime-mismatch.** It is already cheap to rebuild and already self-detects staleness by erroring (`scheduler/queue/mod.rs:111-117`). M4 change: convert that `require!` into "drop the cached queue and rebuild" instead of erroring, so a remote edit to a queued card triggers a transparent rebuild.
- **Metadata** (`deck_cache`, `notetype_cache`, `scheduler_info`) → invalidate on remote writes via a **version-stamp / epoch check** as the primary mechanism, with **`LISTEN/NOTIFY` as an optimization**:
  - *Epoch (primary, robust):* a `meta_epoch(kind text PRIMARY KEY, epoch bigint)` table bumped in the writer's transaction; each process records the epoch it last loaded and re-checks it cheaply at op start (one indexed read), calling the existing `clear_caches()` (`collection/mod.rs:240-242`) when stale. Survives missed notifications and needs no extra connection.
  - *`LISTEN/NOTIFY` (optimization):* a writer `NOTIFY`s a per-kind channel on commit; listeners drop the affected entry proactively. **Constraint (verified):** `PgStorage` uses a single **synchronous** `postgres::Client` (`storage/pg/mod.rs:62,115`, `NoTls`) used under the asyncio lock, so it cannot also block on `LISTEN`. NOTIFY therefore needs a **dedicated listener connection + background thread per process**. Given that cost, **epoch-check is the recommended primary**, with NOTIFY added only if epoch-poll latency proves insufficient.
- No `LISTEN/NOTIFY` plumbing exists yet (grep = 0) — M4 builds it from scratch.

### 4.5 Write-conflict handling — row locks + atomic increments + retry (multi-process only)
PG MVCC + targeted locking, by operation:
- **Answer a card** — the **deck-stats lost-update** (§E/§H) is the one place that needs care. **Recommended fix:** replace the app-side read-modify-write in `update_deck_stats` (`decks/stats.rs:29-38`) with an **atomic in-SQL increment** (`UPDATE decks SET new_studied = new_studied + $d, review_studied = review_studied + $r WHERE id = ANY($ancestors)`), which MVCC serializes correctly with no lost update. The root deck row becomes a serialization point for *all* answers — negligible for a single user. (Principled alternative per decision #3: drop the stored counters, derive "done today" from `revlog` at build — lock-free but a core-scheduler change.) Card/revlog/sibling writes are otherwise disjoint.
- **Structural ops** (deck tree, notetype edit, deck config) — take `SELECT … FOR UPDATE` on the affected metadata rows (and for reparent/rename, lock the subtree's rows) so concurrent structural edits serialize. These are rare and low-contention.
- **Tag bulk ops** — note rows are disjoint; guard the shared `tags` registry with `ON CONFLICT` upserts (already used) + lock during `clear_unused_tags`.
- **Serialization-failure retry:** wrap each op (the `transact()` boundary, `collection/transact.rs`) in a bounded retry on PG `40001`/`40P01`; safe because ankiweb ops are already funneled and idempotent at the request layer.

### 4.6 ID allocation — make `max(id)+1` concurrency-safe (multi-process only)
Replace the racy read-then-insert (§G) with one of: (a) a short **`pg_advisory_xact_lock(table_oid)`** around id assignment computing `id = max(now_ms, last+1)` (cheapest, preserves ms-id semantics + apkg-export compatibility), or (b) a per-table sequence clamped to `max(now_ms, nextval)`. Decision recorded under open items.

### 4.7 Daily-limit accounting — see §E and decision #3
The only computed-not-stored guarantee that *holds* is "which cards are due." The "how many done today" counters are the exception and are handled in 4.5. Flagging explicitly so it is not assumed safe.

---

## Part 4 — M5 design (ankiweb on PG: DSN + data dir + import/export boundary)

### 5.1 Open-by-DSN wiring (rslib + pylib)
Today the **production** open path always builds SQLite — `open_collection` (`backend/collection.rs:17-27`) calls `CollectionBuilder::new(input.collection_path)` and never `set_pg_dsn`; only `CollectionBuilder::build()` routes to PG when `pg_dsn` is set or (test-only) the env switch fires (`collection/mod.rs:68-89`). Minimal change:
- **rslib:** in `open_collection` (`backend/collection.rs:20`), scheme-detect `postgresql://`/`postgres://` in `collection_path` → `builder.set_pg_dsn(path)`. No proto change. When a DSN is used, `build()` ignores `collection_path` for storage (`collection/mod.rs:68`) but still needs explicit media paths (next bullet).
- **pylib:** `Collection.__init__` does `os.path.abspath(path)` (`collection.py:151`) and `reopen()` derives media via `media_paths_from_col_path` = regex-replace `.anki2`→`.media` (`collection.py:289`, `media.py:24`). A raw DSN would be mangled by both. M5 makes the open path DSN-aware: when `path` is a DSN, skip `abspath` and take an **explicit absolute media dir** (a new `__init__`/`reopen` param) instead of deriving it from the path.
- **ankiweb:** `CollectionService.open()` passes the DSN where it passes the file path today (`collection_service.py:47`), plus the absolute data dir. `Settings` grows `ANKIWEB_PG_DSN` + an absolute `data_dir` (generalize the `collection_path.parent`-derived dirs in `config.py:46-58`); relative media paths stored in the DB, base dir at startup (per the spec).

### 5.2 Import/export boundary — already structurally correct
`.apkg`/`.colpkg` are SQLite files; the fork already opens them as SQLite by construction:
- **Import** extracts the package to a tempfile and opens it with `CollectionBuilder::new(tempfile.path()).build()` (`apkg/import/mod.rs:134-135`) — a plain path ⇒ `pg_dsn=None` ⇒ **SQLiteStorage**. The target is the live (PG) collection; rows copy source→target via the ported PG methods.
- **Export** materializes into a temp SQLite collection (`apkg/export.rs:99`, `colpkg/export.rs:186` `dummy_col`) then zips. Source = live PG, sink = temp SQLite package.

So **no new SQLite path is needed** — the file boundary already yields SQLite regardless of the live backend. M5 work is: (1) the DSN wiring (5.1) so the live side is PG, and (2) **verification/integration tests** of the SQLite-package ↔ PG-live copy in both directions — currently **unproven**, because rslib's `import_export::` tests are harness-routed to SQLite on both sides (STATUS B5a). This is the substantive M5 risk to retire.

### 5.3 Raw-SQL translations (ankiweb's 11 sites)
Per `docs/pg-rewrite/ankiweb-raw-sql.md`: `?`→`$n`, `count()`→`count(*)`, `"unixepoch"/"localtime"` rewrite of `date(…)` (`stats.py:27`), `"left"`/`"lastIvl"` quoting. These land in `pg/dbproxy.rs` `db_query`/`db_execute_many` (already ported); only `db_scalar_with_args` is `todo!()` and is ankiweb-unreachable (§1.6).

### 5.4 Secondary M5 notes
- **Media DB:** `MediaManager` opens a **separate SQLite** `.media.db` (`media/mod.rs:34,44-55`) regardless of the collection backend; its purpose is media *sync* tracking (`crate::sync::media::database`). Under multi-process it would be a shared-SQLite contention point — but since ankiweb never syncs, it is largely vestigial. Keep it **per-process** (each process its own media.db) or stub it; media *files* on disk are content-addressed and filesystem-safe. Flag for the multi-process decision.
- **Notifier:** the background `DeckNotifier` (`__main__.py:30-31`) runs one-per-process; under multi-process, N notifiers would duplicate pushes — an M6 deployment detail.

---

## Part 5 — M4-vs-M5 sequencing: recommend **M5 first**

The plan lists M4 before M5. The evidence argues the reverse:

1. **M5 is independently testable end-to-end; M4 is not.** ankiweb is one process today (§1.1). M5 (DSN open + import/export verification) yields a fully-functional ankiweb-on-PG under the **existing** single-writer topology — runnable against ankiweb's ~284-test suite — on a backend already proven equivalent (332/0/6). For one process, plain `BEGIN` + one `asyncio.Lock` + one worker is correct; **no single-writer removal is required to get ankiweb green on PG.**
2. **M4 has a hard dependency on M5.** M4's concurrency tests require launching 2+ processes against PG, which is impossible until ankiweb can point at PG at all — i.e. until M5's DSN wiring exists. Doing M4 first produces nothing runnable.
3. **Risk profile.** M4 is the highest-risk work and **none of it is implemented**: no locks, no `FOR UPDATE`, no advisory locks, no `LISTEN/NOTIFY`; the id-race, the deck-counter lost-update, and the queue mtime hard-error are all live (Part 2). Doing M4 first destabilizes a proven-green base before the app even runs. M5-first **banks a shippable single-process ankiweb-on-PG**, then M4 hardens a *running* system with a real app to drive concurrency tests (folding in M6's multi-process validation).
4. **Cheap M4 items can ride along.** USN removal (4.1) and the `db_scalar_with_args` decision (4.2) are inert for ankiweb and can land opportunistically during M5. The substantive M4 (4.4–4.6) follows, and its very necessity depends on decision #1.

**Recommendation:** M5 → (then, if multi-process is chosen) M4 → M6. If decision #1 is single-process-on-PG, M4 collapses to just 4.1–4.3 + relaxing ankiweb's `asyncio.Lock` to a connection pool, and the LISTEN/NOTIFY subsystem is dropped entirely.

---

## Part 6 — Open decisions for the user (full list)

1. **(Top) Deployment shape:** true multi-process/multi-container, or one process on PG with the in-process lock relaxed to a pool? Determines whether 4.4 (cache coherence) and most of 4.5–4.6 are needed at all.
2. **(Top) Undo semantics:** per-process/per-session with a staleness guard (recommended for single-user), or globally coherent across processes? (Moot if single-process.)
3. **(Top) Daily-limit accounting:** atomic in-SQL increment (4.5a, recommended), drop-and-derive-from-revlog (4.5b), or accept bounded drift (4.5c)?
4. **ID allocation:** advisory-xact-lock + ms-clamp (recommended) vs. clamped sequence vs. keep ms-id with PK-collision retry.
5. **USN columns:** drop the columns, or keep them at a constant default (recommended — zero schema churn)?
6. **Media DB under multi-process:** per-process `.media.db`, or stub it (sync-only, vestigial here)?
7. **`db_scalar_with_args`:** implement trivially, or leave `todo!()` (ankiweb-unreachable)?
