"""Fast-path probe: check whether an existing rtunnel proxy is already reachable."""

from __future__ import annotations

import re
from typing import Optional

from inspire.bridge.tunnel import load_tunnel_config
from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url
from inspire.platform.web.session import WebSession, build_requests_session

from .state import (
    DEFAULT_PROXY_CACHE_TTL_SECONDS,
    get_cached_rtunnel_proxy_candidates,
    save_rtunnel_proxy_state,
)

_PROXY_PORT_PATTERN = re.compile(r"/proxy/\d+/")


def _rewrite_proxy_port(proxy_url: str, port: int) -> str:
    if f"/proxy/{port}/" in proxy_url:
        return proxy_url
    if _PROXY_PORT_PATTERN.search(proxy_url):
        return _PROXY_PORT_PATTERN.sub(f"/proxy/{port}/", proxy_url, count=1)
    return proxy_url


def _is_reachable_proxy_response(*, status_code: int, body: str) -> bool:
    text = (body or "").strip().lower()

    if status_code == 200:
        if "econnrefused" in text or "connection refused" in text:
            return False
        if "<html" in text:
            return False
        return True

    return False


def _candidate_urls_from_tunnel_config(
    *,
    notebook_id: str,
    port: int,
    account: Optional[str],
) -> list[str]:
    try:
        config = load_tunnel_config(account=account)
    except (OSError, ValueError, TypeError):
        return []

    candidates: list[str] = []
    for bridge in config.bridges.values():
        proxy_url = str(getattr(bridge, "proxy_url", "") or "")
        if notebook_id not in proxy_url or "/proxy/" not in proxy_url:
            continue
        candidates.append(_rewrite_proxy_port(proxy_url, port))
    return candidates


def probe_existing_rtunnel_proxy_url(
    *,
    notebook_id: str,
    port: int,
    session: WebSession,
    candidate_urls: Optional[list[str]] = None,
    account: Optional[str] = None,
    cache_ttl_seconds: int = DEFAULT_PROXY_CACHE_TTL_SECONDS,
) -> str | None:
    """Return the existing proxy URL if it looks reachable (otherwise None)."""
    base_url = _get_base_url().rstrip("/")
    notebook_lab_path = _browser_api_path(f"/notebook/lab/{notebook_id}/proxy/{port}/")
    known_proxy_url = f"{base_url}{notebook_lab_path}"

    resolved_account = account or session.login_username
    urls: list[str] = [known_proxy_url]
    if candidate_urls:
        urls.extend(candidate_urls)
    urls.extend(
        get_cached_rtunnel_proxy_candidates(
            notebook_id=notebook_id,
            port=port,
            base_url=base_url,
            account=resolved_account,
            ttl_seconds=cache_ttl_seconds,
        )
    )
    urls.extend(
        _candidate_urls_from_tunnel_config(
            notebook_id=notebook_id,
            port=port,
            account=resolved_account,
        )
    )
    deduped_urls = list(dict.fromkeys(urls))

    http: Optional[object] = None
    try:
        http = build_requests_session(session, base_url)
        for url in deduped_urls:
            try:
                resp = http.get(url, timeout=5)  # type: ignore[attr-defined]
            except (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError):
                continue
            body = resp.text[:400] if getattr(resp, "text", "") else ""  # type: ignore[attr-defined]
            if not _is_reachable_proxy_response(status_code=resp.status_code, body=body):  # type: ignore[attr-defined]
                continue
            try:
                save_rtunnel_proxy_state(
                    notebook_id=notebook_id,
                    proxy_url=url,
                    port=port,
                    ssh_port=22222,
                    base_url=base_url,
                    account=resolved_account,
                )
            except OSError:
                pass
            return url
        return None
    except (OSError, ValueError, RuntimeError, AttributeError):
        return None
    finally:
        try:
            if http is not None:
                http.close()  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            pass
