from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


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


@dataclass
class AnkiConnectConfig:
    enabled: bool = True
    api_key: str | None = None
    cors_origin_list: list = field(default_factory=lambda: ["http://localhost"])
    bind_address: str = "127.0.0.1"
    bind_port: int = 8765
    ignore_origin_list: list = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "AnkiConnectConfig":
        data = {}
        if Path(path).exists():
            data = json.loads(Path(path).read_text() or "{}")
        # Env vars override ankiconnect.json, so the AnkiConnect host/port can be set the
        # same way as the web server's (ANKIWEB_AC_HOST / ANKIWEB_AC_PORT / ANKIWEB_AC_KEY).
        return cls(
            enabled=_env_bool("ANKIWEB_AC_ENABLED", True),
            api_key=os.environ.get("ANKIWEB_AC_KEY") or data.get("apiKey"),
            cors_origin_list=data.get("webCorsOriginList", ["http://localhost"]),
            bind_address=os.environ.get("ANKIWEB_AC_HOST", data.get("webBindAddress", "127.0.0.1")),
            bind_port=int(os.environ.get("ANKIWEB_AC_PORT", data.get("webBindPort", 8765))),
            ignore_origin_list=data.get("ignoreOriginList", []),
        )

    def validate_for_production(self, web_auth_enabled: bool) -> None:
        """Do not expose compatibility RPC alongside production web authentication."""
        if not web_auth_enabled or not self.enabled:
            return
        if self.bind_address not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("production AnkiConnect must remain loopback-only")
        if not self.api_key or len(self.api_key) < 32:
            raise ValueError(
                "production AnkiConnect requires a separate API key of at least 32 characters"
            )
