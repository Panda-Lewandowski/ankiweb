from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4


class LessonReceiptStore:
    """Durable, text-free import receipts stored beside the server collection."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    async def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        receipt = {
            **payload,
            "receipt_id": uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await asyncio.to_thread(self._save_sync, receipt)
        return receipt

    async def list(self, limit: int = 20) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_sync, limit)

    def _save_sync(self, receipt: dict[str, Any]) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._directory / f"{receipt['created_at'].replace(':', '')}-{receipt['receipt_id']}.json"
        descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", suffix=".json", dir=self._directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(receipt, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _list_sync(self, limit: int) -> list[dict[str, Any]]:
        if not self._directory.is_dir():
            return []
        receipts = []
        for path in sorted(self._directory.glob("*.json"), reverse=True)[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("receipt_id"):
                receipts.append(data)
        return receipts
