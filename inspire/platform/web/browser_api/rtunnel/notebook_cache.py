"""Cache resolved notebook lab URLs to avoid repeated browser navigation."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_CACHE_BASENAME = "notebook-contexts"
_CACHE_VERSION = 1


@dataclass
class NotebookContext:
    notebook_id: str
    lab_url: str
    jupyter_token: str | None = None
    resolved_at: float = field(default_factory=time.time)
    proxy_url: str | None = None

    def is_fresh(self, ttl_seconds: int = 300) -> bool:
        """Return True if this context was resolved within *ttl_seconds*."""
        if self.resolved_at <= 0:
            return False
        return (time.time() - self.resolved_at) <= ttl_seconds

    @classmethod
    def load(cls, notebook_id: str, account: Optional[str] = None) -> NotebookContext | None:
        """Load a cached context for *notebook_id* (or None if missing / expired)."""
        path = _cache_path(account=account)
        payload = _load_cache_file(path)
        notebooks = payload.get("notebooks", {})
        entry = notebooks.get(notebook_id)
        if not isinstance(entry, dict):
            return None
        return cls(
            notebook_id=entry.get("notebook_id", notebook_id),
            lab_url=entry.get("lab_url", ""),
            jupyter_token=entry.get("jupyter_token"),
            resolved_at=float(entry.get("resolved_at", 0)),
            proxy_url=entry.get("proxy_url"),
        )

    def save(self, account: Optional[str] = None) -> None:
        """Persist this context to the cache file."""
        path = _cache_path(account=account)
        payload = _load_cache_file(path)
        notebooks = payload.setdefault("notebooks", {})
        if not isinstance(notebooks, dict):
            notebooks = {}
            payload["notebooks"] = notebooks

        notebooks[self.notebook_id] = {
            "notebook_id": self.notebook_id,
            "lab_url": self.lab_url,
            "jupyter_token": self.jupyter_token,
            "resolved_at": self.resolved_at,
            "proxy_url": self.proxy_url,
        }
        payload["version"] = _CACHE_VERSION
        _save_cache_file(path, payload)


def _normalize_account(account: Optional[str]) -> Optional[str]:
    if not account:
        return None
    value = account.strip()
    if not value:
        return None
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return normalized or None


def _default_cache_dir() -> Path:
    return Path.home() / ".cache" / "inspire-cli"


def _cache_path(account: Optional[str] = None) -> Path:
    root = _default_cache_dir()
    normalized = _normalize_account(account)
    if normalized:
        return root / f"{_CACHE_BASENAME}-{normalized}.json"
    return root / f"{_CACHE_BASENAME}.json"


def _load_cache_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": _CACHE_VERSION, "notebooks": {}}
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"version": _CACHE_VERSION, "notebooks": {}}
    if not isinstance(raw, dict):
        return {"version": _CACHE_VERSION, "notebooks": {}}
    notebooks = raw.get("notebooks")
    if not isinstance(notebooks, dict):
        notebooks = {}
    return {"version": raw.get("version", _CACHE_VERSION), "notebooks": notebooks}


def _save_cache_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
