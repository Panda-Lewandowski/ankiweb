# Search Backend Port Plan — SQLite → PostgreSQL (`PgStorage`)

Read-only analysis of fork `/mnt/sda/git/tools/anki-pg-fork` (branch `pg-fork`, tag 25.09.4).
All `file:line` cites are against that checkout. Companion to `docs/pg-rewrite/storage-surface.md`,
`docs/pg-rewrite/ankiweb-raw-sql.md`, `docs/pg-rewrite/pg-schema.sql` (in the fork), and the
worktree's `docs/superpowers/STATUS-pg-rewrite.md` (UNICASE ORDERING HAZARD).

This document is the execution plan for porting the **search backend** — the path that turns a
parsed search into SQL, runs it, and materializes matching card/note ids. An implementer with zero
prior context should be able to port it from here.

---

## TL;DR — the single most important finding

**All 11 pgrx-extension function signatures match the generated SQL exactly** — arity AND argument
order, including the two intentional quirks: `extract_fsrs_retrievability`'s arg-4 `next_day_at` is
**unused** in the body (the closure reads args 0,1,2,3,5; skips 4 — `sqlite.rs:282`,
`sql_functions.rs:141-168`), and `extract_fsrs_relative_retrievability` takes
`(data, due, days_elapsed, ivl, next_day_at, now)` — i.e. `days_elapsed` and `ivl` are **swapped**
relative to `extract_fsrs_retrievability` (`sqlite.rs:301-336`, `sql_functions.rs:180-232`), and the
call site emits them in that swapped order (`card/mod.rs:840`, `sqlwriter.rs` does not call relative).
No ext-signature change is required.

**The ONE mismatch class** is `regexp`: the writer emits it as the SQLite **infix operator**
(`expr regexp ?`) at **5 sites**, and PostgreSQL has no `regexp` operator. SQLite's `X REGEXP Y`
desugars to the function call `regexp(Y, X)` (pattern first, value second), and SQLite **also**
registers `regexp` as a plain 2-arg function (`sqlite.rs:131`), so the function-call form
`regexp(?, expr)` is valid on **both** backends. **Fix: change the writer to emit the function form.**
Sites: `sqlwriter.rs:314` (tag), `:514-516` (deck, ×2), `:545` (template), `:562` (notetype),
`:913` & `:926` (regex). This is the headline porting action; everything else is mechanical.

---

## 1. Search pipeline map

### 1a. The two entry shapes

There are two ways search results are consumed, and they take different storage paths:

**(A) Return ids directly** — `Collection::search_cards` / `search_notes` / `search_notes_unordered`
(`search/mod.rs:158-178`) → `Collection::search<T>` (`search/mod.rs:181-196`):

```
search<T>()                                     search/mod.rs:181
 ├─ T::as_return_item_type()                     -> ReturnItemType::{Cards,Notes}
 ├─ search.try_into_search()                     -> Node                (parser)
 ├─ SqlWriter::new(self, item_type)              sqlwriter.rs:48
 ├─ writer.build_query(&top_node, mode.required_table())   sqlwriter.rs:62
 │     ├─ write_table_sql()                      sqlwriter.rs:73   ("select c.id from cards c ... where ")
 │     └─ write_node_to_sql(node)                sqlwriter.rs:98   (recursive; pushes args)
 │     => (sql: String, args: Vec<String>)
 ├─ self.add_order(&mut sql, item_type, mode)    search/mod.rs:198 (appends " order by ...")
 │     ├─ prepare_sort(...)                       search/mod.rs:409 (builds `sort_order` temp table via execute_batch)
 │     └─ write_order(...)                        search/mod.rs:325 (the ORDER BY clause text)
 └─ self.storage.query_ids(&sql, &args)          search/mod.rs:193  ==> Vec<CardId|NoteId>
```

`query_ids` runs the assembled `SELECT c.id FROM ... WHERE ... ORDER BY ...` and reads column 0 as
the id (`SqliteStorage::query_ids` `sqlite.rs:560-575`; **`PgStorage::query_ids` already exists,
partial** `pg/mod.rs:1299-1318`).

**(B) Materialize into a temp table** — used when the caller needs full `Card`/`Note` rows, to iterate,
or to feed downstream SQL. `Collection::search_cards_into_table` (`search/mod.rs:223-248`) and
`search_notes_into_table` (`search/mod.rs:295-313`):

```
search_cards_into_table(search, mode)           search/mod.rs:223
 ├─ build_query + add_order                       (same as above)
 ├─ storage.setup_searched_cards_table[_to_preserve_order]()  search/mod.rs:237/239
 ├─ sql = format!("insert into search_cids {sql}")            search/mod.rs:241
 ├─ storage.execute_raw(&sql, params_from_iter(args))         search/mod.rs:245
 └─ returns CardTableGuard { cards, col }         (Drop => clear_searched_cards_table, mod.rs:136-142)
```

Downstream readers of the temp table:
- `all_cards_for_search` → `all_searched_cards` (`search/mod.rs:250-253`)
- `all_cards_for_search_in_order` → `all_searched_cards_in_search_order` (`:255-262`)
- `all_cards_for_ids` → `with_searched_cards_table` + `set_search_table_to_card_ids` (`:264-277`)
- `for_each_card_in_search` (`:279-289`)
- notes: `all_searched_notes`, `for_each_note_in_search`, `with_ids_in_searched_notes_table`,
  `for_each_note_tag_in_searched_notes`, `get_note_tags_by_id_list`
- `search_cards_of_notes_into_table` — copy cids of nids in `search_nids` into `search_cids`
  (`search/mod.rs:317-321`, `card/mod.rs:585-590`).

### 1b. Every `SqliteStorage` method on the search path (exact names + signatures + port status)

`✓` = already ported in `pg/mod.rs`. `TODO` = `todo!()` stub in `pg/mod.rs` (line cited).
`PARTIAL` = present but only handles function-free SQL.

| Method (`impl SqliteStorage`) | SQLite defn | PG status |
|---|---|---|
| `query_ids<T: FromSql>(&self, sql, args: &[String]) -> Result<Vec<T>>` | `sqlite.rs:560` | **PARTIAL** `pg/mod.rs:1299` (function-free only; placeholders OK) |
| `execute_raw(&self, sql, params) -> Result<usize>` | `sqlite.rs:544` | **TODO** `pg/mod.rs:1848` |
| `execute_batch(&self, sql) -> Result<()>` | `sqlite.rs:536` | **TODO** `pg/mod.rs:1847` |
| `setup_searched_cards_table(&self)` | `card/mod.rs:705` (`search_cids_setup.sql`) | **TODO** `pg/mod.rs:1764` |
| `setup_searched_cards_table_to_preserve_order(&self)` | `card/mod.rs:711` (`search_cids_setup_ordered.sql`) | **TODO** `pg/mod.rs:1765` |
| `clear_searched_cards_table(&self)` | `card/mod.rs:717` (`drop table if exists search_cids`) | **TODO** `pg/mod.rs:1742` |
| `with_searched_cards_table<T>(&self, preserve_order, f)` | `card/mod.rs:552` | **TODO** `pg/mod.rs:1767` |
| `set_search_table_to_card_ids(&self, &[CardId])` | `card/mod.rs:724` (`insert into search_cids values (?)`) | **TODO** `pg/mod.rs:1763` |
| `all_searched_cards(&self) -> Vec<Card>` | `card/mod.rs:592` | **TODO** `pg/mod.rs:1738` |
| `all_searched_cards_in_search_order(&self) -> Vec<Card>` | `card/mod.rs:602` (`order by search_cids.rowid`) | **TODO** `pg/mod.rs:1739` |
| `for_each_card_in_search<F>(&self, f)` | `card/mod.rs:613` | **TODO** `pg/mod.rs:1746` |
| `search_cards_of_notes_into_table(&self) -> usize` | `card/mod.rs:585` (`search_cards_of_notes_into_table.sql`) | **TODO** `pg/mod.rs:1762` |
| `all_cards_at_or_above_position(&self, start) -> Vec<Card>` | `card/mod.rs:696` (`at_or_above_position.sql`) | **TODO** `pg/mod.rs:1735` |
| `setup_searched_notes_table(&self)` | `note/mod.rs:310` (`search_nids_setup.sql`) | **TODO** `pg/mod.rs:1824` |
| `clear_searched_notes_table(&self)` | `note/mod.rs:316` | **TODO** (in the notes block ~`pg/mod.rs:1810`) |
| `with_ids_in_searched_notes_table<T>(&self, &[NoteId], f)` | `note/mod.rs:323` | **TODO** (notes block) |
| `all_searched_notes(&self) -> Vec<Note>` | `note/mod.rs:277` | **TODO** `pg/mod.rs:1807` |
| `for_each_note_in_search(&self, f)` | `note/mod.rs:341` | **TODO** `pg/mod.rs:1812` |
| `for_each_note_tag_in_searched_notes<F>(&self, f)` | `note/mod.rs:262` | **TODO** (notes block) |
| `get_note_tags_by_id_list(&self, &[NoteId])` | `note/mod.rs:250` | **TODO** (notes block) |
| `note_fields_by_checksum(&self, ntid, csum)` (uses `field_at_index`) | `note/mod.rs:170` | **TODO** `pg/mod.rs:1819` |

Already-ported helpers the search readers reuse: `pg_row_to_card` and `pg_row_to_note` decoders
(committed in `f23608a57`), the placeholder rewriter `sqlite_placeholders_to_pg` (`pg/mod.rs:1543`),
and `tx_depth`/`begin/commit_rust_trx`.

### 1c. The ORDER-BY / sort_order sub-path

`add_order` (`search/mod.rs:198`) handles three `SortMode`s:
- `NoOrder` → nothing.
- `Custom(text)` → appended verbatim.
- `Builtin{column, reverse}` → `prepare_sort` (`search/mod.rs:409`) runs one of **12** `*_order.sql`
  files via `execute_batch` to build a `sort_order` temp table, then `write_order`
  (`search/mod.rs:325`) appends an ORDER BY referencing `(select pos from sort_order where ...)`.

`write_order` body: `card_order_from_sort_column` (`search/mod.rs:350-382`) and
`note_order_from_sort_column` (`:384-407`). These embed custom-fn calls and a `collate nocase`
(see §2).

---

## 2. Generated-SQL inventory (SQLite → PG translation, per construct)

Format: **construct — where — SQLite form — PG action.**

### 2a. Custom function call sites — arity/arg-order cross-check vs the ext

The ext signatures (from the task spec; **confirmed against the SQLite registrations** at the cited
lines — same arity, same arg order). All integer args are PG `bigint`.

| Ext signature | SQLite reg (arity) | Generated call site(s) | Match? |
|---|---|---|---|
| `field_at_index(text, bigint)` | `sqlite.rs:85` (2) | `note/mod.rs:176` `field_at_index(flds, 0)` | ✓ |
| `process_text(text, bigint)` | `sqlite.rs:103` (2) | `sqlwriter.rs:233-234` `process_text(cast(n.sfld as text), {bits})`, `process_text(n.flds, {bits})`; `:629`; `:896` | ✓ |
| `fnvhash(VARIADIC bigint[])` | `sqlite.rs:119` (-1) | `card/mod.rs:821` `fnvhash(id, mod)`, `:924-925` `fnvhash(nid,salt)`/`fnvhash(id,salt)`; `card/filtered.rs:45` `fnvhash(c.id, c.mod)` | ✓ (2-arg ⊂ variadic) |
| `regexp(re text, text text)` | `sqlite.rs:131` (2) | **OPERATOR FORM** — see §2b | ⚠ rewrite |
| `regexp_fields(re, flds, VARIADIC bigint[])` | `sqlite.rs:160` (-1) | `sqlwriter.rs:590` `regexp_fields(?n, n.flds)` (no indices = all), `:595`, `:664` `regexp_fields(?n, n.flds, {idx,...})`, `:915` | ✓ (empty variadic = all fields; body `sql_functions.rs:126`) |
| `regexp_tags(re, tags)` | `sqlite.rs:183` (2) | `sqlwriter.rs:303` `regexp_tags(?n, n.tags)` | ✓ |
| `extract_original_position(text)` | `sqlite.rs:205` (1) | `note_original_position_order.sql` `extract_original_position(data)` | ✓ |
| `extract_custom_data(text, text)` | `sqlite.rs:223` (2) | `sqlwriter.rs:404` `cast(extract_custom_data(c.data,'{key}') as float)`, `:411`, `:438` `... is not null` | ✓ (key is an **inlined literal**, not a param) |
| `extract_fsrs_variable(text, text)` | `sqlite.rs:243` (2) | `sqlwriter.rs:415` `('s')`, `:419` `('d')`; `search/mod.rs:372-373`; `card/mod.rs:826-827` | ✓ |
| `extract_fsrs_retrievability(data,due,ivl,days_elapsed,next_day_at,now)` | `sqlite.rs:264` (6) | `sqlwriter.rs:428`; `search/mod.rs:374-379` | ✓ (arg-4 `next_day_at` unused in body) |
| `extract_fsrs_relative_retrievability(data,due,days_elapsed,ivl,next_day_at,now)` | `sqlite.rs:301` (6) | `card/mod.rs:840`; `card/filtered.rs:57` | ✓ (`days_elapsed`/`ivl` swapped vs retrievability — call site matches) |

**Conclusion: no ext-signature edits needed.** When the ext is installed, every function-call-form
site resolves unchanged. Only the `regexp` operator form (§2b) needs rewriting, and a handful of
non-function SQLite-isms (§2c–§2g).

### 2b. `regexp` operator form → function form (THE one real fn mismatch)

PG has **no `regexp` operator**. The 5 operator-form sites:

| Site | Emitted (SQLite) | Required (PG, also valid SQLite) |
|---|---|---|
| `sqlwriter.rs:314` (tag) | `n.tags regexp ?` | `regexp(?, n.tags)` |
| `sqlwriter.rs:514-516` (deck, twice in the OR) | `name regexp ?{n}` | `regexp(?{n}, name)` |
| `sqlwriter.rs:545` (template) | `name regexp ?` | `regexp(?, name)` |
| `sqlwriter.rs:562` (notetype) | `name regexp ?` | `regexp(?, name)` |
| `sqlwriter.rs:913` & `:926` (regex on fields) | `{flds_expr} regexp ?{arg_idx}` | `regexp(?{arg_idx}, {flds_expr})` |

Arg order: SQLite `X REGEXP Y` ≡ `regexp(Y, X)` = `regexp(pattern, value)`, matching the ext's
`regexp(re, text)`. So **pattern first, value second**.

**RECOMMENDED FIX: edit the writer** to emit the function form. Rationale: the function form is valid
on both backends (SQLite registers `regexp/2` at `sqlite.rs:131`), so one SQL string serves both, and
we avoid a fragile textual operator→function rewrite (the left operand can be a nested expression,
e.g. `coalesce(process_text(n.flds,1),n.flds) regexp ?1` at `:913`). The five edits keep the arg-push
order unchanged (still write the placeholder, then push the arg as today). **Update the affected
`sqlwriter.rs` unit-test asserts** (`:1230-1296` deck/template/notetype/tag, `:1374-1395` regex/word
boundary) to the new strings; the behavioral search tests are unaffected.

(Fallback if the writer must stay untouched: a PG-side rewrite, but it must tokenize to find the LHS
operand — not a simple regex. Not recommended.)

### 2c. `==` → `=`

PG does not accept `==`. Two sites:
- `sqlwriter.rs:187` (Flag): `(c.flags & 7) == {flag}`.
- `note_original_position_order.sql`: `WHEN type == 0 THEN due`.

SQLite accepts `=` as a synonym, so **change both to `=` in the writer / sql file** (dual-compatible;
no PG-side rewrite needed). Update the two Flag asserts (`sqlwriter.rs:1259-1260`).

### 2d. `LIKE` case-sensitivity → `ILIKE` (UNICASE-adjacent)

Field/text searches emit `LIKE ... ESCAPE '\'` and rely on **SQLite LIKE being ASCII-case-insensitive**.
PG `LIKE` is case-sensitive. Sites (all in `sqlwriter.rs`):
- `:276`, `:292` unqualified: `{sfld_expr} like ?{n} escape '\'`, `{flds_expr} like ?{n} escape '\'`.
- `:268`, `:701` field-qualified: `... like '{f}' escape '\'` (`{f}` contains `' || ?{n} || '` and `\x1f`/`%`).
- `:629` no-combining field: `coalesce(process_text(n.flds,1),n.flds) like '{f}' escape '\'`.

PG action: emit **`ILIKE`** instead of `LIKE`. `ESCAPE '\'` works with ILIKE.
⚠ Divergence: SQLite folds **ASCII only**; PG `ILIKE` folds **full Unicode**, so it over-matches on
non-ASCII case pairs (`'Ü' ILIKE 'ü'` is true in PG, false in SQLite). See §5. The `nc:`/no-combining
paths strip diacritics to ASCII first (`without_combining`), so they are unaffected; only plain
field/text LIKE on non-ASCII letters can diverge.

This cannot be unified to one keyword (SQLite has no `ILIKE`) → use a **dialect flag** (§2h), not a
textual rewrite (a blind `\blike\b`→`ilike` would corrupt an inlined custom-data literal such as
`extract_custom_data(c.data,'like')`).

### 2e. `COLLATE nocase` → `COLLATE unicase` (sort field)

`write_order` emits `n.sfld collate nocase asc` (`search/mod.rs:369`, `:398`). PG has **no `nocase`
collation**. Map to `COLLATE unicase` (the named collation in `pg-schema.sql:36`). Again per-backend
(SQLite needs `nocase`, PG needs `unicase`) → dialect flag (§2h). See §5 for the deeper `sfld`
numeric-sort divergence (sfld is `integer` in SQLite, `text` in PG).

### 2f. `\x1f` field separator, `||`, `coalesce`, `cast`

- `\x1f` (U+001F) appears as a **literal byte inside single-quoted SQL** (e.g. `'\x1f%'` at
  `sqlwriter.rs:268`, the field-index join at `:267`). PG accepts a raw 0x1F byte in a string literal;
  **no rewrite** (do NOT convert to `E'\x1f'`).
- `||` string concat (`'' || ?{n} || '\x1f%'`) — identical in PG. Precedence (`||` binds tighter than
  `LIKE`/`ILIKE`) is the same. No rewrite.
- `coalesce(...)` (`:233`, `:629`, `:896`; `note_original_position_order.sql`) — identical. No rewrite.
- `cast(n.sfld as text)` (`:233`) — no-op in PG (sfld is already `text`); harmless. `cast(... as float)`
  (`:404`) — PG has `float` (= `double precision`); `extract_custom_data` returns text and the cast
  parses it, same as SQLite. No rewrite.
- No `ifnull` / `instr` / blob-`length` / `glob` in the **search-generated** SQL. **`GLOB` is never
  emitted** — globs are pre-converted to regex in Rust via `to_re`/`to_custom_re`/`is_glob`
  (`text.rs:458-490`) before SQL generation, then flow through the `regexp(...)` function. (One
  `cast(flds as blob)` exists in `note/mod.rs:151` `fix_invalid_utf8_in_note`, but that is dbcheck, not
  search.)

### 2g. `"left"` reserved word + the `get_card` / `get` SELECTs

The temp-table readers concat `get_card.sql` (`card/mod.rs:592-628`) / `get.sql`
(`note/mod.rs:277-356`). `get_card.sql` selects `left` **unquoted** and wraps `cast(mod as integer)`,
`cast(ivl as integer)`. For PG:
- `left` → **`"left"`** (reserved word; the column is already quoted in `pg-schema.sql:86`). The ported
  `PgStorage` card CRUD already uses the correct quoted SELECT — see the `"left"` column list at
  `pg/mod.rs:635` and `:646`. **Reuse that exact column list / `pg_row_to_card`** for the searched-card
  readers rather than translating `get_card.sql`.
- Drop the `cast(... as integer)` wrappers (columns are strict `bigint`; read as i64 and narrow in
  Rust, exactly as `pg_row_to_card` does). Same pattern already used for revlog — see the
  `REVLOG_GET_SQL` note at `pg/mod.rs:1528-1531`.
- `get.sql` `cast(sfld as text)` → no-op (sfld is text); reuse `pg_row_to_note`.

### 2h. Placeholders `?` / `?N` → `$N` + the dialect flag

The writer builds a positional `args: Vec<String>` (`sqlwriter.rs:42`) and emits a **mix** of bare `?`
(auto-numbered: tag `:314`, template `:545`, notetype `:562`) and explicit `?{n}` where
`n = self.args.len()` after the push (field/regex/deck). **`PgStorage::sqlite_placeholders_to_pg`
(`pg/mod.rs:1543-1568`) already handles both correctly**: bare `?` → `$auto` (auto = running max+1,
which matches SQLite's "largest assigned + 1" rule because args are pushed strictly in emission order),
and `?N` → `$N` (reuse of an index — common, e.g. `?1` twice at `:292` — is fine; PG allows `$1`
repeated). Typical param count: 1 per text/field/tag/deck/regex node; id/state/prop/flag nodes inline
constants and push **0** params.

**Dialect flag (recommended):** add a `Dialect::{Sqlite,Pg}` field to `SqlWriter` (it already holds
`col: &mut Collection`, which knows its backend via `col.storage`). Emit `like`/`collate nocase` for
Sqlite and `ilike`/`collate unicase` for Pg (§2d, §2e). The regexp→function-form (§2b) and `==`→`=`
(§2c) edits are **dialect-neutral** (dual-compatible) and unconditional. This keeps all PG knowledge in
the writer and avoids fragile post-hoc string munging; the only remaining PG-side string transform is
the placeholder rewrite (already done). `prepare_sort`/`write_order` (in `search/mod.rs`) similarly need
to pick `ilike`/`unicase` per backend — thread the same dialect in.

---

## 3. Temp-table strategy for PG

`PgStorage` holds one `RefCell<postgres::Client>` → one session → PG `TEMPORARY` tables are
session-private and visible across statements. Lifecycle today is **create-per-search + drop-on-guard-
drop** (`CardTableGuard`/`NoteTableGuard` Drop at `search/mod.rs:136-155` call `clear_searched_*_table`
= `DROP TABLE IF EXISTS`). Keep that lifecycle; it maps cleanly to PG.

### 3a. DDL translations (provide PG-specific SQL constants in `PgStorage`)

| SQLite (`*.sql`) | PG DDL |
|---|---|
| `search_cids_setup.sql`: `CREATE TEMPORARY TABLE search_cids (cid integer PRIMARY KEY NOT NULL)` | `CREATE TEMPORARY TABLE search_cids (cid bigint PRIMARY KEY NOT NULL)` |
| `search_cids_setup_ordered.sql`: `CREATE TEMPORARY TABLE search_cids (cid integer NOT NULL)` (order via implicit `rowid`) | `CREATE TEMPORARY TABLE search_cids (cid bigint NOT NULL, pos bigint GENERATED ALWAYS AS IDENTITY)` |
| `search_nids_setup.sql`: `... search_nids (nid integer PRIMARY KEY NOT NULL)` | `CREATE TEMPORARY TABLE search_nids (nid bigint PRIMARY KEY NOT NULL)` |
| `*_order.sql`: `sort_order (pos integer PRIMARY KEY, ...)` | `sort_order (pos bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ...)` (rest `integer`→`bigint`) |

Each setup file is `DROP TABLE IF EXISTS x; CREATE TEMPORARY TABLE x (...);` — both statements run via
`execute_batch` → PG `client.batch_execute(...)`. PG default is `ON COMMIT PRESERVE ROWS`, which is
what we want (the table outlives the `INSERT` and is read by a later `SELECT`). Explicit `DROP` in the
guard handles cleanup; `DROP TABLE IF EXISTS` handles a stale table from a prior search in the same
session.

### 3b. The `rowid` → `pos` problem (ordered cards)

`all_searched_cards_in_search_order` (`card/mod.rs:602-610`) does
`... , search_cids where cards.id = search_cids.cid order by search_cids.rowid`. **PG has no `rowid`.**
PG version: add the `pos bigint GENERATED ALWAYS AS IDENTITY` column (above) and
`ORDER BY search_cids.pos`. Identity values are assigned in the order rows arrive from the
`INSERT ... SELECT ... ORDER BY <add_order clause>`, so insertion order == search order. (Verify with a
test that exercises a `Builtin` sort through `all_cards_for_search_in_order`.)

### 3c. The `INSERT INTO search_cids {sql}` column-list problem

`search/mod.rs:241` builds `insert into search_cids {sql}` (and `:306` for `search_nids`). With the
ordered table now having a second `pos` column, a bare `INSERT ... SELECT <one col>` fails in PG
(column count mismatch). **Fix: change `search/mod.rs:241` and `:306` to
`insert into search_cids (cid) {sql}` / `insert into search_nids (nid) {sql}`** — valid on **both**
backends (SQLite accepts the explicit single-column list), so no dialect branch. Likewise
`set_search_table_to_card_ids` (`card/mod.rs:724`): `insert into search_cids values (?)` →
PG `insert into search_cids (cid) values ($1)`, and `with_ids_in_searched_notes_table`
(`note/mod.rs:331`) → `insert into search_nids (nid) values ($1)`. `search_cards_of_notes_into_table.sql`
and `at_or_above_position.sql` insert into the **unordered** (single-column) `search_cids`, so they work
without a list — but adding `(cid)` to them too is harmless and uniform.

### 3d. Transactions

Searches usually run in autocommit. In PG, `CREATE TEMPORARY TABLE` in autocommit commits immediately;
the subsequent INSERT/SELECT/DROP all see it. If a search runs **inside** an outer `PgStorage`
transaction (`tx_depth > 0`), the temp table lives in that tx and the guard's `DROP` cleans it — fine.
No `ON COMMIT DROP` (we want PRESERVE). The existing `tx_depth`/savepoint machinery
(`pg/mod.rs:734-773`) needs no change for search.

---

## 4. Method-by-method port plan (ordered, with PG SQL sketches)

Dependency order. Tasks T0–T4 are **not** blocked on the pgrx ext (testable with function-free
searches: `nid:`, `cid:`, `is:new`, `prop:ivl>1`, `added:`, `flag:`, `deck:` by id, whole-collection,
state). Tasks marked **[EXT]** need the ext installed on the local test PG (M2.3b-2) because the SQL
carries `regexp`/`process_text`/`extract_*`/`field_at_index`/`fnvhash`.

**Equivalence gate (all tasks):** flip the rslib test harness to build PG collections when
`ANKI_TEST_PG_DSN` is set (per STATUS "Execution plan" step 1), then run rslib's existing search tests
on PG. Key suites: the behavioral `search_cards`/`search_notes` tests under `rslib/src/search/` and
`rslib/src/notes.rs`/scheduler; the `sqlwriter.rs` unit test (`:1123`) asserts exact **SQLite** strings
and must be updated for the writer edits (T0). The string-assert test is not the behavioral gate — the
collection-level search tests are.

---

### T0 — Writer changes (do FIRST; keeps SQLite green; unblocks valid PG SQL) — no ext

Edit `sqlwriter.rs` + `search/mod.rs`:
1. Add `Dialect` to `SqlWriter` (§2h). Thread the collection's backend in via `SqlWriter::new`.
2. regexp operator → function form, 5 sites (§2b). Dialect-neutral.
3. `==` → `=`, 1 writer site + 1 sql file (§2c). Dialect-neutral.
4. `like` → `ilike` and `collate nocase` → `collate unicase` under `Dialect::Pg` (§2d, §2e);
   same in `write_order`/`prepare_sort`.
5. Update `sqlwriter.rs` unit-test asserts for items 2–3 (`:1230-1296`, `:1259-1260`, `:1374-1395`).
6. `search/mod.rs:241`/`:306`: add `(cid)`/`(nid)` column list (§3c). Dialect-neutral.

Gate: `./ninja check:rust_test` stays green on SQLite (string asserts updated, behavior unchanged).

### T1 — Core execution: `execute_batch`, `execute_raw`, finish `query_ids` — no ext (for fn-free)

- `execute_batch(sql)` → `self.client.borrow_mut().batch_execute(&pg_sql)`. Used for the temp-table
  setup DDL and (later) the `*_order.sql`. For the DDL files, prefer **PG-specific SQL constants**
  (§3a) over translating SQLite DDL at runtime (the `pos integer PRIMARY KEY`→identity change is not a
  pure type swap). So `setup_searched_*` call `batch_execute(PG_SEARCH_CIDS_SETUP)` etc.
- `execute_raw(sql, params)` → translate placeholders (`sqlite_placeholders_to_pg`), bind the
  positional `args`, `self.client.borrow_mut().execute(&pg_sql, &params)` → returns `u64` row count as
  `usize`. (Same param-binding shape as the existing `query_ids` at `pg/mod.rs:1305-1309`.)
- `query_ids` (`pg/mod.rs:1299`): already correct for function-free SQL. Once T0 lands and the ext is
  installed, the **same** code runs function-bearing SQL unchanged. No change needed beyond confirming
  the dialect-emitted SQL (ilike/unicase/regexp-fn) reaches it.

Gate: function-free `search_cards("nid:..")`, `search_cards("is:new")` return correct ids on PG.

### T2 — Card temp-table infra + readers — no ext (fn-free), [EXT] for fn-bearing searches

Port (all `todo!()` in `pg/mod.rs`):
- `setup_searched_cards_table` / `setup_searched_cards_table_to_preserve_order` /
  `clear_searched_cards_table` (`DROP TABLE IF EXISTS search_cids`) — DDL constants from §3a.
- `with_searched_cards_table(preserve_order, f)` — setup → `f()` → clear (mirror `card/mod.rs:552`).
- `all_searched_cards` →
  `SELECT <card cols, "left" quoted> FROM cards WHERE id IN (SELECT cid FROM search_cids)`, decode via
  `pg_row_to_card` (reuse the column list at `pg/mod.rs:635`).
- `all_searched_cards_in_search_order` →
  `... FROM cards JOIN search_cids ON cards.id = search_cids.cid ORDER BY search_cids.pos` (§3b).
- `for_each_card_in_search<F>` — same SELECT as `all_searched_cards`, iterate. **BORROW HAZARD**
  (STATUS): bind `let rows = client.borrow_mut().query(...)?;` first (releasing the `RefMut`), then loop
  calling `func(card)` — the callback may re-enter the DB.
- `set_search_table_to_card_ids` — `insert into search_cids (cid) values ($1)` loop (§3c).
- `search_cards_of_notes_into_table` — PG of `search_cards_of_notes_into_table.sql`:
  `INSERT INTO search_cids (cid) SELECT id FROM cards WHERE nid IN (SELECT nid FROM search_nids)`.
- `all_cards_at_or_above_position(start)` — `with_searched_cards_table(false, ||)` +
  `INSERT INTO search_cids (cid) SELECT id FROM cards WHERE due >= $1 AND type = $2` (params
  `start`, `CardType::New`) + `all_searched_cards`.

Gate: `all_cards_for_search`, `all_cards_for_search_in_order`, `all_cards_for_ids`,
`for_each_card_in_search` over fn-free searches; full coverage once ext lands.

### T3 — Note temp-table infra + readers — no ext (fn-free), [EXT] for fn-bearing

- `setup_searched_notes_table` / `clear_searched_notes_table` — DDL constant (§3a).
- `with_ids_in_searched_notes_table(&[NoteId], f)` — setup + `insert into search_nids (nid) values ($1)`
  loop + `f()` + clear.
- `all_searched_notes` →
  `SELECT <note cols, cast(sfld as text) no-op> FROM notes WHERE id IN (SELECT nid FROM search_nids)`,
  decode `pg_row_to_note`.
- `for_each_note_in_search` — same SELECT, iterate (BORROW HAZARD as in T2).
- `for_each_note_tag_in_searched_notes` →
  `SELECT tags FROM notes WHERE id IN (SELECT nid FROM search_nids)`.
- `get_note_tags_by_id_list` — `with_ids_in_searched_notes_table` + `get_tags.sql` translated
  (`SELECT id, mtime_secs?/...` — check `get_tags.sql` columns; tag SELECT is fn-free).

Gate: note-side `all_cards_for_search`-equivalents and note-tag iteration on PG.

### T4 — `note_fields_by_checksum` — **[EXT]** (`field_at_index`)

`note/mod.rs:170`: `select id, field_at_index(flds, 0) from notes where csum=? and mid=?` →
`SELECT id, field_at_index(flds, 0) FROM notes WHERE csum=$1 AND mid=$2`. Needs the ext for
`field_at_index`. Used by `write_dupe` (`sqlwriter.rs:835-857`) → the `dupe:` search node.

### T5 — Builtin-sort path: `prepare_sort` + the 12 `*_order.sql` — no ext for most, **[EXT]** for 2

`prepare_sort` (`search/mod.rs:409`) → `execute_batch(one of 12 *_order.sql)`. Provide **PG variants**
(DDL §3a; identity `pos`; `==`→`=`):
- Fn-free, mechanical: `template_order.sql`, `deck_order.sql`, `notetype_order.sql`,
  `note_cards_order.sql`, `card_mod_order.sql`, `note_decks_order.sql` (uses
  `row_number() OVER (ORDER BY name)` — PG supports it), `note_due_order.sql`, `note_ease_order.sql`,
  `note_interval_order.sql`, `note_lapses_order.sql`, `note_reps_order.sql`. The `ORDER BY name` in
  template/deck/notetype uses the column's `COLLATE unicase` default (`pg-schema.sql:113,128,143`),
  matching SQLite's `COLLATE unicase` on those columns (`sqlite-schema-v18.sql`).
- **[EXT]** `note_original_position_order.sql` — `extract_original_position(data)` + `type == 0`
  (→ `type = 0`).
- `write_order`'s inline ORDER BY (`search/mod.rs:350-407`): `extract_fsrs_variable` (Stability/
  Difficulty), `extract_fsrs_retrievability` (Retrievability) → **[EXT]**; `collate nocase` (SortField)
  → `collate unicase` (T0); the Due/Ease/NoteCreation/etc. clauses are fn-free.

### T6 — Scheduler/queue order clauses (search-adjacent) — **[EXT]**

Not browser-search, but they reuse the temp-table/order machinery and the same ext fns; port alongside:
- `card/mod.rs:821` `fnvhash(id, mod)`, `:826-840` `extract_fsrs_variable`/
  `extract_fsrs_relative_retrievability`, `:924-925` `fnvhash` (`NewCardSorting`).
- `card/filtered.rs:45` `fnvhash(c.id, c.mod)`, `:57` `extract_fsrs_relative_retrievability`
  (filtered-deck gather, which goes through `search_cards_into_table`).

### T7 — dbproxy raw SQL (`db_query`/`db_query_row`/`db_execute_many`/`db_scalar*`) — separate track

`todo!()` at `pg/mod.rs:1839-1848`. Path for ankiweb's 11 raw-SQL sites
(`docs/pg-rewrite/ankiweb-raw-sql.md`): translate `?`→`$n` (reuse `sqlite_placeholders_to_pg`),
`count()`→`count(*)`, double-quoted string literals → single-quoted, bare `lastIvl`→`"lastIvl"`,
`date(...,'unixepoch','localtime')` rewrite. Independent of the search-node path; can proceed in
parallel. (Listed for completeness; full detail in `ankiweb-raw-sql.md`.)

---

## 5. Open risks / divergences (must be validated against rslib's search tests on PG)

1. **`sfld` numeric sort (highest-impact).** SQLite stores `sfld` as `integer` *when the sort field is
   numeric* (the deliberate dynamic-typing trick — `sqlite-schema-v18.sql:85-87`), so
   `ORDER BY n.sfld collate nocase` yields NULLs, then numbers in **numeric** order, then text. PG's
   `sfld` is `text` (`pg-schema.sql:63`), so `"10" < "9"` lexically and numbers don't precede text.
   `pg-schema.sql:22-23` explicitly defers this to "M3 search". **The `SortField` browser column will
   mis-order numeric sort fields** unless reproduced at query time (e.g.
   `ORDER BY (case when n.sfld ~ '^-?[0-9]+$' then 0 else 1 end), nullif(n.sfld,'')::numeric nulls first, n.sfld collate unicase`
   — design + difftest required). Flag prominently; affects `search_*` with `SortMode::Builtin{SortField}`.

2. **`ILIKE` Unicode over-match (§2d).** PG `ILIKE` folds full Unicode; SQLite `LIKE` folds ASCII only.
   Plain (non-`nc:`) field/text search on non-ASCII letters can match in PG where SQLite would not.
   Likely *more* correct for users, but **diverges from any rslib test asserting ASCII-only folding**.
   Validate; if a test breaks, consider an ASCII-fold expression instead of `ILIKE`.

3. **`COLLATE unicase` ordering (UNICASE HAZARD, STATUS).** The ICU `und-u-ks-level2` backing is not
   byte-identical to Rust `UniCase` (ICU treats `\x1f` and other control chars as ignorable). For the
   sort path (`ORDER BY name`, `collate unicase` on sfld) ICU == UniCase for control-char-free names
   (the realistic case). Residual divergence on control-char names → the planned pgrx **fold-key fn**
   (`pg-schema.sql:24`). Ensure difftests include mixed-case (and ideally control-char) names.

4. **Regex dialect.** The custom `regexp*` fns use the **Rust `regex` crate** (`sql_functions.rs:110-134`)
   in *both* backends (SQLite via rusqlite aux, PG via the ext). So no regex-dialect drift **provided**
   the ext genuinely reuses `regex` (the spike/option-(a) plan does). Do **not** be tempted to use PG's
   POSIX `~`/`~*` operators — they are a different engine and would diverge. The `(?i)` / `(?is)` flags
   the writer prepends (`sqlwriter.rs:303,316,511,543,560,589,594,657,907`) are Rust-regex flags; they
   only work through the ext, not PG POSIX.

5. **FSRS float formatting / equality.** `prop:retr=`, `prop:s=`, `prop:d=` emit a Rust-formatted float
   literal compared with `=`/`<`/`>` against `extract_fsrs_*` (f32) results (`sqlwriter.rs:415-431`).
   Equality on floats is inherently fragile, but identical fragility on both backends *iff* the ext's
   `fsrs` crate is pinned to anki's **5.1.0** (STATUS: 5.2.0 drifts retrievability). The math itself
   runs in the ext (same Rust), so values match; only literal round-trip formatting could differ —
   include a difftest over the retrievability/stability/difficulty corpus.

6. **`fnvhash` i64 wraparound.** `sql_functions.rs:54-60` returns `hasher.finish() as i64` (wrapping
   u64→i64). The ext returns PG `bigint` (i64) — same wrap. Order-by-`fnvhash` (random order) is stable
   only if the ext reproduces the exact `FnvHasher::write_i64` sequence. Anchor test (STATUS):
   `fnvhash(123,456) = 2901588438723782001`.

7. **Identity insertion order (§3b).** `INSERT ... SELECT ... ORDER BY` assigning `GENERATED AS IDENTITY`
   `pos` in SELECT order is reliable in PG practice but not contractually guaranteed across all plans.
   Validate the search-order readers with a multi-row `Builtin` sort; if ever flaky, switch to
   `row_number() OVER (ORDER BY <clause>)` materialized into `pos` explicitly.

8. **Integer division / `&` / `between`.** PG and SQLite both truncate integer division toward zero and
   share `&`, `between`, `not between`, `!=` semantics — the `prop:due` division (`sqlwriter.rs:373`),
   `(c.flags & 7)` (`:187`), and `rated`/`resched` ranges (`:330-355`) need no change beyond `==`→`=`.

9. **Inlined-literal `?` edge (low).** `prop:cds`/`has-cd` inline the key/value as SQL string literals
   (`sqlwriter.rs:404,411,438`); a literal containing `?` would be mis-rewritten by
   `sqlite_placeholders_to_pg`. Rare; note it. (Keys/values are user-controlled — also a pre-existing
   minor injection surface, unchanged by the port.)

---

## Appendix — the 3 SQL strings to keep verbatim (no `\x1f` escaping, exact bytes)

- Field-index LIKE pattern join uses a literal U+001F between `%`/`?n` tokens (`sqlwriter.rs:267`).
- `deck:` regex arg embeds U+001F: `(?i)^{native_deck}($|\x1f)` (`sqlwriter.rs:511`).
- `tag:` regex arg: `(?i).* {re}(::| ).*` (`sqlwriter.rs:316`).
These are passed as **bound params** (safe) except the field-index `%`/`\x1f` scaffold, which is inlined
into the LIKE/ILIKE pattern — keep the raw 0x1F byte; PG accepts it in a single-quoted literal.
