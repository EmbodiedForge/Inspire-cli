"""Requests-based session helpers for web UI APIs."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import requests

from inspire.platform import tls_policy

from .models import WebSession


def _cookie_jar_from_session(
    session: WebSession, base_url: str
) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    base_host = urlsplit(base_url).hostname or ""

    storage_cookies = session.storage_state.get("cookies") if session.storage_state else None
    if storage_cookies:
        for cookie in storage_cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            # A pair with no name or no value renders as a header fragment ("=abc", "sid="), which
            # the platform answers with an auth failure that reads as an expired session. Dropping
            # it here means every consumer of the jar - the HTTP calls and the WebSocket header
            # below - sees only complete pairs.
            if not name or not value:
                continue
            domain = cookie.get("domain") or base_host
            path = cookie.get("path") or "/"
            jar.set(name, value, domain=domain, path=path)

    if not storage_cookies and session.cookies:
        for name, value in session.cookies.items():
            if not name or not value:
                continue
            jar.set(name, value, domain=base_host, path="/")

    return jar


def build_requests_session(session: WebSession, base_url: str) -> requests.Session:
    storage_cookies = session.storage_state.get("cookies") if session.storage_state else None
    if not storage_cookies and not session.cookies:
        raise ValueError("Session expired or invalid (missing storage state)")

    http = requests.Session()
    http.cookies.update(_cookie_jar_from_session(session, base_url))
    http.verify = tls_policy.verify_setting()
    if not http.verify:
        tls_policy.warn_insecure_channel("platform web API")
    http.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
    )
    return http


def cookie_header_for_url(http: requests.Session, url: str) -> str:
    """The Cookie header requests would send for ``url``: host-scoped, values untouched.

    A WebSocket handshake has no cookie jar, so the caller has to hand over a header. Walking
    ``http.cookies`` by hand - which is what the exec PTY and the notebook terminal transport used
    to do - sends the whole jar to the WS host whatever it is. On a measured live session that is
    eight of nine cookies - the CAS ticket-granting ticket ``CASTGC`` (``cas.sii.edu.cn``, ~800
    bytes), ``KEYCLOAK_IDENTITY``, the CAS and Keycloak session cookies - travelling to
    ``qz.sii.edu.cn``, which asked for exactly one cookie (``inspire-session``). Hand-rolling it also
    percent-encoded the values, which mangles any value holding ``%``, ``+`` or ``=`` -
    ``KC_AUTH_SESSION_HASH`` is one such cookie on that same live session.

    Letting requests prepare the request scopes the set exactly as the HTTP calls on the same jar
    already do, so the handshake carries what a browser would carry. ``ws``/``wss`` are mapped to
    their HTTP equivalents because requests decides "may I send a Secure cookie" by scheme.
    """
    parts = urlsplit(url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme.lower(), parts.scheme)
    scope_url = urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))
    prepared = http.prepare_request(requests.Request("GET", scope_url))
    return str(prepared.headers.get("Cookie") or "")
