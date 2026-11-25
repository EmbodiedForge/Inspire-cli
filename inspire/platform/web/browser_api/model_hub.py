"""Browser (web-session) Model Hub APIs."""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _request_json,
)
from inspire.platform.web.browser_api.errors import ensure_browser_api_success
from inspire.platform.web.session import WebSession, get_web_session


def _model_hub_referer() -> str:
    return f"{_get_base_url()}/model"


def _request_model_hub_data(
    session: WebSession,
    method: str,
    endpoint_path: str,
    *,
    body: Optional[dict[str, Any]] = None,
    timeout: int = 30,
    default_data: Any = None,
    action: str = "Model Hub request",
) -> Any:
    data = _request_json(
        session,
        method,
        _browser_api_path(endpoint_path),
        referer=_model_hub_referer(),
        body=body,
        timeout=timeout,
    )
    ensure_browser_api_success(data, action=action)
    return data.get("data", default_data)


def list_model_hub_models(
    *,
    workspace_id: str,
    page: int = 1,
    page_size: int = 20,
    filter_by: Optional[dict[str, Any]] = None,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """List Model Hub repository models visible to the current account."""
    if session is None:
        session = get_web_session()

    data = _request_model_hub_data(
        session,
        "POST",
        "/model/list",
        body={
            "page": page,
            "page_size": page_size,
            "filter_by": filter_by or {},
            "workspace_id": workspace_id,
        },
        timeout=30,
        default_data={},
        action="List Model Hub models",
    )
    return data if isinstance(data, dict) else {}


def get_model_hub_detail(
    model_id: str,
    *,
    version: str | int | None = None,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Fetch Model Hub detail for a model or a specific model version."""
    if session is None:
        session = get_web_session()

    if version not in (None, ""):
        data = _request_model_hub_data(
            session,
            "POST",
            "/model/detail",
            body={"model_id": model_id, "version": version},
            timeout=30,
            default_data={},
            action="Get Model Hub version detail",
        )
    else:
        safe_model_id = quote(model_id, safe="")
        data = _request_model_hub_data(
            session,
            "GET",
            f"/model/{safe_model_id}",
            timeout=30,
            default_data={},
            action="Get Model Hub detail",
        )
    return data if isinstance(data, dict) else {}


def list_model_hub_versions(
    model_id: str,
    *,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """List versions for one Model Hub repository model."""
    if session is None:
        session = get_web_session()

    safe_model_id = quote(model_id, safe="")
    data = _request_model_hub_data(
        session,
        "GET",
        f"/model/{safe_model_id}/versions",
        timeout=30,
        default_data={},
        action="List Model Hub versions",
    )
    return data if isinstance(data, dict) else {}


def list_model_hub_projects(*, session: Optional[WebSession] = None) -> dict[str, Any]:
    """List projects available to the Model Hub form."""
    if session is None:
        session = get_web_session()

    data = _request_model_hub_data(
        session,
        "GET",
        "/model/projects",
        timeout=30,
        default_data={},
        action="List Model Hub projects",
    )
    return data if isinstance(data, dict) else {}


def list_model_hub_tags(*, session: Optional[WebSession] = None) -> list[Any]:
    """List Model Hub tags."""
    if session is None:
        session = get_web_session()

    data = _request_model_hub_data(
        session,
        "POST",
        "/model/tags",
        body={},
        timeout=30,
        default_data=[],
        action="List Model Hub tags",
    )
    return data if isinstance(data, list) else []


def list_model_hub_users(*, session: Optional[WebSession] = None) -> dict[str, Any]:
    """List Model Hub users/creators for the form filter."""
    if session is None:
        session = get_web_session()

    data = _request_model_hub_data(
        session,
        "POST",
        "/model/users",
        body={},
        timeout=30,
        default_data={},
        action="List Model Hub users",
    )
    return data if isinstance(data, dict) else {}


def list_model_hub_related_servings(
    model_id: str,
    *,
    version: str | int | None = None,
    page: int = 1,
    page_size: int = 20,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """List inference servings linked to a Model Hub model/version."""
    if session is None:
        session = get_web_session()

    body: dict[str, Any] = {
        "model_id": model_id,
        "page": page,
        "page_size": page_size,
    }
    if version not in (None, ""):
        body["version"] = version
    data = _request_model_hub_data(
        session,
        "POST",
        "/model/inference_servings",
        body=body,
        timeout=30,
        default_data={},
        action="List Model Hub related servings",
    )
    return data if isinstance(data, dict) else {}


def check_model_hub_pending_serving(
    model_id: str,
    *,
    version: str | int | None = None,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Check whether pending inference servings block model/version deletion."""
    if session is None:
        session = get_web_session()

    body: dict[str, Any] = {"model_id": model_id}
    if version not in (None, ""):
        body["version"] = version
    data = _request_model_hub_data(
        session,
        "POST",
        "/model/inference_serving/pending",
        body=body,
        timeout=30,
        default_data={},
        action="Check Model Hub pending serving blockers",
    )
    return data if isinstance(data, dict) else {}


__all__ = [
    "check_model_hub_pending_serving",
    "get_model_hub_detail",
    "list_model_hub_projects",
    "list_model_hub_related_servings",
    "list_model_hub_models",
    "list_model_hub_tags",
    "list_model_hub_users",
    "list_model_hub_versions",
]
