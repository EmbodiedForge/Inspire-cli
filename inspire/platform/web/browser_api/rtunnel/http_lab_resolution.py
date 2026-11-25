"""Pure HTTP JupyterLab URL resolution (no browser required)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url
from inspire.platform.web.browser_api.rtunnel.logging import trace_event, update_trace_summary
from inspire.platform.web.session import WebSession, build_requests_session, get_web_session
from inspire.platform.web.session.models import SessionExpiredError

_log = logging.getLogger("inspire.platform.web.browser_api.rtunnel.http_lab_resolution")

_LAB_RESOLUTION_MAX_ATTEMPTS = 6
_LAB_RESOLUTION_INITIAL_BACKOFF_S = 0.5
_LAB_RESOLUTION_MAX_BACKOFF_S = 4.0
_LAB_RESOLUTION_REQUEST_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class _LabUrlProbe:
    url: str | None = None
    reason: str = ""
    retryable: bool = True


def resolve_lab_url_http(
    notebook_id: str,
    session: WebSession,
    timeout: int = 60,
) -> str:
    """Resolve the JupyterLab URL for a notebook using pure HTTP.

    1. Queries the notebook API for ``jupyter_url`` / ``proxy_uri``.
    2. If the API returns a URL, validates it looks like a lab URL and returns it.
    3. Otherwise performs a GET on the configured
       ``_browser_api_path("/notebook/lab/{id}")`` route with
       ``allow_redirects=True`` and parses the final URL after all redirects.

    Returns:
        The resolved lab URL string (e.g. ``https://…/lab?token=…``).

    Raises:
        RuntimeError: if the lab URL cannot be resolved through any path.
        SessionExpiredError: if the server returns 401.
    """
    base_url = _get_base_url().rstrip("/")
    timeout_s = max(float(timeout), 0.1)
    started = time.monotonic()
    deadline = started + timeout_s
    attempts = 0
    last_api_reason = "not attempted"
    last_redirect_reason = "not attempted"
    notebook_lab_pattern = _browser_api_path("/notebook/lab/")

    while attempts < _LAB_RESOLUTION_MAX_ATTEMPTS:
        remaining_s = deadline - time.monotonic()
        if attempts > 0 and remaining_s <= 0:
            break
        attempts += 1
        request_timeout_s = max(
            0.1,
            min(_LAB_RESOLUTION_REQUEST_TIMEOUT_S, max(remaining_s, 0.1)),
        )

        # ── 1. API fast-path ────────────────────────────────────────────────
        api_probe = _try_api_for_jupyter_url(
            notebook_id,
            base_url,
            session,
            timeout=request_timeout_s,
        )
        if api_probe.url and _is_lab_like_url(
            api_probe.url,
            notebook_lab_pattern=notebook_lab_pattern,
        ):
            trace_event(
                "lab_resolution_http_api",
                notebook_id=notebook_id,
                lab_url=api_probe.url,
                attempt=attempts,
            )
            update_trace_summary(lab_resolution="http_api_direct")
            return api_probe.url
        last_api_reason = (
            "notebook API returned a non-Lab URL" if api_probe.url else api_probe.reason
        )

        # ── 2. HTTP redirect path ───────────────────────────────────────────
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            last_redirect_reason = "resolution deadline expired before redirect probe"
            break
        trace_event(
            "lab_resolution_http_redirect",
            notebook_id=notebook_id,
            attempt=attempts,
        )
        redirect_probe = _try_http_redirect_for_lab_url(
            notebook_id,
            base_url,
            session,
            timeout=max(0.1, min(_LAB_RESOLUTION_REQUEST_TIMEOUT_S, remaining_s)),
        )
        if redirect_probe.url:
            trace_event(
                "lab_resolution_http_redirect_ok",
                notebook_id=notebook_id,
                lab_url=redirect_probe.url,
                attempt=attempts,
            )
            update_trace_summary(lab_resolution="http_redirect")
            return redirect_probe.url
        last_redirect_reason = redirect_probe.reason

        if not redirect_probe.retryable or attempts >= _LAB_RESOLUTION_MAX_ATTEMPTS:
            break
        remaining_s = deadline - time.monotonic()
        backoff_s = min(
            _LAB_RESOLUTION_INITIAL_BACKOFF_S * (2 ** (attempts - 1)),
            _LAB_RESOLUTION_MAX_BACKOFF_S,
            max(remaining_s, 0.0),
        )
        if backoff_s <= 0:
            break
        trace_event(
            "lab_resolution_http_retry",
            notebook_id=notebook_id,
            attempt=attempts,
            delay_s=round(backoff_s, 3),
            api_reason=last_api_reason,
            redirect_reason=last_redirect_reason,
        )
        time.sleep(backoff_s)

    elapsed_s = max(time.monotonic() - started, 0.0)
    raise RuntimeError(
        f"Failed to resolve JupyterLab URL for notebook {notebook_id} via HTTP "
        f"after {attempts} attempt(s) over {elapsed_s:.1f}s. "
        f"Last notebook API result: {last_api_reason}. "
        f"Last redirect result: {last_redirect_reason}."
    )


def _try_api_for_jupyter_url(
    notebook_id: str,
    base_url: str,
    session: WebSession,
    *,
    timeout: float,
) -> _LabUrlProbe:
    """Probe the notebook detail API for a Jupyter URL."""
    try:
        from inspire.platform.web.browser_api.core import _request_json as browser_request

        data = browser_request(
            session,
            "GET",
            f"/api/v1/notebook/{notebook_id}",
            referer=f"{base_url}/jobs/interactiveModeling",
            timeout=max(int(timeout), 5),
        )
        notebook_data = data.get("data", {})
        jupyter_url = notebook_data.get("jupyter_url") or notebook_data.get("proxy_uri")
        if jupyter_url:
            trace_event(
                "lab_api_jupyter_url_found", notebook_id=notebook_id, jupyter_url=jupyter_url
            )
            return _LabUrlProbe(url=str(jupyter_url))
        status = str(notebook_data.get("status") or "").strip().upper()
        status_suffix = f" (status={status})" if status else ""
        return _LabUrlProbe(reason=f"notebook API returned no jupyter_url/proxy_uri{status_suffix}")
    except SessionExpiredError:
        raise
    except Exception as exc:
        trace_event("lab_api_jupyter_url_failed", notebook_id=notebook_id, error=str(exc))
        return _LabUrlProbe(reason=f"notebook API request failed ({type(exc).__name__})")


def _try_http_redirect_for_lab_url(
    notebook_id: str,
    base_url: str,
    session: WebSession,
    timeout: float = 60,
) -> _LabUrlProbe:
    """Follow ``/notebook/lab/{id}`` redirects and classify failures."""
    try:
        http = build_requests_session(session, base_url)
        try:
            lab_path = _browser_api_path(f"/notebook/lab/{notebook_id}")
            resp = http.get(
                f"{base_url.rstrip('/')}{lab_path}",
                allow_redirects=True,
                timeout=timeout,
                headers={"Accept": "text/html,application/json,*/*"},
            )

            if resp.status_code == 401:
                raise SessionExpiredError("Session expired or invalid")
            if resp.status_code >= 400:
                trace_event(
                    "lab_http_redirect_error",
                    notebook_id=notebook_id,
                    status_code=resp.status_code,
                )
                retryable = resp.status_code in {404, 408, 409, 425, 429} or resp.status_code >= 500
                return _LabUrlProbe(
                    reason=f"redirect returned HTTP {resp.status_code}",
                    retryable=retryable,
                )

            final_url = resp.url
            if not final_url:
                return _LabUrlProbe(reason="redirect response had no final URL")

            if _is_lab_like_url(
                final_url, notebook_lab_pattern=_browser_api_path("/notebook/lab/")
            ):
                return _LabUrlProbe(url=final_url)

            trace_event(
                "lab_http_redirect_not_lab",
                notebook_id=notebook_id,
                final_url=final_url,
            )
            return _LabUrlProbe(reason="redirect ended at a non-Lab URL")
        finally:
            http.close()
    except SessionExpiredError:
        raise
    except Exception as exc:
        trace_event("lab_http_redirect_failed", notebook_id=notebook_id, error=str(exc))
        return _LabUrlProbe(reason=f"redirect request failed ({type(exc).__name__})")


def _is_lab_like_url(url: str, *, notebook_lab_pattern: str) -> bool:
    """Check if a URL looks like a JupyterLab URL."""
    value = str(url or "")
    if not value:
        return False

    parsed = urlsplit(value)
    path = parsed.path.rstrip("/")
    if "notebook-inspire" in parsed.netloc and path.endswith("/lab"):
        return True
    if notebook_lab_pattern.rstrip("/") in parsed.path and path.endswith("/lab"):
        return True
    if "/jupyter/" in parsed.path and path.endswith("/lab"):
        return True
    return False


def get_jupyter_token(
    notebook_id: str,
    session: Optional[WebSession] = None,
    timeout: int = 60,
) -> tuple[str, str, str]:
    """Resolve lab URL and extract (base_url, token, host).

    Returns:
        Tuple of (base_url, token, host) where base_url is the Jupyter server
        base URL (without /lab), token is the authentication token, and host
        is the hostname.

    Raises:
        RuntimeError: if the lab URL cannot be resolved.
        SessionExpiredError: if the server returns 401.
    """
    if session is None:
        session = get_web_session()

    lab_url = resolve_lab_url_http(notebook_id, session, timeout=timeout)
    parsed = urlsplit(lab_url)
    host = parsed.netloc

    # Derive base URL (strip /lab suffix)
    path = parsed.path.rstrip("/")
    if path.endswith("/lab"):
        path = path[:-4]
    if not path.endswith("/"):
        path = path + "/"
    base_url = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    # Extract token from query or path
    from urllib.parse import parse_qs

    query_token = parse_qs(parsed.query).get("token", [None])[0]
    if query_token:
        return base_url, query_token, host

    path_parts = [part for part in parsed.path.split("/") if part]
    try:
        jupyter_index = path_parts.index("jupyter")
        if len(path_parts) > jupyter_index + 2:
            return base_url, path_parts[jupyter_index + 2], host
    except ValueError:
        pass

    raise RuntimeError(f"Could not extract Jupyter token from lab URL: {lab_url}")
