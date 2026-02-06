"""Browser (web-session) notebook APIs (HTTP endpoints)."""

from __future__ import annotations

from typing import Any, Optional

from inspire.config import Config
from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from .models import ImageInfo
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session


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


def list_images(
    workspace_id: Optional[str] = None,
    source: str = "SOURCE_OFFICIAL",
    session: Optional[WebSession] = None,
) -> list[ImageInfo]:
    """List available Docker images."""
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

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


def get_notebook_schedule(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> dict:
    """Get notebook schedule configuration including resource specs."""
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    return _request_notebooks_data(
        session,
        "GET",
        f"/notebook/schedule?workspace_id={workspace_id}",
        timeout=30,
        default_data={},
    )


def list_notebook_compute_groups(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List notebook compute groups.

    Falls back to config-based compute groups when the API endpoint
    is unavailable (404).
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    body = {
        "workspace_id": workspace_id,
    }

    try:
        return _request_notebooks_data(
            session,
            "POST",
            "/notebook/compute_groups",
            body=body,
            timeout=30,
            default_data=[],
        )
    except ValueError as e:
        if "404" not in str(e):
            raise
        # API endpoint missing — fall back to config-based compute groups
        return _config_compute_groups_fallback()


def _config_compute_groups_fallback() -> list[dict]:
    """Build synthetic compute group list from inspire-cli config."""
    try:
        cfg, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
    except Exception:
        return []

    groups = cfg.compute_groups
    result = []
    for g in groups:
        gpu_type = g.get("gpu_type", "")
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
                    if gpu_type
                    else []
                ),
            }
        )
    return result


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
) -> dict:
    """Create a new notebook instance."""
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    # Use proto-compatible field names (mirror_id/mirror_url, not image_id/image_url)
    body = {
        "name": name,
        "project_id": project_id,
        "project_name": project_name,
        "mirror_id": image_id,
        "mirror_url": image_url,
        "logic_compute_group_id": logic_compute_group_id,
        "quota_id": quota_id,
        "gpu_count": gpu_count,
        "cpu_count": cpu_count,
        "memory_size": memory_size,
        "shared_memory_size": shared_memory_size,
        "auto_stop": auto_stop,
        "workspace_id": workspace_id,
    }

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


__all__ = [
    "create_notebook",
    "get_notebook_detail",
    "get_notebook_schedule",
    "list_images",
    "list_notebook_compute_groups",
    "start_notebook",
    "stop_notebook",
]
