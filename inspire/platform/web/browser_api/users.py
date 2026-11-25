"""Current-user diagnostics over the platform's browser API."""

from __future__ import annotations

from typing import Any, Optional

from inspire.platform.web.session import WebSession, get_web_session

from .core import _browser_api_path, _get_base_url, _request_json
from .errors import ensure_browser_api_success, get_browser_api_action_result


def get_my_permissions(
    *, workspace_id: str | None = None, session: Optional[WebSession] = None
) -> dict[str, Any]:
    """Return global v2 permissions or one workspace-specific v1 permission set."""
    if session is None:
        session = get_web_session()
    if workspace_id:
        payload = _request_json(
            session,
            "GET",
            _browser_api_path(f"/user/permissions/{workspace_id}"),
            referer=f"{_get_base_url()}/userCenter",
            timeout=30,
        )
        ensure_browser_api_success(payload, action="Get workspace permissions")
        data = payload.get("data", {})
        return data if isinstance(data, dict) else {"permissions": data}

    payload = _request_json(
        session,
        "POST",
        "/api/v2/user?Action=GetMyPermissions",
        referer=f"{_get_base_url()}/userCenter",
        timeout=30,
    )
    return get_browser_api_action_result(payload, action="Get my permissions")


def list_ssh_public_keys(
    *, page: int = 1, page_size: int = 100, session: Optional[WebSession] = None
) -> dict[str, Any]:
    """List SSH public-key metadata without fetching private key material."""
    if session is None:
        session = get_web_session()
    payload = _request_json(
        session,
        "POST",
        _browser_api_path("/ssh/list"),
        referer=f"{_get_base_url()}/userCenter?tab=sshkey",
        body={"page": page, "page_size": page_size},
        timeout=30,
    )
    ensure_browser_api_success(payload, action="List SSH public keys")
    data = payload.get("data", {})
    return data if isinstance(data, dict) else {"items": data}


__all__ = ["get_my_permissions", "list_ssh_public_keys"]
