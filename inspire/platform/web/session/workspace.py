"""Workspace availability helpers using a WebSession."""

from __future__ import annotations

import logging
from typing import Callable

from .models import WebSession

logger = logging.getLogger(__name__)


def fetch_node_dimensions(
    session: WebSession,
    *,
    request_json_fn: Callable[..., dict],
    base_url: str = "https://api.example.com",
    workspace_id: str,
    logic_compute_group_id: str,
    page_size: int = 1000,
) -> dict:
    """Fetch node-dimension metrics for one workspace compute group.

    This endpoint is readable by normal workspace users and includes dynamic
    GPU/CPU/memory availability plus task counts. It is the preferred source
    for full-node availability; ``cluster_nodes/list`` is admin-only on the
    current platform.
    """
    if not session.storage_state or not session.storage_state.get("cookies"):
        if not session.cookies:
            raise ValueError("Session expired or invalid (missing storage state)")

    resolved_workspace_id = str(workspace_id or "").strip()
    if not resolved_workspace_id:
        raise ValueError("workspace_id is required for node-dimension availability")
    resolved_group_id = str(logic_compute_group_id or "").strip()
    if not resolved_group_id:
        raise ValueError("logic_compute_group_id is required for node-dimension availability")

    # The console reads this action from the workspace service.  The same account gets
    # AccessForbidden from cluster?Action=ListNodeDimension even though the console can
    # show the per-node data; using the service the browser uses is what makes the probe
    # useful to a normal member.
    url = f"{base_url}/api/v2/workspace?Action=ListNodeDimension"
    page_num = 1
    nodes: list[dict] = []
    total = 0

    while True:
        body = {
            "page_num": page_num,
            "page_size": page_size,
            "filter": {
                "workspace_id": resolved_workspace_id,
                "logic_compute_group_id": resolved_group_id,
            },
        }
        payload = request_json_fn(
            session,
            "POST",
            url,
            body=body,
            headers={"Referer": f"{base_url}/jobs/distributedTraining"},
            timeout=30,
        )
        # v2 Action envelope (#82): failures arrive as HTTP 200 with
        # ResponseMetadata.Error, so decode the envelope instead of checking a code field.
        error = ((payload.get("ResponseMetadata") or {}).get("Error")) or {}
        if isinstance(error, dict) and error:
            message = error.get("Message", "unknown error")
            raise ValueError(
                f"List node dimensions failed: API error {error.get('Code', 'Error')}: {message}"
            )
        data = payload.get("Result") or {}
        if not isinstance(data, dict):
            data = {}
        page_nodes = data.get("node_dimensions", []) or []
        nodes.extend(page_nodes)
        total = int(data.get("total", len(nodes)) or 0)
        if len(nodes) >= total or not page_nodes:
            break
        page_num += 1

    return {"data": {"node_dimensions": nodes, "total": total or len(nodes)}}
