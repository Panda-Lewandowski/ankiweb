from __future__ import annotations
import asyncio
import os
import signal
import socket
import subprocess
import sys
import uvicorn
from ankiweb.config import Settings
from ankiweb.collection_service import CollectionService
from ankiweb.bridge.hub import BridgeHub
from ankiweb.ankiconnect.config import AnkiConnectConfig
from ankiweb.app import create_app
from ankiweb.ankiconnect.app import create_ankiconnect_app
from ankiweb.notifier import NotifierState, DeckNotifier, snapshot


def _reuseport_sock(host: str, port: int) -> socket.socket:
    """A listening socket with SO_REUSEPORT so every prefork worker binds the SAME
    (host, port) and the kernel load-balances connections across them — one public
    port, no external proxy."""
    if not hasattr(socket, "SO_REUSEPORT"):
        sys.exit("ANKIWEB_WORKERS>1 needs SO_REUSEPORT (Linux); not available on this OS.")
    # Match uvicorn's own Config.bind_socket EXACTLY, only adding SO_REUSEPORT: create,
    # SO_REUSEADDR, bind, set_inheritable — and crucially DO NOT listen()/setblocking().
    # uvicorn hands the bound-but-unlistened socket to asyncio's create_server, which
    # does the listen + non-blocking + per-connection setup itself; passing a socket we
    # already listen()ed on took a degraded accept path (~40ms/request, TCP-stalled).
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind((host, port))
    s.set_inheritable(True)
    return s


async def _serve(worker_id: int = 0, reuse_port: bool = False) -> None:
    settings = Settings.from_env()
    ac_config = AnkiConnectConfig.load(settings.collection_path.parent / "ankiconnect.json")
    service = CollectionService(settings)
    await service.open()
    hub = BridgeHub()
    notifier_state = NotifierState(settings.collection_path.parent / "notify.json")
    web = create_app(settings, service=service, hub=hub, notifier=notifier_state)
    # Same NotifierState instance, so /extra_actions/setNotifyConfig on the AC port edits
    # the live config that the web form and the running notifier task share.
    api = create_ankiconnect_app(settings, service=service, config=ac_config, hub=hub,
                                 notifier=notifier_state)
    web_server = uvicorn.Server(uvicorn.Config(web, host=settings.host, port=settings.port,
                                               log_level="info"))
    api_server = uvicorn.Server(uvicorn.Config(api, host=ac_config.bind_address,
                                               port=ac_config.bind_port, log_level="info"))
    # In a prefork fleet each worker binds its own SO_REUSEPORT socket (kernel spreads
    # connections); passing it to serve() also stops uvicorn re-binding the port itself.
    web_socks = [_reuseport_sock(settings.host, settings.port)] if reuse_port else None
    api_socks = [_reuseport_sock(ac_config.bind_address, ac_config.bind_port)] if reuse_port else None
    # Background deck-learnability push notifier: exactly ONE instance (worker 0) — N
    # workers would each push duplicates. In multi-worker mode it reloads notify.json
    # each cycle so setNotifyConfig landing on any worker still takes effect.
    notifier_task = None
    if worker_id == 0:
        notifier = DeckNotifier(notifier_state, fetch=lambda: service.run(snapshot),
                                watch_config=reuse_port)
        notifier_task = asyncio.create_task(notifier.run())
    try:
        await asyncio.gather(web_server.serve(sockets=web_socks),
                             api_server.serve(sockets=api_socks))
    finally:
        if notifier_task is not None:
            notifier_task.cancel()
            try:
                await notifier_task
            except asyncio.CancelledError:
                pass
        await service.close()


def _spawn_workers(n: int) -> None:
    """Supervisor: SPAWN N fresh `python -m ankiweb` worker processes (each re-imports
    cleanly and initialises its own PG-backed collection), all behind the single
    ANKIWEB_AC_PORT via SO_REUSEPORT — true N-way concurrency on the ONE shared
    collection, no external proxy. PostgreSQL only; SQLite cannot be shared by multiple
    writer processes.

    We SPAWN (re-exec), not os.fork(): the anki Rust backend / native state does not
    survive a fork into usable form across every worker, which throttled each forked
    worker badly. A fresh interpreter per worker is what gunicorn/uvicorn do too."""
    if not os.environ.get("ANKIWEB_PG_DSN"):
        sys.exit("ANKIWEB_WORKERS>1 requires PostgreSQL (set ANKIWEB_PG_DSN); "
                 "SQLite cannot be shared by multiple writer processes.")
    settings = Settings.from_env()
    ac_config = AnkiConnectConfig.load(settings.collection_path.parent / "ankiconnect.json")
    procs: list[subprocess.Popen] = []
    for i in range(n):
        env = dict(os.environ, ANKIWEB_WORKER_ID=str(i))  # marks the child as a worker
        procs.append(subprocess.Popen([sys.executable, "-m", "ankiweb"], env=env))

    def _forward(signum, _frame):  # relay Ctrl-C / SIGTERM to the whole worker group
        for p in procs:
            try:
                p.send_signal(signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGINT, _forward)
    signal.signal(signal.SIGTERM, _forward)
    print(f"[ankiweb] {n} PG workers up — AC :{ac_config.bind_port}, web :{settings.port} "
          f"(SO_REUSEPORT, schema={os.environ.get('ANKIWEB_PG_SCHEMA', 'public')})", flush=True)
    for p in procs:
        try:
            p.wait()
        except KeyboardInterrupt:
            pass


def main() -> None:
    workers = int(os.environ.get("ANKIWEB_WORKERS", "1") or "1")
    worker_id = os.environ.get("ANKIWEB_WORKER_ID")
    if worker_id is not None:
        # spawned worker: serve this one process directly (own SO_REUSEPORT socket),
        # notifier only on worker 0. Do NOT recurse into the supervisor.
        asyncio.run(_serve(worker_id=int(worker_id), reuse_port=True))
    elif workers > 1:
        _spawn_workers(workers)
    else:
        asyncio.run(_serve())


if __name__ == "__main__":
    main()
