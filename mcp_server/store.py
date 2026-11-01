"""Where the wiki's pages live: a folder on disk, or Cloud Storage behind an in-memory cache.

A store holds pages by relative name (`projects/quotes.md`). Everything above it (paths, validation,
scrubbing, index, log) lives in `wiki_store.py` and does not care which store it has.

- `FileStore` reads and writes the folder directly, so edits made in Obsidian are seen immediately.
- `CachedStore` loads every page once from a `Backend` (Cloud Storage in production), serves reads from
  memory and writes through. It assumes ONE writer: the Cloud Run service is deployed with
  max-instances 1, so there is no cache to go stale. Do not run two instances against one bucket.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol


class Store(Protocol):
    def names(self) -> list[str]: ...
    def read(self, name: str) -> str | None: ...
    def write(self, name: str, text: str) -> None: ...


class Backend(Protocol):
    """A remote object store of markdown pages."""

    def list_names(self) -> list[str]: ...
    def get(self, name: str) -> str | None: ...
    def put(self, name: str, text: str) -> None: ...


class FileStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, name: str) -> Path:
        f = (self.root / name).resolve()
        if self.root not in f.parents or f.suffix != ".md":
            raise ValueError(f"not a page inside the wiki folder: {name!r}")
        return f

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*.md") if ".git" not in p.parts)

    def read(self, name: str) -> str | None:
        f = self._path(name)
        return f.read_text(encoding="utf-8") if f.is_file() else None

    def write(self, name: str, text: str) -> None:
        f = self._path(name)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8", newline="\n")


class CachedStore:
    """Every page in memory, written through to a backend. Backend first: memory never runs ahead of the durable copy."""

    def __init__(self, backend: Backend, workers: int = 16):
        self._backend = backend
        self._workers = workers
        self._pages: dict[str, str] | None = None
        self._lock = threading.RLock()

    def _load(self) -> dict[str, str]:
        with self._lock:
            if self._pages is None:
                names = self._backend.list_names()
                with ThreadPoolExecutor(self._workers) as pool:
                    texts = list(pool.map(self._backend.get, names))
                self._pages = {n: t for n, t in zip(names, texts, strict=True) if t is not None}
            return self._pages

    def warm(self) -> int:
        """Load now (call at startup so the first request is not the slow one); returns the page count."""
        return len(self._load())

    def names(self) -> list[str]:
        return sorted(self._load())

    def read(self, name: str) -> str | None:
        return self._load().get(name)

    def write(self, name: str, text: str) -> None:
        pages = self._load()
        with self._lock:
            self._backend.put(name, text)
            pages[name] = text


def _token_credentials():
    """Credentials from BRAIN_GCS_TOKEN (a short-lived token from the operator's gcloud login) when set,
    else None so the client falls back to the environment (the Cloud Run service account, or a mirror key).
    Lets an operator seed or pull with a short-lived gcloud token instead of replacing application-default credentials."""
    token = os.environ.get("BRAIN_GCS_TOKEN")
    if not token:
        return None
    from google.oauth2.credentials import Credentials

    return Credentials(token)


class GcsBackend:
    """Pages as objects `<prefix>/<name>` in one bucket. The bucket must have object versioning on (e.g. a 90-day
    noncurrent-version delete rule; see deploy/README.md)."""

    def __init__(self, bucket_name: str, prefix: str = "", client=None):
        if client is None:
            from google.cloud import storage

            client = storage.Client(credentials=_token_credentials())
        self._client = client
        self._bucket = client.bucket(bucket_name)
        prefix = prefix.strip("/")
        self._prefix = f"{prefix}/" if prefix else ""

    def list_names(self) -> list[str]:
        return sorted(b.name[len(self._prefix):] for b in self._client.list_blobs(self._bucket, prefix=self._prefix) if b.name.endswith(".md"))

    def get(self, name: str) -> str | None:
        try:
            return self._bucket.blob(self._prefix + name).download_as_text(encoding="utf-8")
        except Exception as exc:
            if exc.__class__.__name__ == "NotFound":
                return None
            raise

    def put(self, name: str, text: str) -> None:
        self._bucket.blob(self._prefix + name).upload_from_string(text.encode("utf-8"), content_type="text/markdown; charset=utf-8")
