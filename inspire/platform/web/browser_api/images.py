"""Browser (web-session) image management APIs (list, detail, create, save, delete)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional
from uuid import uuid4

from inspire.platform.web.browser_api.notebooks import (
    _get_session_and_workspace_id,
    _request_notebooks_data,
)
from inspire.platform.web.session import WebSession, get_web_session

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class CustomImageInfo:
    """Custom Docker image information."""

    image_id: str
    url: str
    name: str
    framework: str
    version: str
    source: str  # SOURCE_PRIVATE / SOURCE_PUBLIC / SOURCE_OFFICIAL
    status: str  # READY / BUILDING / FAILED
    description: str
    created_at: str


# Statuses that mean the image is pullable. ``SUCCESS`` is what the live
# /image/list response reports (official images all show it); ``READY`` appears in
# other responses. Everywhere else in the CLI already accepts both
# (cli/utils/workload_launch.py, cli/utils/job_submit.py), so anything that waits
# on an image has to accept both as well - accepting only READY turned a healthy
# image into a 300s hang followed by a bogus TimeoutError.
IMAGE_READY_STATUSES: frozenset[str] = frozenset({"READY", "SUCCESS"})

# Terminal failure states. ``ERROR`` has no live sample, but a permanently failed
# image must not burn the whole poll timeout either.
IMAGE_FAILED_STATUSES: frozenset[str] = frozenset({"FAILED", "ERROR"})


def _image_from_api(item: dict[str, Any]) -> CustomImageInfo:
    """Convert an API image dict to a CustomImageInfo."""
    url = item.get("address", "")
    name = item.get("name", url.split("/")[-1] if url else "")
    return CustomImageInfo(
        image_id=item.get("image_id", ""),
        url=url,
        name=name,
        framework=item.get("framework", ""),
        version=item.get("version", ""),
        source=item.get("source", ""),
        status=item.get("status", ""),
        description=item.get("description", ""),
        created_at=item.get("created_at", ""),
    )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_images_by_source(
    source: str = "official",
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
    visibility: Optional[str] = None,
) -> list[CustomImageInfo]:
    """List Docker images for any source, returning full metadata.

    Unlike :func:`~inspire.platform.web.browser_api.notebooks.list_images`
    (which returns the limited ``ImageInfo``), this function always returns
    ``CustomImageInfo`` objects with ``source``, ``status``, ``description``,
    and ``created_at`` populated from the raw API response.

    Args:
        source: One of ``"official"``, ``"public"``, ``"private"``,
            ``"personal-visible"``.
        workspace_id: Workspace ID for filtering.
        session: Existing web session.
        visibility: ``"public"`` or ``"private"``. This is a **different axis** from ``source``: the
            platform stores provenance in ``source`` and who-can-read-it in ``filter.visibility``, and they
            combine freely (an image with ``source=SOURCE_PRIVATE`` semantics is rare, while images with
            provenance "public" and visibility "private" are the normal result of saving a notebook).
            Measured: ``source="private"`` returns 0 for an account whose three saved images all appear
            under ``visibility="private"``.
    """
    source_map = {
        "official": "SOURCE_OFFICIAL",
        "public": "SOURCE_PUBLIC",
        "private": "SOURCE_PRIVATE",
        "personal-visible": "SOURCE_PERSONAL_VISIBLE",
    }
    api_source = source_map.get(source.lower(), source)

    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    if api_source in ("SOURCE_PUBLIC", "SOURCE_PERSONAL_VISIBLE"):
        # Named apart from the `visibility` parameter on purpose: reusing it made an explicit
        # --visibility flag read the enum string back as if it were a user value, silently forcing
        # every public query to private.
        visibility_enum = (
            "VISIBILITY_PUBLIC" if api_source == "SOURCE_PUBLIC" else "VISIBILITY_PRIVATE"
        )
        body: dict[str, Any] = {
            "page": 0,
            "page_size": -1,
            "filter": {
                "source_list": ["SOURCE_PRIVATE", "SOURCE_PUBLIC"],
                "visibility": visibility_enum,
                "registry_hint": {"workspace_id": workspace_id},
            },
        }
    else:
        body = {
            "page": 0,
            "page_size": -1,
            "filter": {
                "source": api_source,
                "source_list": [],
                "registry_hint": {"workspace_id": workspace_id},
            },
        }

    if visibility:
        body["filter"]["visibility"] = (
            "VISIBILITY_PUBLIC" if visibility.strip().lower() == "public" else "VISIBILITY_PRIVATE"
        )

    data = _request_notebooks_data(
        session,
        "POST",
        "/image/list",
        body=body,
        timeout=30,
        default_data={},
    )
    items = data.get("images", [])
    return [_image_from_api(item) for item in items]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def list_private_images(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[CustomImageInfo]:
    """List private (custom) Docker images.

    Args:
        workspace_id: Workspace ID for filtering.
        session: Existing web session.
        visibility: ``"public"`` or ``"private"``. This is a **different axis** from ``source``: the
            platform stores provenance in ``source`` and who-can-read-it in ``filter.visibility``, and they
            combine freely (an image with ``source=SOURCE_PRIVATE`` semantics is rare, while images with
            provenance "public" and visibility "private" are the normal result of saving a notebook).
            Measured: ``source="private"`` returns 0 for an account whose three saved images all appear
            under ``visibility="private"``.
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    body: dict[str, Any] = {
        "page": 0,
        "page_size": -1,
        "filter": {
            "source": "SOURCE_PRIVATE",
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
    return [_image_from_api(item) for item in items]


def get_image_detail(
    image_id: str,
    session: Optional[WebSession] = None,
) -> CustomImageInfo:
    """Get detailed image information.

    Args:
        image_id: The image ID to look up.
        session: Existing web session.
    """
    session, workspace_id = _get_session_and_workspace_id(workspace_id=None, session=session)

    for source in ("personal-visible", "official", "public"):
        for image in list_images_by_source(
            source=source,
            workspace_id=workspace_id,
            session=session,
        ):
            if image.image_id == image_id:
                return image
    raise ValueError(f"Image {image_id!r} was not found in the visible image catalogs")


def create_image(
    name: str,
    version: str,
    workspace_id: Optional[str] = None,
    description: str = "",
    visibility: str = "VISIBILITY_PRIVATE",
    add_method: str = "DIRECT_PUSH",
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Register a custom Docker image.

    Args:
        name: Image name (lowercase, digits, dashes, dots, underscores).
        version: Image version tag (max 64 chars).
        workspace_id: Workspace ID (determines registry).
        description: Optional description.
        visibility: ``"VISIBILITY_PRIVATE"`` or ``"VISIBILITY_PUBLIC"``.
        add_method: ``"DIRECT_PUSH"`` (create a slot, then ``docker push``).
        session: Existing web session.

    Returns:
        API response data (contains ``image`` dict with ``image_id``).
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    body: dict[str, Any] = {
        "name": name,
        "version": version,
        "registry_hint": {"workspace_id": workspace_id},
        "visibility": visibility,
        "add_method": add_method,
        "description": description,
    }

    return _request_notebooks_data(
        session,
        "POST",
        "/image/create",
        body=body,
        timeout=30,
        default_data={},
    )


def get_image_robot_credentials(
    *,
    image_name: str,
    image_version: str,
    workspace_id: Optional[str] = None,
    robot_name: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Fetch registry robot credentials/example commands for direct image push."""
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )
    generated_robot_name = not robot_name
    if generated_robot_name:
        from inspire.platform.web.browser_api.jobs import get_current_user

        robot_name = str(get_current_user(session=session).get("id") or "")
    if not robot_name:
        raise ValueError("The current user has no id for registry robot credentials")

    body = {
        "robotName": robot_name,
        "imageName": image_name,
        "imageVersion": image_version,
        "workspaceId": workspace_id,
    }
    result = _request_notebooks_data(
        session,
        "POST",
        "/image/robot",
        body=body,
        timeout=30,
        default_data={},
    )
    if generated_robot_name and not result:
        body = {**body, "robotName": f"{robot_name}-{uuid4().hex[:12]}"}
        result = _request_notebooks_data(
            session,
            "POST",
            "/image/robot",
            body=body,
            timeout=30,
            default_data={},
        )
    return result


def save_notebook_as_image(
    notebook_id: str,
    name: str,
    version: str = "v1",
    description: str = "",
    session: Optional[WebSession] = None,
    accessible: int = 1,
) -> dict[str, Any]:
    """Save a running notebook's state as a custom Docker image.

    Uses the ``/mirror/save`` endpoint.

    ``accessible`` is 1 (personal) or 2 (public). We used to omit it, and the platform then filed the image
    under **public** - a saved personal environment, visible to the whole institution, from a command that
    sounded private. A visibility default must be ours to choose and it must be the narrow one, so we always
    send it.

    Args:
        notebook_id: ID of the running notebook.
        name: Name for the saved image.
        version: Version tag (default ``"v1"``).
        description: Optional description.
        session: Existing web session.

    Returns:
        API response data.
    """
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    body: dict[str, Any] = {
        "notebook_id": notebook_id,
        "name": name,
        "version": version,
        "description": description,
        "accessible": 2 if int(accessible) == 2 else 1,
    }

    return _request_notebooks_data(
        session,
        "POST",
        "/mirror/save",
        body=body,
        timeout=60,
        default_data={},
    )


def delete_image(
    image_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Delete a custom Docker image.

    Args:
        image_id: ID of the image to delete.
        session: Existing web session.

    Returns:
        API response data.
    """
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    return _request_notebooks_data(
        session,
        "DELETE",
        f"/image/{image_id}",
        timeout=30,
        default_data={},
    )


def sync_image(
    *,
    workspace_id: str,
    image_name: str,
    image_version: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Trigger platform image sync."""
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id,
        session=session,
    )

    body = {
        "workspaceId": workspace_id,
        "imageName": image_name,
        "imageVersion": image_version,
    }
    return _request_notebooks_data(
        session,
        "POST",
        "/image/sync",
        body=body,
        timeout=60,
        default_data={},
    )


def preheat_image(
    *,
    name: str,
    version: str,
    logic_compute_group_id: str,
    address: str = "",
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Preheat/precache an image on a logic compute group."""
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)
    body: dict[str, Any] = {
        "name": name,
        "version": version,
        "logic_compute_group_id": logic_compute_group_id,
    }
    if address:
        body["address"] = address
    return _request_notebooks_data(
        session,
        "POST",
        "/image/preheat",
        body=body,
        timeout=60,
        default_data={},
    )


def wait_for_image_ready(
    image_id: str,
    session: Optional[WebSession] = None,
    timeout: int = 300,
    poll_interval: int = 5,
) -> CustomImageInfo:
    """Wait for a custom image to reach a usable status (READY or SUCCESS).

    Args:
        image_id: The image ID to poll.
        session: Existing web session.
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between polls.

    Raises:
        TimeoutError: If the image does not become ready in time.
        ValueError: If the image build fails.
    """
    if session is None:
        session = get_web_session()

    start = time.time()
    last_status = None

    while True:
        image = get_image_detail(image_id=image_id, session=session)
        status = (image.status or "").upper()
        if status:
            last_status = status

        if status in IMAGE_READY_STATUSES:
            return image

        if status in IMAGE_FAILED_STATUSES:
            raise ValueError(f"Image '{image_id}' build failed (status: {status})")

        if time.time() - start >= timeout:
            raise TimeoutError(
                f"Image '{image_id}' did not reach a usable status "
                f"({'/'.join(sorted(IMAGE_READY_STATUSES))}) within {timeout}s "
                f"(last status: {last_status or 'unknown'})"
            )

        time.sleep(poll_interval)


__all__ = [
    "CustomImageInfo",
    "IMAGE_FAILED_STATUSES",
    "IMAGE_READY_STATUSES",
    "create_image",
    "get_image_robot_credentials",
    "delete_image",
    "get_image_detail",
    "list_images_by_source",
    "list_private_images",
    "preheat_image",
    "save_notebook_as_image",
    "sync_image",
    "wait_for_image_ready",
]
