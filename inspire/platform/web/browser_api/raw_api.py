"""Raw browser-API passthrough for endpoints the CLI has not wrapped.

Every wrapped domain module hard-codes three things: the path, the envelope to unwrap, and the
referer of the console page that normally issues the call. A caller reaching an unwrapped endpoint
has to supply those itself, and must not have to reimplement session handling to do it. This module
keeps only the parts that cannot be guessed - the configured base URL, the browser-API prefix, and
``_request_json`` (so auth, the 401 re-auth, and the bounded 429 retry policy are the same ones the
wrapped commands use) - and returns the platform envelope untouched.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

from inspire.platform.web.session import WebSession, get_web_session, request_json

from .core import _browser_api_path, _get_base_url
from .errors import ensure_browser_api_success


def _request_json(
    session: WebSession,
    method: str,
    path: str,
    *,
    referer: str,
    body: Optional[dict[str, Any] | list[Any]] = None,
    timeout: int = 30,
) -> Any:
    """Send the caller's path verbatim instead of applying the wrapped v2 route adapters."""
    return request_json(
        session,
        method,
        f"{_get_base_url()}{path}",
        headers={"Referer": referer},
        body=body,
        timeout=timeout,
    )


HTTP_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "DELETE")

__all__ = [
    "HTTP_METHODS",
    "merge_query_params",
    "normalize_method",
    "normalize_path",
    "raw_request",
]


def normalize_method(method: str) -> str:
    """Upper-case an HTTP method and reject what the session layer cannot send.

    Raised here rather than inside ``request_json`` so a typo reads as a bad argument to this
    command, not as an API failure.
    """
    candidate = str(method or "").strip().upper()
    if candidate not in HTTP_METHODS:
        allowed = ", ".join(HTTP_METHODS)
        raise ValueError(f"Unsupported HTTP method: {method!r} (expected one of: {allowed})")
    return candidate


def normalize_path(path: str) -> str:
    """Resolve a caller-supplied path against the configured browser-API prefix.

    A path that already carries an ``/api/`` prefix is taken verbatim, because that is how the
    console spells its v1 and v2 endpoints. Anything else is shorthand - ``train?Action=ListJobs`` -
    and is resolved through ``_browser_api_path`` exactly as a wrapped module resolves its own path.
    A query string already present on the path is kept.
    """
    endpoint = str(path or "").strip()
    if not endpoint:
        raise ValueError("API path must not be empty")
    if not endpoint.startswith("/"):
        endpoint = f"/{endpoint}"
    if endpoint.startswith("/api/"):
        return endpoint
    return _browser_api_path(endpoint)


def merge_query_params(path: str, params: Optional[dict[str, Any]]) -> str:
    """Append ``params`` to the query string ``path`` already carries."""
    if not params:
        return path
    encoded = urlencode(params, doseq=True)
    parts = urlsplit(path)
    query = f"{parts.query}&{encoded}" if parts.query else encoded
    return urlunsplit(("", "", parts.path, query, ""))


def _ensure_platform_success(payload: Any, *, action: str) -> None:
    """Raise with the platform's own code and message when the envelope reports a failure.

    The response body itself is left intact for the caller to print: this only decides that a
    body which says "error" must not exit zero.
    """
    if not isinstance(payload, dict):
        return

    metadata = payload.get("ResponseMetadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("Error"), dict):
        error = metadata["Error"]
        code = error.get("Code") or "Error"
        message = error.get("Message") or "unknown error"
        raise ValueError(f"{action} failed: {code}: {message}")

    if "code" in payload:
        ensure_browser_api_success(payload, action=action)


def raw_request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any] | list[Any]] = None,
    session: Optional[WebSession] = None,
    timeout: int = 30,
) -> Any:
    """Send one browser-API request by hand and return the platform's response untouched.

    ``path`` accepts a full ``/api/...`` path or a prefix-relative shorthand; ``params`` is merged
    into the query string and ``body`` is sent as the JSON request body.
    """
    resolved_method = normalize_method(method)
    resolved_path = merge_query_params(normalize_path(path), params)

    if session is None:
        session = get_web_session()

    payload = _request_json(
        session,
        resolved_method,
        resolved_path,
        referer=_get_base_url(),
        body=body,
        timeout=timeout,
    )
    _ensure_platform_success(payload, action=f"{resolved_method} {resolved_path}")
    return payload
