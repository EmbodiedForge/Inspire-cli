"""Workspace enumeration through the workspace v2 Action API."""

from __future__ import annotations

import re
from typing import Any

from inspire.platform.web.session.models import (
    DEFAULT_WORKSPACE_ID,
    SessionExpiredError,
    WebSession,
)

from .core import _browser_api_path, _get_base_url, _request_json

_WS_ID_RE = re.compile(r"^ws-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def try_enumerate_workspaces(
    session: WebSession,
    base_url: str | None = None,
    workspace_id: str | None = None,
    *,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Return accessible workspaces from ``workspace?Action=ListWorkspaces``.

    The optional workspace arguments are accepted by callers that enumerate a specific
    session scope, but enumeration itself is account-wide and needs no workspace body.
    ``strict`` preserves lookup failures when the caller needs authoritative metadata.
    """
    del workspace_id
    resolved_base_url = (base_url or "").strip() or _get_base_url()
    referer = f"{resolved_base_url}/jobs/distributedTraining"
    results: dict[str, dict[str, Any]] = {}

    try:
        response = _request_json(
            session,
            "POST",
            _browser_api_path("/workspaces/list"),
            referer=referer,
            body={},
            timeout=15,
        )
        data = response.get("data") or {}
        items = data.get("items") if isinstance(data, dict) else []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            workspace_id = str(item.get("id") or "").strip()
            if (
                not _WS_ID_RE.match(workspace_id)
                or workspace_id == DEFAULT_WORKSPACE_ID
                or workspace_id in results
            ):
                continue
            results[workspace_id] = dict(item)
            results[workspace_id]["id"] = workspace_id
            results[workspace_id]["name"] = str(item.get("name") or "").strip()
    except SessionExpiredError:
        raise
    except Exception:
        if strict:
            raise

    return list(results.values())
