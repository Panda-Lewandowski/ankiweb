"""Single-worker production entrypoint; never starts AnkiConnect or legacy RPC."""
import os
import re
import logging
from urllib.parse import urlsplit

from ankiweb.config import Settings


def validate(settings):
    if not settings.production_mode:
        raise ValueError("production entrypoint requires ANKIWEB_PRODUCTION=true")
    if not settings.password_hash or len(settings.app_secret) < 32 or not settings.cookie_secure:
        raise ValueError("production requires password hash, independent secret and secure cookies")
    if not settings.allowed_hosts or any("*" in host for host in settings.allowed_hosts):
        raise ValueError("production requires exact allowed hosts")
    if not settings.auth_allowed_origins or any(
        urlsplit(origin).scheme != "https" for origin in settings.auth_allowed_origins
    ):
        raise ValueError("production requires explicit HTTPS origins")
    if not settings.source_url.startswith("https://") or not re.search(r"[0-9a-f]{40}", settings.source_url):
        raise ValueError("source URL must identify the exact 40-character deployed commit")
    if settings.backups_dir is None:
        raise ValueError("production requires persistent backups directory")
    collection = settings.collection_path.resolve()
    backups = settings.backups_dir.resolve()
    if backups == collection.parent or backups.is_relative_to(collection.parent):
        raise ValueError("backups must be separate from the collection directory")
    if os.environ.get("WEB_CONCURRENCY", "1") != "1" or os.environ.get("UVICORN_WORKERS", "1") != "1":
        raise ValueError("only one application worker is supported")


def main():
    import uvicorn
    from ankiweb.app import create_app
    os.umask(0o077)
    operations_log = logging.getLogger("language_trainer.operations")
    operations_log.setLevel(logging.INFO)
    operations_log.propagate = False
    if not operations_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        operations_log.addHandler(handler)
    settings = Settings.from_env()
    validate(settings)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1,
                access_log=False, proxy_headers=False, timeout_graceful_shutdown=120)


if __name__ == "__main__":
    main()
