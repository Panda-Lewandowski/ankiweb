from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

_BASE_HOST_PREFIXES = ("127.0.0.1:", "localhost:", "[::1]:")
_BASE_HOSTS = ("127.0.0.1", "localhost", "testserver", "[::1]")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def host_allowed(host: str, extra=()) -> bool:
    """DNS-rebinding guard. Always allows localhost; allows any host explicitly listed in
    `extra` (matched with OR without a :port); `'*'` in `extra` disables the check entirely
    (open to any Host header — only do this on a trusted network)."""
    if "*" in extra:
        return True
    if host.startswith(_BASE_HOST_PREFIXES) or host in _BASE_HOSTS:
        return True
    if host in extra:
        return True
    bare = host.rsplit(":", 1)[0] if host.count(":") == 1 else host  # strip :port (not IPv6)
    return bare in extra


@dataclass(frozen=True)
class Settings:
    collection_path: Path
    host: str = "127.0.0.1"
    port: int = 8000
    assets_dir: Path = Path(__file__).parent / "web_assets"
    shell_dir: Path = Path(__file__).parent / "shell"
    import_tmp_dir: Path = Path(__file__).parent / "_import_tmp"
    trainer_dir: Path = Path(__file__).parent / "trainer_static"
    # Extra Host-header values accepted by the DNS-rebinding guard (beyond localhost),
    # e.g. ("192.168.1.50:8000",) or ("myhost.local",). "*" disables the check.
    allowed_hosts: tuple = ()
    # AGPL §13 source offer: where this deployment's Corresponding Source lives. Shown on
    # the /about page + the toolbar "Source" link. Set ANKIWEB_SOURCE_URL when deploying.
    source_url: str = ""
    # UI language chosen at startup (Anki locale code, e.g. "zh-CN", "ja"). Empty = English.
    # Applied by CollectionService.open() via anki.lang.set_lang(); there is no in-UI switcher.
    lang: str = ""
    # Production auth is enabled by an Argon2id encoded password hash. The independent app
    # secret HMACs opaque session/CSRF tokens before they reach the sidecar auth database.
    password_hash: str = ""
    app_secret: str = ""
    auth_db_path: Path | None = None
    auth_allowed_origins: tuple[str, ...] = ()
    cookie_secure: bool = True
    session_idle_seconds: int = 12 * 60 * 60
    session_absolute_seconds: int = 30 * 24 * 60 * 60
    login_max_attempts: int = 5
    login_base_delay_seconds: int = 2
    login_max_delay_seconds: int = 15 * 60
    login_attempt_window_seconds: int = 15 * 60
    production_mode: bool = False
    backups_dir: Path | None = None
    minimum_free_bytes: int = 256 * 1024 * 1024
    max_request_bytes: int = 2 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        if os.environ.get("ANKIWEB_PASSWORD"):
            raise ValueError(
                "ANKIWEB_PASSWORD is no longer accepted; configure ANKIWEB_PASSWORD_HASH"
            )
        default = Path.home() / ".local/share/ankiweb/collection.anki2"
        collection_path = Path(os.environ.get("ANKIWEB_COLLECTION", str(default)))
        return cls(
            production_mode=_env_bool("ANKIWEB_PRODUCTION", False),
            backups_dir=Path(os.environ["ANKIWEB_BACKUPS"]) if os.environ.get("ANKIWEB_BACKUPS") else None,
            collection_path=collection_path,
            host=os.environ.get("ANKIWEB_HOST", "127.0.0.1"),
            port=int(os.environ.get("ANKIWEB_PORT", "8000")),
            import_tmp_dir=Path(os.environ["ANKIWEB_IMPORT_TMP_DIR"]) if os.environ.get("ANKIWEB_IMPORT_TMP_DIR") else (collection_path.parent / "import-tmp"),
            trainer_dir=Path(os.environ["ANKIWEB_TRAINER_DIR"]) if os.environ.get("ANKIWEB_TRAINER_DIR") else Path(__file__).parent / "trainer_static",
            allowed_hosts=tuple(
                h.strip() for h in os.environ.get("ANKIWEB_ALLOWED_HOSTS", "").split(",") if h.strip()),
            source_url=os.environ.get("ANKIWEB_SOURCE_URL", ""),
            lang=os.environ.get("ANKIWEB_LANG", ""),
            password_hash=os.environ.get("ANKIWEB_PASSWORD_HASH", ""),
            app_secret=os.environ.get("ANKIWEB_APP_SECRET", ""),
            auth_db_path=(Path(os.environ["ANKIWEB_AUTH_DB"])
                          if os.environ.get("ANKIWEB_AUTH_DB") else None),
            auth_allowed_origins=tuple(
                origin.strip().rstrip("/")
                for origin in os.environ.get("ANKIWEB_AUTH_ALLOWED_ORIGINS", "").split(",")
                if origin.strip()
            ),
            cookie_secure=_env_bool("ANKIWEB_COOKIE_SECURE", True),
            session_idle_seconds=int(os.environ.get("ANKIWEB_SESSION_IDLE_SECONDS", 12 * 60 * 60)),
            session_absolute_seconds=int(os.environ.get(
                "ANKIWEB_SESSION_ABSOLUTE_SECONDS", 30 * 24 * 60 * 60)),
            login_max_attempts=int(os.environ.get("ANKIWEB_LOGIN_MAX_ATTEMPTS", 5)),
            login_base_delay_seconds=int(os.environ.get("ANKIWEB_LOGIN_BASE_DELAY_SECONDS", 2)),
            login_max_delay_seconds=int(os.environ.get("ANKIWEB_LOGIN_MAX_DELAY_SECONDS", 15 * 60)),
            login_attempt_window_seconds=int(os.environ.get(
                "ANKIWEB_LOGIN_ATTEMPT_WINDOW_SECONDS", 15 * 60)),
        )
