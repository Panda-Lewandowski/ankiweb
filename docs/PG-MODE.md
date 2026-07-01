# Running ankiweb on PostgreSQL

ankiweb supports **two storage backends from the same code**: the original
**SQLite** (default) and **PostgreSQL** (opt-in). PG is an *additive* translation-style
port (a `StorageBackend` enum in the forked `anki` rslib); SQLite is never disabled.
The differential CI gate runs both side-by-side and asserts byte-for-byte identical
results, so the two stay equivalent by construction.

- **SQLite** — single process per collection (`max_workers=1` + an asyncio lock). The
  default; needs nothing extra.
- **PostgreSQL** — **N processes can serve ONE collection concurrently** (the whole
  point of the rewrite), launched as one command with `ANKIWEB_WORKERS=N` (§3). Each
  collection lives in its own PG **schema**.

---

## 1. Which backend runs is decided by ONE env var

| | SQLite mode | PG mode |
|---|---|---|
| trigger | `ANKIWEB_PG_DSN` unset/empty (the default) | `ANKIWEB_PG_DSN` set |
| data lives in | the `.anki2` SQLite file | the PG schema |
| `ANKIWEB_COLLECTION` | the actual data file | **media folder only** (no `.anki2` written) |

`ankiweb/config.py` defaults: `pg_dsn = ""`, `pg_schema = "ankiweb"`.

---

## 2. Prerequisites for PG mode

PG mode needs **two** things the default SQLite setup doesn't:

### 2a. The `anki_pg_ext` PostgreSQL extension (REQUIRED)

Anki's SQL uses 11 custom functions that plain PG lacks (`regexp`/`regexp_fields`/
`regexp_tags`, `field_at_index`, `process_text`, `fnvhash`, `extract_*` incl. the FSRS
extractors). They're shipped as a **pgrx (Rust) extension** that links Anki's *exact*
rslib code, so PG computes identically to SQLite. Without it, opening a collection
fails immediately.

Crate: `/mnt/sda/git/tools/anki-pg-ext/`. Prebuilt `.so` in `artifacts/`.

**glibc must match the target PG's.** The `artifacts/anki_pg_ext.so` was built on a
glibc-2.38 host; a Debian-bookworm PG (`postgres:18` / `pgvector/pgvector:pg18`) is
glibc 2.36 and will reject it (`version 'GLIBC_2.38' not found`). Rebuild in an image
whose glibc ≤ the target:

```bash
# builds a glibc-2.36 .so (loads on bookworm); ~10-13 min cold
docker run --rm \
  -v /mnt/sda/git/tools/anki-pg-fork:/fork-ro:ro \
  -v /mnt/sda/git/tools/anki-pg-ext:/ext-ro:ro \
  -v /some/out:/out \
  --entrypoint bash postgres:18-bookworm -c '
    set -eu   # NOT pipefail (cmd|head SIGPIPE would kill it)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq postgresql-server-dev-18 build-essential libclang-dev \
        clang libssl-dev pkg-config protobuf-compiler cmake curl git rsync ca-certificates
    curl -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal
    . "$HOME/.cargo/env"
    cargo install cargo-pgrx --version 0.19.1 --locked
    cargo pgrx init --pg18 "$(which pg_config)"
    rsync -a --exclude target --exclude .git /fork-ro/ /fork/ && rm -f /fork/rust-toolchain.toml
    rsync -a --exclude target --exclude .git /ext-ro/ /build/
    cd /build && cargo pgrx package
    cp "$(find target -name anki_pg_ext.so)" /out/ && cp anki_pg_ext.control /out/
    find /build -name "anki_pg_ext--*.sql" -exec cp {} /out/ \;'
```

Install the 3 files into the running PG container + create the extension **per database**:

```bash
docker cp /some/out/anki_pg_ext.so         <pg-container>:/usr/lib/postgresql/18/lib/
docker cp /some/out/anki_pg_ext.control    <pg-container>:/usr/share/postgresql/18/extension/
docker cp /some/out/anki_pg_ext--0.1.0.sql <pg-container>:/usr/share/postgresql/18/extension/
psql "<dsn>/<db>" -c 'CREATE EXTENSION anki_pg_ext;'   # installs the 11 fns into public
# sanity: fnvhash(123,456) must equal 2901588438723782001
```

### 2b. A Python with the forked `anki` wheel (REQUIRED)

PG mode lives in the **forked** `anki` wheel (the `StorageBackend` PG variant). The
stock `anki` from PyPI / the `conda ankiweb` env does **not** have it (check:
`strings .../anki/_rsbridge.so | grep nextpos_seq` → must be present). Build the fork
wheel with `cd /mnt/sda/git/tools/anki-pg-fork && ./ninja wheels:anki`, then
`pip install --reinstall out/wheels/anki-*.whl` into the env you run ankiweb with.

### 2c. The PG-aware ankiweb code

PG support is on the **`pg-rewrite` worktree branch**, NOT on `master`. Run ankiweb
from `/mnt/sda/git/web/ankiweb/.claude/worktrees/pg-rewrite` (or merge the branch).

---

## 3. How to run

### SQLite (default — nothing special)

```bash
cd /mnt/sda/git/web/ankiweb && \
ANKIWEB_PASSWORD="…" ANKIWEB_AC_KEY="…" ANKIWEB_LANG="zh-CN" \
ANKIWEB_HOST="0.0.0.0" ANKIWEB_AC_HOST="0.0.0.0" ANKIWEB_ALLOWED_HOSTS="<ip>" \
ANKIWEB_PORT="23674" ANKIWEB_AC_PORT="18765" \
ANKIWEB_COLLECTION="data/test_sqlite/collection.anki2" \
/root/anaconda3/envs/ankiweb/bin/python -m ankiweb
```

### PostgreSQL

Same, but **(a)** run from the worktree, **(b)** use the fork-wheel Python, **(c)** add
the two PG vars:

```bash
cd /mnt/sda/git/web/ankiweb/.claude/worktrees/pg-rewrite && \
ANKIWEB_PASSWORD="…" ANKIWEB_AC_KEY="…" ANKIWEB_LANG="zh-CN" \
ANKIWEB_HOST="0.0.0.0" ANKIWEB_AC_HOST="0.0.0.0" ANKIWEB_ALLOWED_HOSTS="<ip>" \
ANKIWEB_PORT="23674" ANKIWEB_AC_PORT="18765" \
ANKIWEB_COLLECTION="data/pg_media/collection.anki2" \
ANKIWEB_PG_DSN="postgresql://USER:PASS@HOST:5432/DBNAME" \
ANKIWEB_PG_SCHEMA="mycollection" \
<fork-wheel-python> -m ankiweb
```

- `ANKIWEB_PG_SCHEMA` (default `ankiweb`) selects the collection. A new name
  auto-bootstraps an empty collection; an existing one is reopened.
- `ANKIWEB_COLLECTION` in PG mode writes **no `.anki2`** — it only derives the media
  folder (`…/foo.anki2` → `…/foo.media` via a `.anki2`→`.media` rewrite). End the path
  with `.anki2`; the basename is free.

### Scaling out: N worker processes behind ONE port (`ANKIWEB_WORKERS`)

A single ankiweb process serializes everything (`max_workers=1` + an asyncio lock), so
one process never uses PG's concurrency no matter the backend. To actually run N
processes over the one collection you used to launch N copies by hand on N different
ports and load-balance in front. **`ANKIWEB_WORKERS=N` does that internally:** the
command below stays a *single* invocation on the *single* `ANKIWEB_AC_PORT`, but the
master pre-forks N worker processes that all bind that port via `SO_REUSEPORT`, so the
kernel spreads connections across them — no per-process ports, no external proxy.

```bash
# exactly the PG command above, plus ONE var:
ANKIWEB_WORKERS=8 \
ANKIWEB_PG_DSN="postgresql://USER:PASS@HOST:5432/DBNAME" \
ANKIWEB_PG_SCHEMA="mycollection" ANKIWEB_AC_PORT="18765" … \
<fork-wheel-python> -m ankiweb
# → "[ankiweb] 8 PG workers up — AC :18765, web :… (SO_REUSEPORT, schema=mycollection)"
```

- **PostgreSQL only.** `ANKIWEB_WORKERS>1` refuses to start without `ANKIWEB_PG_DSN` —
  SQLite cannot be shared by multiple writer processes. `ANKIWEB_WORKERS=1` (default) is
  the unchanged single-process path.
- **Each worker opens its own PG connection** to the one shared schema → true N-way
  concurrency (`id` minting is race-safe across processes: revlog/notes/cards ids come
  from wall-clock sequences, uniqueness guaranteed by the PK). Kill with one Ctrl-C /
  `SIGTERM` on the master; it forwards to the whole worker group.
- **Push notifier runs in worker 0 only** (else N workers would each push). A
  `setNotifyConfig` that lands on any worker is picked up within one poll cycle (worker 0
  re-reads `notify.json`).
- **Measuring throughput:** use `curl`, a browser, or the real AnkiConnect clients — a
  single **8-worker fleet scales ~1.98x** over one worker on concurrent `curl`. Do NOT
  benchmark it with a Python **`httpx`** client: httpx has a ~40 ms per-request stall
  against a `SO_REUSEPORT` pool that makes the fleet *look* slower than one worker — a
  client-side artifact, not the server (see `docs/pg-rewrite/m17_prefork_workers.py`).
- Sizing: workers are real processes; keep `N ≤ cores` and mind PG `max_connections`
  (each worker holds ~1 connection). Start around the number of CPU cores.

---

## 4. The schema model (one collection = one schema)

A PG **schema** is a namespace inside a database — think "sub-database / folder". Each
ankiweb collection is one schema with its own 13 tables. Multiple collections coexist
in one database, isolated by schema; each process connects with
`options=-csearch_path=<schema>,public` so it sees only its own tables (+ the extension
functions, which live in `public`).

```
ankiweb_test (database)
├── realcol   (schema)  → 13 tables, one full collection
├── ankiweb   (schema)  → 13 tables, another full collection
└── public    (schema)  → 0 tables, just the 11 anki_pg_ext functions
```

- Table-level isolation is complete (`realcol.cards` ≠ `ankiweb.cards`).
- They share the database (connection, the `public` extension, catalog, roles). It is
  NOT a security boundary by default — set per-schema grants if you need that.

### Tables: SQLite 14 vs PG 13 — the data tables match 1:1

12 domain tables are identical on both: `cards col config deck_config decks fields
graves notes notetypes revlog tags templates`. Differences are engine-only, **no Anki
data table is added or removed**:

- PG **+1** `meta_epoch` — cross-process cache-invalidation counters (multi-process
  infra; SQLite is single-process and doesn't need it).
- PG **−2** `sqlite_stat1`/`sqlite_stat4` — SQLite's own query-planner stats; PG keeps
  equivalents in `pg_statistic` (system catalog), so no user table.

### Inspecting in a GUI — look at the schema, not `public`

The tables are **not in `public`** (which holds only the extension functions), so a GUI
defaulting to `public` shows nothing. Expand/select the collection's schema. Or query:

```sql
SELECT table_schema, table_name FROM information_schema.tables
WHERE table_schema NOT IN ('public','information_schema') AND table_schema NOT LIKE 'pg\_%'
ORDER BY 1,2;
SET search_path TO <schema>, public;   -- then unqualified names work
SELECT count(*) FROM cards;
SELECT id, cid, ease, ivl, "lastIvl" FROM revlog;   -- camelCase cols need quoting
```

---

## 5. Migrating an existing SQLite collection into PG

Two fidelities (both copy the live file set first, incl. `-wal`, so the running server
is untouched):

- **apkg whole-collection** (simple, `Collection.export_anki_package(limit=None,
  with_scheduling=True)` → fresh PG `Collection.import_anki_package`): brings ALL
  notes/cards/decks/notetypes + current scheduling. **Drops empty decks and trims old
  revlog history** (apkg exports studyable content, not a byte copy).
- **colpkg full-collection**: byte-for-byte (all decks + full revlog). The fork supports
  it on PG (`import_export/package/colpkg/{import,pg}.rs`), but it is *not* a plain
  `Collection` method (it replaces the whole collection), so it needs a small backend
  call rather than a one-liner.

---

## 6. The test harness against your PG

`docs/pg-rewrite/m6_ci.sh` + the `m6/m8/m10/m12/m14` Python harnesses resolve the
test-PG DSN from, in order: env `ANKI_TEST_PG_DSN` → a **gitignored**
`docs/pg-rewrite/pg_test.env` (`export ANKI_TEST_PG_DSN=…`) → an old localhost default.
Point them at any PG that has `anki_pg_ext` + a database for per-test schemas:

```bash
echo 'export ANKI_TEST_PG_DSN="postgresql://USER:PASS@HOST:5432/anki_test"' \
  > docs/pg-rewrite/pg_test.env      # gitignored — keep the credential here, not in source
psql "<dsn>/postgres" -c 'CREATE DATABASE anki_test;'
psql "<dsn>/anki_test" -c 'CREATE EXTENSION anki_pg_ext;'
bash docs/pg-rewrite/m6_ci.sh        # auto-sources pg_test.env
```

The harness creates a random schema per test and `DROP SCHEMA … CASCADE`s it after —
so the test database normally looks **empty** between runs (by design).

---

## 7. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `CREATE EXTENSION` → `GLIBC_2.x not found` | `.so` built on newer glibc than the PG | rebuild in a base image with glibc ≤ target (§2a) |
| open fails: function `regexp`/`fnvhash`/… missing | extension not installed in that DB | `CREATE EXTENSION anki_pg_ext` in the DB |
| startup error, `_rsbridge` has no PG | stock anki wheel, not the fork | install the fork wheel (§2b) |
| "I see no tables" in a GUI | looking at `public` (only functions there) | open the collection's **schema** (§4) |
| `addNote: model was not found: Basic` | `ANKIWEB_LANG=zh-CN` localizes model names | use the localized name (e.g. `问答题`) or set lang `en` |
| test DB looks empty | per-test schemas are dropped after each run | expected; not a failure |
| `ANKIWEB_WORKERS>1 requires PostgreSQL` on startup | multi-worker needs PG (SQLite is single-writer) | set `ANKIWEB_PG_DSN`, or use `ANKIWEB_WORKERS=1` |
| multi-worker looks *slower* than 1 worker in a benchmark | Python `httpx` stalls ~40 ms/req against a `SO_REUSEPORT` pool (client artifact) | benchmark with `curl` / real clients; the fleet scales ~1.98x (§3) |
