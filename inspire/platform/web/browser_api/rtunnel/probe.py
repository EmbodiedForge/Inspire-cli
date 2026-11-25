"""Fast-path probe: check whether an existing rtunnel proxy is already reachable."""

from __future__ import annotations

import concurrent.futures
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
from .verify import probe_rtunnel_proxy_once

_PROXY_PORT_PATTERN = re.compile(r"/proxy/\d+/")


def _rewrite_proxy_port(proxy_url: str, port: int) -> str:
    if f"/proxy/{port}/" in proxy_url:
        return proxy_url
    if _PROXY_PORT_PATTERN.search(proxy_url):
        return _PROXY_PORT_PATTERN.sub(f"/proxy/{port}/", proxy_url, count=1)
    return proxy_url


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


def _probe_single_url(session: WebSession, base_url: str, url: str) -> str | None:
    try:
        http = build_requests_session(session, base_url)
        try:
            ready, _summary = probe_rtunnel_proxy_once(
                proxy_url=url,
                session=http,
                request_timeout_ms=5000,
            )
            if ready:
                return url
        finally:
            http.close()
    except Exception:
        pass
    return None


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
    if not deduped_urls:
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(deduped_urls), 8)) as pool:
        futures = [pool.submit(_probe_single_url, session, base_url, url) for url in deduped_urls]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                try:
                    save_rtunnel_proxy_state(
                        notebook_id=notebook_id,
                        proxy_url=result,
                        port=port,
                        ssh_port=22222,
                        base_url=base_url,
                        account=resolved_account,
                    )
                except OSError:
                    pass
                return result

    return None
