"""Jupyter server helpers shared by terminal and upload modules."""

from __future__ import annotations

from typing import Any


def _jupyter_server_base(lab_url: str) -> str:
    """Derive the Jupyter server base URL from a lab frame URL.

    Only strips ``/lab`` when it is the **final** path segment (the
    JupyterLab UI route), not when ``/lab/`` appears mid-path as part
    of the platform's proxy path (e.g. ``/api/v1/notebook/lab/{id}/``).
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(lab_url)
    path = parts.path.rstrip("/")
    if path.endswith("/lab"):
        path = path[:-4]
    if not path.endswith("/"):
        path = path + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _build_jupyter_xsrf_headers(context: Any) -> dict[str, str]:
    """Return Jupyter XSRF headers from browser context cookies (best-effort)."""
    headers: dict[str, str] = {}
    try:
        for cookie in context.cookies():
            if cookie.get("name") == "_xsrf":
                headers["X-XSRFToken"] = cookie["value"]
                break
    except (AttributeError, KeyError, TypeError):
        pass
    return headers


def _extract_jupyter_token(lab_url: str) -> str | None:
    from urllib.parse import parse_qs, urlsplit

    parsed = urlsplit(lab_url)
    query_token = parse_qs(parsed.query).get("token", [None])[0]
    if query_token:
        return query_token

    path_parts = [part for part in parsed.path.split("/") if part]
    try:
        jupyter_index = path_parts.index("jupyter")
        if len(path_parts) > jupyter_index + 2:
            return path_parts[jupyter_index + 2]
    except ValueError:
        return None
    return None
