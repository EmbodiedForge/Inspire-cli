"""Fast-path probe for notebook rtunnel reachability."""

from __future__ import annotations

from typing import Optional

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url
from inspire.platform.web.session import WebSession, build_requests_session


def probe_existing_rtunnel_proxy_url(
    *,
    notebook_id: str,
    port: int,
    session: WebSession,
) -> str | None:
    """Return the existing proxy URL if it looks reachable (otherwise None)."""
    base_url = _get_base_url()
    notebook_lab_path = _browser_api_path(f"/notebook/lab/{notebook_id}/proxy/{port}/")
    known_proxy_url = f"{base_url}{notebook_lab_path}"

    http: Optional[object] = None
    try:
        http = build_requests_session(session, base_url)
        resp = http.get(known_proxy_url, timeout=5)  # type: ignore[attr-defined]
        body = resp.text[:200] if resp.text else ""  # type: ignore[attr-defined]
        if resp.status_code != 200:  # type: ignore[attr-defined]
            return None
        if "ECONNREFUSED" in body:
            return None
        if "<html>" in body.lower():
            return None
        return known_proxy_url
    except Exception:
        return None
    finally:
        try:
            if http is not None:
                http.close()  # type: ignore[attr-defined]
        except Exception:
            pass
