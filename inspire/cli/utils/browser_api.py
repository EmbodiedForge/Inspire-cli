"""Browser-based API client for endpoints not available via OpenAPI.

This module provides access to APIs that require SSO authentication
and are not exposed via the OpenAPI interface.

Discovered endpoints:
- POST /api/v1/train_job/list - List all training jobs
- POST /api/v1/logic_compute_groups/list - List compute groups
- POST /api/v1/train_job/users - List job creators
- GET /api/v1/user/detail - Current user details
- GET /api/v1/compute_resources/logic_compute_groups/{id} - Accurate GPU usage stats
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from .web_session import get_web_session, WebSession, DEFAULT_WORKSPACE_ID


BASE_URL = os.environ.get("INSPIRE_BASE_URL", "https://qz.sii.edu.cn")


@dataclass
class JobInfo:
    """Training job information."""
    job_id: str
    name: str
    status: str
    command: str
    created_at: str
    finished_at: Optional[str]
    created_by_name: str
    created_by_id: str
    project_name: str
    compute_group_name: str
    gpu_type: str
    gpu_count: int
    instance_count: int
    priority: int
    workspace_id: str

    @classmethod
    def from_api_response(cls, data: dict) -> "JobInfo":
        """Create JobInfo from API response."""
        framework_config = data.get("framework_config", [{}])[0]
        gpu_info = framework_config.get("instance_spec_price_info", {}).get("gpu_info", {})

        return cls(
            job_id=data.get("job_id", ""),
            name=data.get("name", ""),
            status=data.get("status", ""),
            command=data.get("command", ""),
            created_at=data.get("created_at", ""),
            finished_at=data.get("finished_at"),
            created_by_name=data.get("created_by", {}).get("name", ""),
            created_by_id=data.get("created_by", {}).get("id", ""),
            project_name=data.get("project_name", ""),
            compute_group_name=data.get("logic_compute_group_name", ""),
            gpu_type=gpu_info.get("gpu_type_display", ""),
            gpu_count=framework_config.get("gpu_count", 0),
            instance_count=framework_config.get("instance_count", 1),
            priority=data.get("priority", 0),
            workspace_id=data.get("workspace_id", ""),
        )


@dataclass
class GPUAvailability:
    """GPU availability for a compute group."""
    group_id: str
    group_name: str
    gpu_type: str
    total_gpus: int
    used_gpus: int
    available_gpus: int
    low_priority_gpus: int  # GPUs used by low-priority tasks (can be preempted)


def list_jobs(
    workspace_id: Optional[str] = None,
    created_by: Optional[str] = None,
    status: Optional[str] = None,
    page_num: int = 1,
    page_size: int = 50,
    session: Optional[WebSession] = None,
) -> tuple[list[JobInfo], int]:
    """List training jobs using browser API.

    This API is not available via OpenAPI - it requires SSO authentication.

    Args:
        workspace_id: Workspace to list jobs from. Defaults to DEFAULT_WORKSPACE_ID.
        created_by: Filter by creator user ID.
        status: Filter by job status (e.g., "job_running", "job_stopped").
        page_num: Page number (1-indexed).
        page_size: Number of jobs per page.
        session: Optional pre-existing web session.

    Returns:
        Tuple of (list of JobInfo, total count).
    """
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "page_num": page_num,
        "page_size": page_size,
    }

    if created_by:
        body["created_by"] = created_by
    if status:
        body["status"] = status

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session.storage_state)

        try:
            resp = context.request.post(
                f"{BASE_URL}/api/v1/train_job/list",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Referer": f"{BASE_URL}/jobs/distributedTraining",
                },
                data=json.dumps(body),
                timeout=30000,
            )

            if resp.status == 401:
                raise ValueError("Session expired or invalid")
            if resp.status >= 400:
                raise ValueError(f"API returned {resp.status}: {resp.text()}")

            data = resp.json()
            if data.get("code") != 0:
                raise ValueError(f"API error: {data.get('message')}")

            jobs_data = data.get("data", {}).get("jobs", [])
            total = data.get("data", {}).get("total", 0)

            jobs = [JobInfo.from_api_response(j) for j in jobs_data]
            return jobs, total

        finally:
            context.close()
            browser.close()


def list_compute_groups(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List compute groups using browser API.

    Args:
        workspace_id: Workspace to list groups from.
        session: Optional pre-existing web session.

    Returns:
        List of compute group dictionaries.
    """
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {
        "page_size": -1,
        "page_num": 1,
        "filter": {"workspace_id": workspace_id},
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session.storage_state)

        try:
            resp = context.request.post(
                f"{BASE_URL}/api/v1/logic_compute_groups/list",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Referer": f"{BASE_URL}/jobs/distributedTraining",
                },
                data=json.dumps(body),
                timeout=30000,
            )

            if resp.status >= 400:
                raise ValueError(f"API returned {resp.status}")

            data = resp.json()
            return data.get("data", {}).get("logic_compute_groups", [])

        finally:
            context.close()
            browser.close()


def get_current_user(session: Optional[WebSession] = None) -> dict:
    """Get current user details.

    Args:
        session: Optional pre-existing web session.

    Returns:
        User details dictionary.
    """
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session.storage_state)

        try:
            resp = context.request.get(
                f"{BASE_URL}/api/v1/user/detail",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": f"{BASE_URL}/jobs/distributedTraining",
                },
                timeout=30000,
            )

            if resp.status >= 400:
                raise ValueError(f"API returned {resp.status}")

            data = resp.json()
            return data.get("data", {})

        finally:
            context.close()
            browser.close()


def list_job_users(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List users who have created jobs.

    Args:
        workspace_id: Workspace to list users from.
        session: Optional pre-existing web session.

    Returns:
        List of user dictionaries.
    """
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session.storage_state)

        try:
            resp = context.request.post(
                f"{BASE_URL}/api/v1/train_job/users",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Referer": f"{BASE_URL}/jobs/distributedTraining",
                },
                data=json.dumps({"workspace_id": workspace_id}),
                timeout=30000,
            )

            if resp.status >= 400:
                raise ValueError(f"API returned {resp.status}")

            data = resp.json()
            return data.get("data", {}).get("items", [])

        finally:
            context.close()
            browser.close()


def get_accurate_gpu_availability(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[GPUAvailability]:
    """Get accurate GPU availability for all compute groups.

    This uses the /api/v1/compute_resources/logic_compute_groups/{id} API
    which provides real-time GPU usage statistics including:
    - Total GPUs in the compute group
    - GPUs currently in use
    - GPUs used by low-priority tasks (can be preempted)

    Args:
        workspace_id: Workspace to get availability for.
        session: Optional pre-existing web session.

    Returns:
        List of GPUAvailability objects with accurate usage stats.
    """
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    # First get all compute groups
    groups = list_compute_groups(workspace_id=workspace_id, session=session)

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session.storage_state)

        try:
            for group in groups:
                group_id = group['logic_compute_group_id']
                group_name = group['name']

                resp = context.request.get(
                    f"{BASE_URL}/api/v1/compute_resources/logic_compute_groups/{group_id}",
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Referer": f"{BASE_URL}/jobs/distributedTraining",
                    },
                    timeout=30000,
                )

                if resp.status != 200:
                    continue

                data = resp.json()
                resources = data.get("data", {}).get("logic_resouces", {})
                gpu_stats = data.get("data", {}).get("gpu_type_stats", [{}])

                gpu_type = ""
                if gpu_stats:
                    gpu_type = gpu_stats[0].get("gpu_info", {}).get("gpu_type_display", "Unknown")

                gpu_total = resources.get("gpu_total", 0)
                gpu_used = resources.get("gpu_used", 0)
                gpu_low_priority = resources.get("gpu_low_priority_used", 0)
                gpu_available = gpu_total - gpu_used

                results.append(GPUAvailability(
                    group_id=group_id,
                    group_name=group_name,
                    gpu_type=gpu_type,
                    total_gpus=gpu_total,
                    used_gpus=gpu_used,
                    available_gpus=gpu_available,
                    low_priority_gpus=gpu_low_priority,
                ))

        finally:
            context.close()
            browser.close()

    return results


# =============================================================================
# Notebook (Interactive Modeling) APIs
# =============================================================================


@dataclass
class ProjectInfo:
    """Project information."""
    project_id: str
    name: str
    workspace_id: str


@dataclass
class ImageInfo:
    """Docker image information."""
    image_id: str
    url: str
    name: str
    framework: str
    version: str


def list_projects(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[ProjectInfo]:
    """List available projects.

    Args:
        workspace_id: Workspace to list projects from.
        session: Optional pre-existing web session.

    Returns:
        List of ProjectInfo objects.
    """
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {
        "page": 1,
        "page_size": -1,
        "filter": {
            "workspace_id": workspace_id,
            "check_admin": True,
        },
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session.storage_state)

        try:
            resp = context.request.post(
                f"{BASE_URL}/api/v1/project/list",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Referer": f"{BASE_URL}/jobs/interactiveModeling",
                },
                data=json.dumps(body),
                timeout=30000,
            )

            if resp.status >= 400:
                raise ValueError(f"API returned {resp.status}")

            data = resp.json()
            if data.get("code") != 0:
                raise ValueError(f"API error: {data.get('message')}")

            items = data.get("data", {}).get("items", [])
            return [
                ProjectInfo(
                    project_id=item.get("id", ""),
                    name=item.get("name", ""),
                    workspace_id=item.get("workspace_id", workspace_id),
                )
                for item in items
            ]

        finally:
            context.close()
            browser.close()


def list_images(
    workspace_id: Optional[str] = None,
    source: str = "SOURCE_OFFICIAL",
    session: Optional[WebSession] = None,
) -> list[ImageInfo]:
    """List available Docker images.

    Args:
        workspace_id: Workspace to list images from.
        source: Image source filter (default: "SOURCE_OFFICIAL").
        session: Optional pre-existing web session.

    Returns:
        List of ImageInfo objects.
    """
    from playwright.sync_api import sync_playwright

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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session.storage_state)

        try:
            resp = context.request.post(
                f"{BASE_URL}/api/v1/image/list",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Referer": f"{BASE_URL}/jobs/interactiveModeling",
                },
                data=json.dumps(body),
                timeout=30000,
            )

            if resp.status >= 400:
                raise ValueError(f"API returned {resp.status}")

            data = resp.json()
            if data.get("code") != 0:
                raise ValueError(f"API error: {data.get('message')}")

            items = data.get("data", {}).get("images", [])
            results = []
            for item in items:
                # Parse image name and version from URL
                url = item.get("address", "")
                name = item.get("name", url.split("/")[-1] if url else "")
                framework = item.get("framework", "")
                version = item.get("version", "")

                results.append(ImageInfo(
                    image_id=item.get("image_id", ""),
                    url=url,
                    name=name,
                    framework=framework,
                    version=version,
                ))
            return results

        finally:
            context.close()
            browser.close()


def get_notebook_schedule(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> dict:
    """Get notebook schedule configuration including resource specs.

    Args:
        workspace_id: Workspace to get schedule for.
        session: Optional pre-existing web session.

    Returns:
        Schedule configuration dictionary with predef_train_spec and quota data.
    """
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session.storage_state)

        try:
            resp = context.request.get(
                f"{BASE_URL}/api/v1/notebook/schedule/{workspace_id}",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": f"{BASE_URL}/jobs/interactiveModeling",
                },
                timeout=30000,
            )

            if resp.status >= 400:
                raise ValueError(f"API returned {resp.status}")

            data = resp.json()
            if data.get("code") != 0:
                raise ValueError(f"API error: {data.get('message')}")

            return data.get("data", {})

        finally:
            context.close()
            browser.close()


def list_notebook_compute_groups(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List compute groups available for interactive notebooks.

    Args:
        workspace_id: Workspace to list groups from.
        session: Optional pre-existing web session.

    Returns:
        List of compute group dictionaries with GPU availability info.
    """
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {
        "page_num": 1,
        "page_size": -1,
        "filter": {
            "workspace_id": workspace_id,
            "support_job_type": "interactive_modeling",
            "include_gpu_type_stats": True,
        },
        "sorter": [],
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session.storage_state)

        try:
            resp = context.request.post(
                f"{BASE_URL}/api/v1/logic_compute_groups/list",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Referer": f"{BASE_URL}/jobs/interactiveModeling",
                },
                data=json.dumps(body),
                timeout=30000,
            )

            if resp.status >= 400:
                raise ValueError(f"API returned {resp.status}")

            data = resp.json()
            return data.get("data", {}).get("logic_compute_groups", [])

        finally:
            context.close()
            browser.close()


def create_notebook(
    name: str,
    project_id: str,
    project_name: str,
    image_id: str,
    image_url: str,
    logic_compute_group_id: str,
    quota_id: str,
    gpu_type: str,
    gpu_count: int = 1,
    cpu_count: int = 20,
    memory_size: int = 200,
    auto_stop: bool = False,
    priority: int = 10,
    vscode_version: str = "1.101.2",
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> dict:
    """Create a new interactive notebook instance.

    Args:
        name: Name for the notebook instance.
        project_id: Project ID to associate with.
        project_name: Project name.
        image_id: Docker image ID (mirror_id).
        image_url: Docker image URL (mirror_url).
        logic_compute_group_id: Compute group ID.
        quota_id: Resource quota/spec ID.
        gpu_type: GPU type string (e.g., "NVIDIA_H200_SXM_141G").
        gpu_count: Number of GPUs (default: 1).
        cpu_count: Number of CPUs (default: 20).
        memory_size: Memory in GB (default: 200).
        auto_stop: Auto-stop when idle (default: False).
        priority: Task priority (default: 10).
        vscode_version: VS Code version (default: "1.101.2").
        workspace_id: Workspace ID.
        session: Optional pre-existing web session.

    Returns:
        API response with notebook_id.
    """
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {
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
        "resource_spec_price": {
            "cpu_type": "",
            "cpu_count": cpu_count,
            "gpu_type": gpu_type,
            "gpu_count": gpu_count,
            "memory_size_gib": memory_size,
            "logic_compute_group_id": logic_compute_group_id,
            "quota_id": quota_id,
        },
        "task_priority": priority,
        "vscode_version": vscode_version,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session.storage_state)

        try:
            resp = context.request.post(
                f"{BASE_URL}/api/v1/notebook/create",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Referer": f"{BASE_URL}/jobs/interactiveModeling",
                },
                data=json.dumps(body),
                timeout=60000,
            )

            if resp.status == 401:
                raise ValueError("Session expired or invalid")
            if resp.status >= 400:
                raise ValueError(f"API returned {resp.status}: {resp.text()}")

            data = resp.json()
            if data.get("code") != 0:
                raise ValueError(f"API error: {data.get('message')}")

            return data.get("data", {})

        finally:
            context.close()
            browser.close()


def stop_notebook(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Stop a running notebook instance.

    Args:
        notebook_id: ID of the notebook to stop.
        session: Optional pre-existing web session.

    Returns:
        API response.
    """
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    body = {
        "notebook_id": notebook_id,
        "operation": "STOP",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session.storage_state)

        try:
            resp = context.request.post(
                f"{BASE_URL}/api/v1/notebook/operate",
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Referer": f"{BASE_URL}/jobs/interactiveModeling",
                },
                data=json.dumps(body),
                timeout=30000,
            )

            if resp.status == 401:
                raise ValueError("Session expired or invalid")
            if resp.status >= 400:
                raise ValueError(f"API returned {resp.status}: {resp.text()}")

            data = resp.json()
            if data.get("code") != 0:
                raise ValueError(f"API error: {data.get('message')}")

            return data.get("data", {})

        finally:
            context.close()
            browser.close()


def get_notebook_detail(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Get detailed notebook information.

    Args:
        notebook_id: Notebook instance ID (UUID).
        session: Optional pre-existing web session.

    Returns:
        Notebook detail dictionary.
    """
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=session.storage_state)

        try:
            resp = context.request.get(
                f"{BASE_URL}/api/v1/notebook/{notebook_id}",
                headers={
                    "Accept": "application/json",
                    "Referer": f"{BASE_URL}/jobs/interactiveModeling",
                },
                timeout=30000,
            )

            if resp.status == 401:
                raise ValueError("Session expired or invalid")
            if resp.status >= 400:
                raise ValueError(f"API returned {resp.status}: {resp.text()}")

            data = resp.json()
            if data.get("code") != 0:
                raise ValueError(f"API error: {data.get('message')}")

            return data.get("data", {})

        finally:
            context.close()
            browser.close()


def wait_for_notebook_running(
    notebook_id: str,
    session: Optional[WebSession] = None,
    timeout: int = 600,
    poll_interval: int = 5,
) -> dict:
    """Wait for a notebook instance to reach RUNNING status.

    Args:
        notebook_id: Notebook instance ID.
        session: Optional pre-existing web session.
        timeout: Max wait time in seconds.
        poll_interval: Poll interval in seconds.

    Returns:
        Notebook detail dictionary when RUNNING.

    Raises:
        TimeoutError: If notebook does not become RUNNING within timeout.
    """
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


def setup_notebook_rtunnel(
    notebook_id: str,
    port: int = 31337,
    ssh_port: int = 22222,
    ssh_public_key: Optional[str] = None,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 120,
) -> str:
    """Ensure the notebook exposes an rtunnel server via Jupyter proxy.

    This automates the JupyterLab UI to:
    1) Open the notebook IDE (JupyterLab)
    2) Open a terminal
    3) (Optional) Install an SSH public key into ~/.ssh/authorized_keys
    4) Start sshd (port `ssh_port`) and rtunnel server (port `port`)

    Returns:
        HTTPS proxy URL for the rtunnel WebSocket endpoint (to be used as PROXY_URL).
    """
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=session.storage_state)
        page = context.new_page()

        try:
            page.goto(
                f"{BASE_URL}/ide?notebook_id={notebook_id}",
                timeout=60000,
                wait_until="domcontentloaded",
            )

            # Find the embedded JupyterLab frame (notebook-inspire host).
            start = time.time()
            lab_frame = None
            while time.time() - start < 60:
                for fr in page.frames:
                    if "notebook-inspire" in fr.url and fr.url.endswith("/lab"):
                        lab_frame = fr
                        break
                if lab_frame:
                    break
                page.wait_for_timeout(500)

            if lab_frame is None:
                raise ValueError("Failed to locate JupyterLab frame")

            jupyter_proxy_url = lab_frame.url.removesuffix("/lab") + f"/proxy/{port}/"

            # Wait for JupyterLab UI to be ready (menu bar should exist).
            lab_frame.get_by_role("menuitem", name="File").first.wait_for(
                state="visible",
                timeout=60000,
            )

            # Dismiss Jupyter news prompt if present.
            for label in ("No", "Yes", "否", "不接收", "取消"):
                try:
                    btn = lab_frame.get_by_role("button", name=label)
                    if btn.count() > 0:
                        # Prefer closing the prompt (No), but any click removes overlay.
                        btn.first.click(timeout=1000)
                        break
                except Exception:
                    pass

            # Open a terminal.
            terminal_opened = False

            # Path A: Launcher card
            terminal_card = lab_frame.locator("div.jp-LauncherCard:has-text('Terminal')")
            try:
                terminal_card.first.wait_for(state="visible", timeout=20000)
                terminal_card.first.click(timeout=8000)
                terminal_opened = True
            except Exception:
                terminal_opened = False

            # Path B: Open Launcher then click Terminal
            if not terminal_opened:
                try:
                    launcher_btn = lab_frame.locator(
                        "button[title*='Launcher'], button[aria-label*='Launcher']"
                    ).first
                    if launcher_btn.count() > 0:
                        launcher_btn.click(timeout=2000)
                        page.wait_for_timeout(500)
                    terminal_card = lab_frame.locator("div.jp-LauncherCard:has-text('Terminal')")
                    terminal_card.first.wait_for(state="visible", timeout=20000)
                    terminal_card.first.click(timeout=8000)
                    terminal_opened = True
                except Exception:
                    terminal_opened = False

            # Path C: File -> New -> Terminal
            if not terminal_opened:
                try:
                    lab_frame.get_by_role("menuitem", name="File").first.click(timeout=3000)
                    lab_frame.get_by_role("menuitem", name="New").first.hover(timeout=3000)
                    lab_frame.get_by_role("menuitem", name="Terminal").first.click(timeout=5000)
                    terminal_opened = True
                except Exception:
                    terminal_opened = False

            if not terminal_opened:
                raise ValueError("Failed to open Jupyter terminal")

            # Ensure terminal tab is active before typing.
            try:
                term_tab = lab_frame.locator("li.lm-TabBar-tab:has-text('Terminal')").first
                if term_tab.count() > 0:
                    term_tab.click(timeout=2000)
                    page.wait_for_timeout(250)
            except Exception:
                pass

            # Run setup via terminal commands.

            # Use the same nightly tarball as the local tunnel client.
            try:
                from inspire.cli.utils.tunnel import RTUNNEL_DOWNLOAD_URL
            except Exception:
                RTUNNEL_DOWNLOAD_URL = "https://github.com/Sarfflow/rtunnel/releases/download/nightly/rtunnel-linux-amd64.tar.gz"

            import shlex

            cmd_lines: list[str] = []

            pip_index_url = os.environ.get("INSPIRE_PIP_INDEX_URL")
            pip_trusted_host = os.environ.get("INSPIRE_PIP_TRUSTED_HOST")
            apt_mirror_url = os.environ.get("INSPIRE_APT_MIRROR_URL")
            rtunnel_bin = os.environ.get("INSPIRE_RTUNNEL_BIN")

            if pip_index_url:
                cmd_lines.append(
                    f"pip config set global.index-url {shlex.quote(pip_index_url)}"
                )
                if pip_trusted_host:
                    cmd_lines.append(
                        f"pip config set global.trusted-host {shlex.quote(pip_trusted_host)}"
                    )
            elif pip_trusted_host:
                cmd_lines.append(
                    f"pip config set global.trusted-host {shlex.quote(pip_trusted_host)}"
                )

            if apt_mirror_url:
                cmd_lines.extend(
                    [
                        "echo '>>> configure apt source...'",
                        "CODENAME=$( . /etc/os-release && echo \"$VERSION_CODENAME\" )",
                        "cat >/etc/apt/sources.list.d/ubuntu.sources <<EOF",
                        "Types: deb",
                        f"URIs: {apt_mirror_url}",
                        "Suites: ${CODENAME} ${CODENAME}-updates ${CODENAME}-backports ${CODENAME}-security",
                        "Components: main restricted universe multiverse",
                        "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg",
                        "EOF",
                        "echo '>>> update apt cache...'",
                        "apt-get update -y -qq || apt-get update -y",
                    ]
                )

            if rtunnel_bin:
                cmd_lines.append(
                    "if [ -x {bin_path} ]; then cp {bin_path} /tmp/rtunnel && chmod +x /tmp/rtunnel; fi".format(
                        bin_path=shlex.quote(rtunnel_bin)
                    )
                )

            if ssh_public_key:
                cmd_lines.extend(
                    [
                        "mkdir -p ~/.ssh && chmod 700 ~/.ssh",
                        "cat >> ~/.ssh/authorized_keys <<'EOF'",
                        ssh_public_key.rstrip(),
                        "EOF",
                        "chmod 600 ~/.ssh/authorized_keys",
                    ]
                )

            cmd_lines.extend(
                [
                    f"RTUNNEL_URL={RTUNNEL_DOWNLOAD_URL!r}",
                    f"PORT={port}",
                    f"SSH_PORT={ssh_port}",
                    "if [ ! -x /usr/sbin/sshd ]; then export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq openssh-server; fi",
                    "pkill -f '/tmp/rtunnel' 2>/dev/null || true; pkill -f 'sshd -p' 2>/dev/null || true",
                    "if [ -x /usr/sbin/sshd ]; then mkdir -p /run/sshd; ssh-keygen -A >/dev/null 2>&1; /usr/sbin/sshd -p \"$SSH_PORT\" -E /tmp/sshd.log -o ListenAddress=127.0.0.1 -o PermitRootLogin=yes -o PasswordAuthentication=no -o PubkeyAuthentication=yes >/dev/null 2>&1 & fi",
                    "if [ ! -x /tmp/rtunnel ]; then rm -rf /tmp/rtunnel.d /tmp/rtunnel.tgz; mkdir -p /tmp/rtunnel.d; curl -fsSL \"$RTUNNEL_URL\" -o /tmp/rtunnel.tgz || echo 'WARN: rtunnel download failed'; tar -xzf /tmp/rtunnel.tgz -C /tmp/rtunnel.d 2>/dev/null || true; rtbin=$(find /tmp/rtunnel.d -maxdepth 4 -type f -name '*rtunnel*' 2>/dev/null | head -n 1); if [ -n \"$rtbin\" ]; then cp \"$rtbin\" /tmp/rtunnel && chmod +x /tmp/rtunnel; fi; fi",
                    "nohup /tmp/rtunnel \"127.0.0.1:$SSH_PORT\" \"0.0.0.0:$PORT\" >/tmp/rtunnel-server.log 2>&1 &",
                ]
            )

            for line in cmd_lines:
                page.keyboard.type(line, delay=5)
                page.keyboard.press("Enter")
                page.wait_for_timeout(80)

            # Derive proxy URL (prefer VSCode/code-server proxy).
            proxy_url = None
            try:
                vscode_tab = page.locator('img[alt="vscode"]').first
                if vscode_tab.count() > 0:
                    vscode_tab.click(timeout=5000)
                    page.wait_for_timeout(3000)

                vscode_url = None
                for fr in page.frames:
                    if "/vscode/" in fr.url:
                        vscode_url = fr.url
                        break

                if vscode_url:
                    from urllib.parse import urlparse, parse_qs

                    parsed = urlparse(vscode_url)
                    token = parse_qs(parsed.query).get("token", [None])[0]
                    base = vscode_url.split("?", 1)[0].rstrip("/")
                    proxy_url = f"{base}/proxy/{port}/"
                    if token:
                        proxy_url = f"{proxy_url}?token={token}"
            except Exception:
                proxy_url = None

            if not proxy_url:
                proxy_url = jupyter_proxy_url

            # Probe the proxy endpoint until it stops reporting connection refused.
            start = time.time()
            last_status = None
            while time.time() - start < timeout:
                try:
                    resp = context.request.get(proxy_url, timeout=5000)
                    body = ""
                    try:
                        body = resp.text()
                    except Exception:
                        body = ""
                    last_status = f"{resp.status} {body[:200].strip()}"
                    if "ECONNREFUSED" not in body:
                        return proxy_url
                except Exception as e:
                    last_status = str(e)

                page.wait_for_timeout(1000)

            raise ValueError(
                f"rtunnel server did not become reachable via proxy URL. Last response: {last_status}"
            )

        finally:
            try:
                context.close()
            finally:
                browser.close()
