"""Lifetime, non-blocking local-filesystem lock (Linux/macOS). Never unlink it."""
import fcntl
from pathlib import Path


class CollectionOwnerLock:
    def __init__(self, collection: Path):
        self.path = collection.resolve().with_suffix(".owner.lock")
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            raise RuntimeError("collection owner already open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("collection already has a writable owner") from None
        self.handle = handle

    def release(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None
