"""HTTP client for the SII data-square portal (aip.sii.edu.cn).

The compute platform's ``session.request_json`` is deliberately NOT reused. Its 401 handler
re-authenticates through CAS with the compute platform's stored credentials, which is both
pointless and wrong here: the data square answers with its own 401 for its own cookie, and the
only correct next step is a fresh browser login on that portal. So the caller is handed a
``DatasquareSessionExpired`` and nothing else happens behind its back.

Every response arrives in the portal's own envelope - ``{"code": 0, "data": ..., "msg": ...}`` -
so the unwrap and the failure check live here once.
"""

from __future__ import annotations

from typing import Any, Optional

from inspire.platform.web.session import WebSession, build_requests_session
from inspire.platform.web.session.models import SessionExpiredError

from .session import get_datasquare_base_url

#: The portal's own "you are not logged in" body code. It arrives with an HTTP 200, so only the
#: code distinguishes it from a real success.
DATASQUARE_CODE_UNAUTHORIZED = 7

_SUPPORTED_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "DELETE")


class DatasquareSessionExpired(SessionExpiredError):
    """The data-square session was rejected; only a fresh browser login fixes it."""


class DatasquareAPIError(RuntimeError):
    """The data square answered its envelope with a non-zero ``code``."""

    def __init__(self, code: Any, msg: Any, *, path: str) -> None:
        self.code = code
        self.msg = str(msg or "")
        self.path = path
        super().__init__(f"Datasquare API error {code} for {path}: {self.msg}")


def datasquare_request_json(
    session: WebSession,
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
    timeout: int = 30,
) -> Any:
    """Send one data-square request and return the envelope's ``data``.

    Raises ``DatasquareSessionExpired`` on HTTP 401, ``ValueError`` on any other HTTP error or a
    body that is not JSON, and ``DatasquareAPIError`` when the envelope reports a failure.
    """
    candidate = str(method or "").strip().upper()
    if candidate not in _SUPPORTED_METHODS:
        allowed = ", ".join(_SUPPORTED_METHODS)
        raise ValueError(f"Unsupported HTTP method: {method!r} (expected one of: {allowed})")

    url = f"{get_datasquare_base_url()}{path if path.startswith('/') else '/' + path}"
    http = build_requests_session(session, url)
    try:
        response = http.request(
            candidate,
            url,
            params=params,
            json=body if candidate != "GET" else None,
            timeout=timeout,
        )
        if response.status_code == 401:
            raise DatasquareSessionExpired(
                "Datasquare session expired or invalid (HTTP 401); log in again on aip.sii.edu.cn"
            )
        if response.status_code >= 400:
            raise ValueError(f"Datasquare API returned {response.status_code}: {response.text}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(
                f"Datasquare API returned a non-JSON response: {response.text[:200]}"
            ) from exc
    finally:
        http.close()

    if not isinstance(payload, dict):
        raise ValueError(f"Datasquare API returned an unexpected envelope: {payload!r}")

    code = payload.get("code")
    if code != 0:
        raise DatasquareAPIError(code, payload.get("msg"), path=path)
    return payload.get("data")


__all__ = [
    "DATASQUARE_CODE_UNAUTHORIZED",
    "DatasquareAPIError",
    "DatasquareSessionExpired",
    "datasquare_request_json",
]
