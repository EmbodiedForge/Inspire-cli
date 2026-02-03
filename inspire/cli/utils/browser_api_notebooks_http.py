"""Browser (web-session) notebook APIs (HTTP endpoints only)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from inspire.cli.utils.browser_api_core import BASE_URL, _browser_api_path, _request_json
from inspire.cli.utils.web_session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session

__all__ = [
    "ImageInfo",
    "create_notebook",
    "get_notebook_detail",
    "get_notebook_schedule",
    "list_images",
    "list_notebook_compute_groups",
    "start_notebook",
    "stop_notebook",
    "wait_for_notebook_running",
]


@dataclass
class ImageInfo:
    """Docker image information."""

    image_id: str
    url: str
    name: str
    framework: str
    version: str


def list_images(
    workspace_id: Optional[str] = None,
    source: str = "SOURCE_OFFICIAL",
    session: Optional[WebSession] = None,
) -> list[ImageInfo]:
    """List available Docker images."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {
        "page": 0,
        "page_size": -1,
        "filter": {
            "source": source,
            "source_list": [],
            "registry_hint": {"workspace_id": workspace_id},
        },
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/image/list"),
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    items = data.get("data", {}).get("images", [])
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
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    data = _request_json(
        session,
        "GET",
        _browser_api_path(f"/notebook/schedule?workspace_id={workspace_id}"),
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    return data.get("data", {})


def list_notebook_compute_groups(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List notebook compute groups."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {
        "workspace_id": workspace_id,
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/notebook/compute_groups"),
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    return data.get("data", [])


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
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {
        "name": name,
        "project_id": project_id,
        "project_name": project_name,
        "image_id": image_id,
        "image_url": image_url,
        "logic_compute_group_id": logic_compute_group_id,
        "quota_id": quota_id,
        "gpu_type": gpu_type,
        "gpu_count": gpu_count,
        "cpu_count": cpu_count,
        "memory_size": memory_size,
        "shared_memory_size": shared_memory_size,
        "auto_stop": auto_stop,
        "workspace_id": workspace_id,
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/notebook/create"),
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    return data.get("data", {})


def stop_notebook(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Stop a running notebook instance."""
    if session is None:
        session = get_web_session()

    body = {
        "notebook_id": notebook_id,
        "operation": "STOP",
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/notebook/operate"),
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    return data.get("data", {})


def start_notebook(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Start a stopped notebook instance."""
    if session is None:
        session = get_web_session()

    body = {
        "notebook_id": notebook_id,
        "operation": "START",
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/notebook/operate"),
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        body=body,
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    return data.get("data", {})


def get_notebook_detail(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Get detailed notebook information."""
    if session is None:
        session = get_web_session()

    data = _request_json(
        session,
        "GET",
        _browser_api_path(f"/notebook/{notebook_id}"),
        referer=f"{BASE_URL}/jobs/interactiveModeling",
        timeout=30,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    return data.get("data", {})


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

        if time.time() - start >= timeout:
            raise TimeoutError(
                f"Notebook '{notebook_id}' did not reach RUNNING within {timeout}s "
                f"(last status: {last_status or 'unknown'})"
            )

        time.sleep(poll_interval)
