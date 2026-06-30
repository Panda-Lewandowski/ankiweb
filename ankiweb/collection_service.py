from __future__ import annotations
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import anki.lang
from anki.collection import Collection
from ankiweb.config import Settings
from google.protobuf.descriptor import FieldDescriptor

T = TypeVar("T")


def op_changes_to_flags(changes) -> dict:
    """Convert an OpChanges proto into a {field_name: bool} dict (only its bool fields)."""
    return {
        f.name: getattr(changes, f.name)
        for f in changes.DESCRIPTOR.fields
        if f.type == FieldDescriptor.TYPE_BOOL
    }


def _pg_connect_dsn(base_dsn: str, schema: str) -> str:
    """Return the base PostgreSQL DSN with an `options=-csearch_path=<schema>,public` query
    param appended (URL-encoded) so the collection's tables resolve in `<schema>` while the
    pgrx extension functions (installed in `public`) stay reachable. The trailing `,public`
    is REQUIRED. If the DSN already carries an `options` param it is left untouched."""
    parts = urlsplit(base_dsn)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "options" for k, _ in query):
        query.append(("options", f"-csearch_path={schema},public"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _provision_pg(base_dsn: str, schema: str, col_path) -> None:
    """Prepare PostgreSQL + the filesystem for a PG-backed open:
      1. CREATE SCHEMA IF NOT EXISTS <schema> on an admin connection (the base DSN, no
         search_path override) — the connect-only open path requires the namespace to
         pre-exist; PostgreSQL never auto-creates a search_path schema.
      2. Ensure the media folder exists — media stays on the filesystem in PG mode, and
         MediaManager skips auto-creating the folder when server=True.
    psycopg is imported lazily so SQLite mode needs no PostgreSQL driver installed."""
    import psycopg
    from psycopg import sql
    with psycopg.connect(base_dsn, autocommit=True) as conn:
        try:
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        except (psycopg.errors.UniqueViolation, psycopg.errors.DuplicateSchema):
            # `CREATE SCHEMA IF NOT EXISTS` is NOT atomic against a concurrent
            # creator: when several ankiweb worker processes start at once on a
            # fresh schema, two can pass the existence check and one then hits a
            # unique-violation on pg_namespace (or duplicate_schema). The schema
            # exists either way — which is the whole post-condition — so the loser
            # treats the race as success. (The actual collection bootstrap inside
            # is made race-safe separately by the PG backend's advisory lock.)
            pass
    from anki.media import media_paths_from_col_path
    media_dir, _media_db = media_paths_from_col_path(str(col_path))
    Path(media_dir).mkdir(parents=True, exist_ok=True)


def build_collection(settings: Settings) -> Collection:
    """Open (bootstrapping on first use) the Collection. SQLite by default; PostgreSQL when
    `settings.pg_dsn` is set. In PG mode the filesystem `collection_path` is used ONLY to
    derive the media folder (no `.anki2` file is written) — the collection store lives in
    PostgreSQL under `settings.pg_schema`. Always runs on the serialized worker thread."""
    anki.lang.set_lang(settings.lang or "en")
    path = settings.collection_path
    if not settings.pg_dsn:
        return Collection(str(path), server=False)
    _provision_pg(settings.pg_dsn, settings.pg_schema, path)
    connect_dsn = _pg_connect_dsn(settings.pg_dsn, settings.pg_schema)
    return Collection(str(path), server=False, pg_dsn=connect_dsn)


class CollectionService:
    """Owns the single Collection. All access is serialized: pylib objects are
    not thread-safe, and the Rust backend serializes internally anyway."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="anki")
        # Auxiliary pool for thread-safe Rust backend calls that must run CONCURRENTLY
        # with the main worker (FSRS compute/simulate + latest_progress polling +
        # set_wants_abort) so progress is observable while a long compute runs.
        self._aux_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="anki-aux")
        self._lock = asyncio.Lock()
        self._col: Collection | None = None
        self._subscribers: list = []

    @property
    def settings(self):
        return self._settings

    async def open(self) -> None:
        path = self._settings.collection_path
        path.parent.mkdir(parents=True, exist_ok=True)

        def _open() -> Collection:
            return build_collection(self._settings)

        loop = asyncio.get_running_loop()
        self._col = await loop.run_in_executor(self._executor, _open)

    async def reopen(self) -> None:
        """Re-open the collection on the worker WITHOUT shutting it down — for ops
        that close it (export_collection_package). Unlike close(), keeps the executor."""

        def _reopen() -> Collection:
            # build_collection re-applies the language for self-consistency with open(): a
            # fresh Collection otherwise inherits the process-global, which a second service
            # with a different lang could have changed. Defensive — the single-service
            # topology makes the global sufficient today. In PG mode this re-opens the same
            # schema (connect-only; no re-bootstrap).
            return build_collection(self._settings)

        loop = asyncio.get_event_loop()
        async with self._lock:
            self._col = await loop.run_in_executor(self._executor, _reopen)

    async def close(self) -> None:
        if self._col is None:
            return
        col, self._col = self._col, None
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, lambda: col.close())
        await loop.run_in_executor(None, self._executor.shutdown)
        await loop.run_in_executor(None, self._aux_executor.shutdown)

    async def run(self, fn: Callable[[Collection], T]) -> T:
        async with self._lock:
            col = self._col
            if col is None:
                raise RuntimeError("collection not open")
            loop = asyncio.get_running_loop()
            pg = bool(self._settings.pg_dsn)

            def _call():
                # De-chattification (M4-6 poll throttle): in PG mode, mark a new
                # operation boundary so the cross-process cache-staleness poll
                # (meta_epoch SELECT) runs at most ONCE for this whole operation
                # instead of once per backend call — AnkiConnect actions loop many
                # backend calls per item. No-op in SQLite mode.
                if pg:
                    col._backend.arm_cache_poll()
                return fn(col)

            return await loop.run_in_executor(self._executor, _call)

    async def run_op(self, fn: Callable[[Collection], T], initiator: str | None = None) -> T:
        """Run a mutating op (fn returns OpChanges or an OpChanges* wrapper), then
        broadcast the change flags on the bus. Returns the op result unchanged."""
        result = await self.run(fn)
        changes = getattr(result, "changes", result)
        flags = op_changes_to_flags(changes)
        if any(flags.values()):  # skip no-op broadcasts (e.g. set_current returns all-False)
            await self.emit(flags, initiator)
        return result

    async def backend_raw(self, method: str, data: bytes) -> bytes:
        def fn(col):
            return getattr(col._backend, f"{method}_raw")(data)
        return await self.run(fn)

    async def backend_raw_concurrent(self, method: str, data: bytes) -> bytes:
        """Call `col._backend.<method>_raw` OFF the serialized main worker, on the aux
        pool, so it runs CONCURRENTLY with the main worker and with other aux calls.
        ONLY for thread-safe Rust backend calls that don't mutate Python-side collection
        state: FSRS compute/simulate (long, read-only), `latest_progress` (polled while
        they run), and `set_wants_abort` (cancels them). The Rust backend serializes its
        own collection access internally; `latest_progress` uses a separate lock, so it
        returns live progress while a compute holds the collection lock."""
        col = self._col
        if col is None:
            raise RuntimeError("collection not open")
        fn = getattr(col._backend, f"{method}_raw")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._aux_executor, lambda: fn(data))

    def subscribe(self, cb) -> None:
        """cb(changes, initiator) — called after a mutating op broadcasts changes."""
        self._subscribers.append(cb)

    async def emit(self, changes, initiator) -> None:
        for cb in list(self._subscribers):
            res = cb(changes, initiator)
            if asyncio.iscoroutine(res):
                await res
