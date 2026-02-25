"""Browser (web-session) notebook HTTP APIs (images, schedule, create, stop, start, detail, wait)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from inspire.config import Config
from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class NotebookFailedError(Exception):
    """Raised when a notebook reaches a terminal failure state."""

    def __init__(self, notebook_id: str, status: str, detail: dict, events: str = ""):
        self.notebook_id = notebook_id
        self.status = status
        self.detail = detail
        self.events = events
        parts = [f"Notebook '{notebook_id}' reached terminal status: {status}"]
        sub = detail.get("sub_status")
        if sub:
            parts.append(f"Sub-status: {sub}")
        super().__init__(". ".join(parts))


_NOTEBOOK_TERMINAL_STATUSES = frozenset({"FAILED", "ERROR", "STOPPED", "DELETED"})


@dataclass
class ImageInfo:
    """Docker image information."""

    image_id: str
    url: str
    name: str
    framework: str
    version: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _notebooks_referer() -> str:
    return f"{_get_base_url()}/jobs/interactiveModeling"


def _get_session_and_workspace_id(
    *,
    workspace_id: Optional[str],
    session: Optional[WebSession],
) -> tuple[WebSession, str]:
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    return session, workspace_id


def _request_notebooks_data(
    session: WebSession,
    method: str,
    endpoint_path: str,
    *,
    body: Optional[dict] = None,
    timeout: int = 30,
    default_data: Any = None,
) -> Any:
    data = _request_json(
        session,
        method,
        _browser_api_path(endpoint_path),
        referer=_notebooks_referer(),
        body=body,
        timeout=timeout,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    return data.get("data", default_data)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def list_images(
    workspace_id: Optional[str] = None,
    source: str = "SOURCE_OFFICIAL",
    session: Optional[WebSession] = None,
) -> list[ImageInfo]:
    """List available Docker images.

    Args:
        workspace_id: Workspace ID for filtering.
        source: Image source filter. Use "SOURCE_OFFICIAL" for official images,
            "SOURCE_PUBLIC" for public images (uses visibility filter as
            required by the platform API).
        session: Existing web session.
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    if source == "SOURCE_PUBLIC":
        # Public images require source_list + visibility (not a simple source field).
        # Discovered via Playwright network capture of the platform UI.
        body: dict = {
            "page": 0,
            "page_size": -1,
            "filter": {
                "source_list": ["SOURCE_PRIVATE", "SOURCE_PUBLIC"],
                "visibility": "VISIBILITY_PUBLIC",
                "registry_hint": {"workspace_id": workspace_id},
            },
        }
    else:
        body = {
            "page": 0,
            "page_size": -1,
            "filter": {
                "source": source,
                "source_list": [],
                "registry_hint": {"workspace_id": workspace_id},
            },
        }

    data = _request_notebooks_data(
        session,
        "POST",
        "/image/list",
        body=body,
        timeout=30,
        default_data={},
    )
    items = data.get("images", [])
    results = []
    for item in items:
        url = item.get("address", "")
        name = item.get("name", url.split("/")[-1] if url else "")
        framework = item.get("framework", "")
        version = item.get("version", "")

        results.append(
            ImageInfo(
                image_id=item.get("image_id", ""),
                url=url,
                name=name,
                framework=framework,
                version=version,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Schedule / Prices / Compute groups
# ---------------------------------------------------------------------------


def get_notebook_schedule(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> dict:
    """Get notebook schedule configuration including resource specs.

    Tries path-parameter format first (the format the UI uses), then falls
    back to query-parameter format.  Returns an empty schedule when neither
    endpoint is available.
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    # Try both endpoint formats — the UI uses path param, older deployments
    # may use query param.
    for endpoint in [
        f"/notebook/schedule/{workspace_id}",
        f"/notebook/schedule?workspace_id={workspace_id}",
    ]:
        try:
            return _request_notebooks_data(
                session,
                "GET",
                endpoint,
                timeout=30,
                default_data={},
            )
        except ValueError:
            continue

    # Neither endpoint worked — return empty schedule.
    return {}


def get_resource_prices(
    workspace_id: Optional[str] = None,
    logic_compute_group_id: str = "",
    session: Optional[WebSession] = None,
) -> list[dict]:
    """Fetch resource spec prices for a compute group.

    The UI calls this endpoint when the user opens the resource spec dialog.
    Returns a list of price entries, each containing quota_id, cpu_count,
    memory_size_gib, gpu_count, gpu_info, and price.
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    body = {
        "workspace_id": workspace_id,
        "schedule_config_type": "SCHEDULE_CONFIG_TYPE_DSW",
        "logic_compute_group_id": logic_compute_group_id,
    }

    try:
        data = _request_notebooks_data(
            session,
            "POST",
            "/resource_prices/logic_compute_groups/",
            body=body,
            timeout=30,
            default_data=[],
        )
    except ValueError:
        return []

    if isinstance(data, list):
        return data
    # The API nests results under 'lcg_resource_spec_prices'
    return data.get(
        "lcg_resource_spec_prices", data.get("resource_spec_prices", data.get("list", []))
    )


def list_notebook_compute_groups(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List notebook compute groups.

    Falls back to config-based compute groups when the API endpoint
    is unavailable (404) or returns an empty list.
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    body = {
        "workspace_id": workspace_id,
    }

    groups: list[dict] = []

    try:
        data = _request_notebooks_data(
            session,
            "POST",
            "/notebook/compute_groups",
            body=body,
            timeout=30,
            default_data=[],
        )
        if isinstance(data, list):
            groups = data
    except ValueError as e:
        if "404" not in str(e):
            raise

    if groups:
        return groups

    # API endpoint missing or returned an empty list — fall back to local config.
    fallback = _config_compute_groups_fallback(workspace_id=workspace_id)
    if fallback:
        return fallback

    # Last-resort fallback: reuse the generic compute group list endpoint.
    # This endpoint is primarily used for training jobs, but is typically a good
    # approximation when the notebook-specific endpoint is unavailable.
    try:
        from inspire.platform.web.browser_api.availability.api import (
            list_compute_groups as _list_groups,
        )

        data = _list_groups(workspace_id=workspace_id, session=session)
        if isinstance(data, list):
            return data
    except Exception:
        return []

    return []


def _config_compute_groups_fallback(workspace_id: str | None = None) -> list[dict]:
    """Build synthetic compute group list from inspire-cli config."""
    try:
        cfg, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
    except Exception:
        return []

    groups = cfg.compute_groups
    result = []
    for g in groups:
        group_ws_ids = g.get("workspace_ids") or []
        if workspace_id and group_ws_ids and workspace_id not in group_ws_ids:
            continue
        gpu_type = g.get("gpu_type", "")
        is_real_gpu = gpu_type and gpu_type.upper() != "CPU"
        result.append(
            {
                "logic_compute_group_id": g.get("id", ""),
                "name": g.get("name", ""),
                "gpu_type_stats": (
                    [
                        {
                            "gpu_info": {
                                "gpu_type": gpu_type,
                                "gpu_type_display": gpu_type,
                                "brand_name": gpu_type,
                            },
                        }
                    ]
                    if is_real_gpu
                    else []
                ),
            }
        )
    return result


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_notebook(
    name: str,
    project_id: str,
    project_name: str,
    image_id: str,
    image_url: str,
    logic_compute_group_id: str,
    quota_id: str,
    gpu_type: str,
    gpu_count: int,
    cpu_count: int,
    memory_size: int,
    shared_memory_size: int,
    auto_stop: bool,
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
    task_priority: Optional[int] = None,
    resource_spec_price: Optional[dict] = None,
) -> dict:
    """Create a new notebook instance.

    The request body must match the exact structure the platform UI sends.
    Captured via Playwright network interception — the proto rejects unknown
    fields, so only send fields the backend expects.
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    # Match the exact field set the platform UI sends (captured via Playwright).
    # Proto-compatible names: mirror_id/mirror_url (not image_id/image_url).
    # The UI does NOT send: gpu_type (top-level).
    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "name": name,
        "project_id": project_id,
        "project_name": project_name,
        "auto_stop": auto_stop,
        "mirror_id": image_id,
        "mirror_url": image_url,
        "logic_compute_group_id": logic_compute_group_id,
        "quota_id": quota_id,
        "cpu_count": cpu_count,
        "gpu_count": gpu_count,
        "memory_size": memory_size,
        "shared_memory_size": shared_memory_size,
    }

    # resource_spec_price is required for GPU notebooks.
    # Structure: {cpu_type, cpu_count, gpu_type, gpu_count, memory_size_gib,
    #             logic_compute_group_id, quota_id}
    if resource_spec_price is not None:
        body["resource_spec_price"] = resource_spec_price

    if task_priority is not None:
        body["task_priority"] = task_priority

    return _request_notebooks_data(
        session,
        "POST",
        "/notebook/create",
        body=body,
        timeout=30,
        default_data={},
    )


def stop_notebook(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Stop a running notebook instance."""
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    body = {
        "notebook_id": notebook_id,
        "operation": "STOP",
    }

    return _request_notebooks_data(
        session,
        "POST",
        "/notebook/operate",
        body=body,
        timeout=30,
        default_data={},
    )


def start_notebook(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Start a stopped notebook instance."""
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    body = {
        "notebook_id": notebook_id,
        "operation": "START",
    }

    return _request_notebooks_data(
        session,
        "POST",
        "/notebook/operate",
        body=body,
        timeout=30,
        default_data={},
    )


def get_notebook_detail(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Get detailed notebook information."""
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    return _request_notebooks_data(
        session,
        "GET",
        f"/notebook/{notebook_id}",
        timeout=30,
        default_data={},
    )


# ---------------------------------------------------------------------------
# Wait
# ---------------------------------------------------------------------------


def _try_fetch_events(notebook_id: str, session: WebSession) -> str:
    """Best-effort fetch of notebook events (K8s scheduling/allocation details)."""
    for path in [
        f"/notebook/{notebook_id}/events",
        f"/notebook/event/{notebook_id}",
    ]:
        try:
            data = _request_notebooks_data(session, "GET", path, timeout=5, default_data=[])
            if not data:
                continue
            items = data if isinstance(data, list) else data.get("events", data.get("list", []))
            if not isinstance(items, list) or not items:
                continue
            lines = []
            for ev in items[-10:]:
                if not isinstance(ev, dict):
                    continue
                reason = ev.get("reason") or ""
                message = ev.get("message") or ""
                ev_type = ev.get("type") or ""
                prefix = f"[{ev_type}] " if ev_type else ""
                label = f"{reason}: " if reason else ""
                lines.append(f"{prefix}{label}{message}")
            return "\n".join(lines) if lines else ""
        except Exception:
            continue
    return ""


def wait_for_notebook_running(
    notebook_id: str,
    session: Optional[WebSession] = None,
    timeout: int = 600,
    poll_interval: int = 5,
) -> dict:
    """Wait for a notebook instance to reach RUNNING status."""
    if session is None:
        session = get_web_session()

    start = time.time()
    last_status = None

    while True:
        notebook = get_notebook_detail(notebook_id=notebook_id, session=session)
        status = (notebook.get("status") or "").upper()
        if status:
            last_status = status

        if status == "RUNNING":
            return notebook

        if status in _NOTEBOOK_TERMINAL_STATUSES:
            events = _try_fetch_events(notebook_id, session)
            raise NotebookFailedError(notebook_id, status, notebook, events=events)

        if time.time() - start >= timeout:
            raise TimeoutError(
                f"Notebook '{notebook_id}' did not reach RUNNING within {timeout}s "
                f"(last status: {last_status or 'unknown'})"
            )

        time.sleep(poll_interval)


__all__ = [
    "ImageInfo",
    "NotebookFailedError",
    "create_notebook",
    "get_notebook_detail",
    "get_notebook_schedule",
    "get_resource_prices",
    "list_images",
    "list_notebook_compute_groups",
    "start_notebook",
    "stop_notebook",
    "wait_for_notebook_running",
    "_config_compute_groups_fallback",
]
