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
