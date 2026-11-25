"""Accepted task-priority observations scoped to one exact scheduling offer."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from inspire.config.account_state import resolve_account_state_root

_STATE_SUBDIR = "priority-observations"


def _observation_path(workspace_id: str, group_id: str, quota_id: str) -> Path:
    workspace = str(workspace_id or "").strip()
    identity = "\0".join((workspace, str(group_id or "").strip(), str(quota_id or "").strip()))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", workspace).strip("._-") or "default"
    return resolve_account_state_root() / _STATE_SUBDIR / f"{slug}-{digest}.json"


def record_accepted_priority(
    workspace_id: str,
    priority: int,
    *,
    group_id: str = "",
    quota_id: str = "",
) -> None:
    """Best-effort record of an accepted create for this exact group and quota."""
    workspace = str(workspace_id or "").strip()
    group = str(group_id or "").strip()
    quota = str(quota_id or "").strip()
    if not workspace or not group or not quota:
        return
    path = _observation_path(workspace, group, quota)
    payload = {
        "workspace_id": workspace,
        "logic_compute_group_id": group,
        "quota_id": quota,
        "accepted_priority": int(priority),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, sort_keys=True)
        temporary.replace(path)
    except (OSError, ValueError):
        return
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def observed_accepted_priority(
    workspace_id: str,
    *,
    group_id: str = "",
    quota_id: str = "",
) -> int | None:
    """The last accepted priority for this offer, never a workspace-wide fallback."""
    if not str(group_id or "").strip() or not str(quota_id or "").strip():
        return None
    path = _observation_path(workspace_id, group_id, quota_id)
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if (
        str(raw.get("workspace_id") or "") != str(workspace_id or "").strip()
        or str(raw.get("logic_compute_group_id") or "") != str(group_id or "").strip()
        or str(raw.get("quota_id") or "") != str(quota_id or "").strip()
    ):
        return None
    try:
        return int(raw["accepted_priority"])
    except (KeyError, TypeError, ValueError):
        return None


__all__ = ["observed_accepted_priority", "record_accepted_priority"]
