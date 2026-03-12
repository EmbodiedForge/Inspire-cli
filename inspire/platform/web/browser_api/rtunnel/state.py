"""Proxy state cache: save/load known rtunnel proxy URLs to disk."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

_CACHE_BASENAME = "rtunnel-proxy-state"
_CACHE_VERSION = 1
DEFAULT_PROXY_CACHE_TTL_SECONDS = 8 * 60 * 60


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


def get_rtunnel_state_file(
    *,
    account: Optional[str],
    cache_dir: Optional[Path] = None,
) -> Path:
    root = cache_dir or _default_cache_dir()
    normalized = _normalize_account(account)
    if normalized:
        return root / f"{_CACHE_BASENAME}-{normalized}.json"
    return root / f"{_CACHE_BASENAME}.json"


def _load_state_file(path: Path) -> dict[str, Any]:
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


def _save_state_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_cached_rtunnel_proxy_candidates(
    *,
    notebook_id: str,
    port: int,
    base_url: str,
    account: Optional[str],
    ttl_seconds: int = DEFAULT_PROXY_CACHE_TTL_SECONDS,
    cache_dir: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> list[str]:
    state_file = get_rtunnel_state_file(account=account, cache_dir=cache_dir)
    payload = _load_state_file(state_file)
    notebooks = payload.get("notebooks", {})
    entry = notebooks.get(notebook_id)
    if not isinstance(entry, dict):
        return []

    proxy_url = str(entry.get("proxy_url") or "").strip()
    entry_port = int(entry.get("port") or 0)
    entry_base_url = str(entry.get("base_url") or "").rstrip("/")
    updated_at = float(entry.get("updated_at") or 0)
    now = now_ts if now_ts is not None else time.time()
    if not proxy_url:
        return []
    if entry_port and entry_port != port:
        return []
    if entry_base_url and entry_base_url != base_url.rstrip("/"):
        return []
    if ttl_seconds > 0 and updated_at > 0 and (now - updated_at) > ttl_seconds:
        return []
    return [proxy_url]


def save_rtunnel_proxy_state(
    *,
    notebook_id: str,
    proxy_url: str,
    port: int,
    ssh_port: int,
    base_url: str,
    account: Optional[str],
    cache_dir: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> None:
    state_file = get_rtunnel_state_file(account=account, cache_dir=cache_dir)
    payload = _load_state_file(state_file)
    notebooks = payload.setdefault("notebooks", {})
    if not isinstance(notebooks, dict):
        notebooks = {}
        payload["notebooks"] = notebooks

    notebooks[notebook_id] = {
        "proxy_url": proxy_url,
        "port": int(port),
        "ssh_port": int(ssh_port),
        "base_url": base_url.rstrip("/"),
        "updated_at": float(now_ts if now_ts is not None else time.time()),
    }
    payload["version"] = _CACHE_VERSION
    _save_state_file(state_file, payload)
