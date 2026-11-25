#!/usr/bin/env python3
"""Debug-only helper for browser-backed Inspire web sessions.

This script intentionally lives under scripts/ instead of the public CLI.  It is
for live E2E/auth debugging when a human has already logged in through a browser
and we need to reuse that browser state without retrying CAS password login.

Two portals share this entry point and their caches never mix.  ``--portal qz``
(the default) is the compute platform: ``web_session-<account>.json``, verified
against ``POST /api/v2/user?Action=GetUserDetail``.  ``--portal aip`` is the data square:
``datasquare_session-<account>.json``, verified against its own dataset
catalogue endpoint, with the base URL taken from INSPIRE_DATASQUARE_BASE_URL
rather than the compute platform's ``[api].base_url``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from inspire.config import Config, ConfigError
from inspire.platform.web.datasquare.session import (
    DATASQUARE_BASE_URL_ENV,
    get_datasquare_base_url,
    get_datasquare_session,
    get_datasquare_session_cache_file,
    save_datasquare_session,
)
from inspire.platform.web.session import build_requests_session
from inspire.platform.web.session.auth import _session_matches_account_context
from inspire.platform.web.session.http_auth import refresh_with_requests
from inspire.platform.web.session.models import (
    DEFAULT_WORKSPACE_ID,
    SessionAuthenticationError,
    WebSession,
    get_session_cache_file,
)

PORTAL_QZ = "qz"
PORTAL_AIP = "aip"

#: A catalogue read that costs the portal the least while still proving the
#: ``datasets-session`` cookie was accepted.
DATASQUARE_VERIFY_PATH = "/api/datasets/getDatasetsList"
DATASQUARE_LOGIN_HINT = "node scripts/login_web_session.mjs --portal aip"


class DebugWebSessionError(RuntimeError):
    """Base error for script-controlled failures."""


class ConfigDebugError(DebugWebSessionError):
    """Config/account/base-url failure."""


class ValidationDebugError(DebugWebSessionError):
    """Input file or storage-state shape failure."""


class ApiDebugError(DebugWebSessionError):
    """Cached browser session could not be verified against the platform."""


def _load_config(account_alias: str | None = None) -> Config:
    try:
        config, _ = Config.from_files_and_env(
            require_credentials=False,
            require_target_dir=False,
            account_alias=account_alias,
        )
        return config
    except ConfigError as exc:
        raise ConfigDebugError(str(exc)) from exc


def _resolve_account(config: Config, account: str | None) -> str:
    value = str(account or config.username or "").strip()
    if not value:
        raise ConfigDebugError(
            "Web session cache needs an account name. Set [auth].username / "
            "INSPIRE_USERNAME, or pass --account."
        )
    return value


def _load_qz_config(account: str | None) -> Config:
    if not account:
        return _load_config()
    try:
        return _load_config(account)
    except ConfigDebugError as selection_error:
        # Legacy configs can pass the configured platform username explicitly.
        try:
            config = _load_config()
        except ConfigDebugError:
            raise selection_error from None
        if config.account_alias or config.username != account:
            raise selection_error
        return config


def _resolve_datasquare_base_url(base_url: str | None, session: WebSession | None = None) -> str:
    """Resolve the data-square base URL without ever falling back to the compute platform's.

    ``[api].base_url``/``INSPIRE_BASE_URL`` point at qz, and a data-square request sent there
    reads as an auth failure rather than as the wrong host, so only an explicit ``--base-url``,
    INSPIRE_DATASQUARE_BASE_URL, or the cached session's own URL is trusted. The env override
    outranks the cache because that is the order ``datasquare_request_json`` itself uses - a
    check against any other host would not be predicting what the CLI does.
    """
    value = (
        str(base_url or "").strip()
        or str(os.environ.get(DATASQUARE_BASE_URL_ENV) or "").strip()
        or str((session.base_url if session is not None else "") or "").strip()
        or get_datasquare_base_url()
    )
    return value.rstrip("/")


def _resolve_base_url(
    config: Config, base_url: str | None, session: WebSession | None = None
) -> str:
    value = str(base_url or (session.base_url if session else "") or config.base_url or "")
    value = value.strip().rstrip("/")
    if not value:
        raise ConfigDebugError(
            "Web session check needs a base URL. Set [api].base_url / INSPIRE_BASE_URL, "
            "or pass --base-url."
        )
    return value


def _load_storage_state(state_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationDebugError(f"Browser state file not found: {state_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationDebugError(f"Browser state file is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValidationDebugError("Browser state file must contain a JSON object.")
    cookies = data.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise ValidationDebugError("Browser state file has no cookies.")
    return data


def _cookies_by_name(storage_state: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for cookie in storage_state.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if name:
            result[name] = value
    return result


def _extract_workspace_id(storage_state: dict[str, Any], base_url: str) -> str:
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        origin_url = str(origin.get("origin") or "").rstrip("/")
        if base_url.rstrip("/") and origin_url != base_url.rstrip("/"):
            continue
        for entry in origin.get("localStorage") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("name") == "spaceId":
                value = str(entry.get("value") or "").strip()
                if value:
                    return value
    return DEFAULT_WORKSPACE_ID


def _verify_web_session(session: WebSession, base_url: str) -> dict[str, Any]:
    """Verify the session against the live v2 Action API.

    The platform retired /api/v1 on 2026-10-15 (#82); ``GET /api/v1/user/detail``
    now 404s and used to make every import look like a bad login. v2 answers at
    ``POST /api/v2/user?Action=GetUserDetail`` and reports failures inside
    ``ResponseMetadata.Error`` while still returning HTTP 200.
    """
    from inspire.platform.web.session.http_auth import _v2_action

    http = build_requests_session(session, base_url)
    try:
        result = _v2_action(http, base_url.rstrip("/"), "user", "GetUserDetail")
        extra_info = result.get("extra_info")
        login_name = extra_info.get("login_name") if isinstance(extra_info, dict) else None
        return {
            "status": 200,
            "ok": True,
            "code": 0,
            "message": "verified via POST /api/v2/user?Action=GetUserDetail",
            "user": login_name or result.get("username"),
        }
    except Exception as exc:  # noqa: BLE001 - report any failure as "not verified"
        return {"status": None, "ok": False, "message": str(exc)[:200]}
    finally:
        http.close()


def _verify_datasquare_session(session: WebSession, base_url: str) -> dict[str, Any]:
    """Ask the data square for one catalogue row; 200 with envelope ``code == 0`` means logged in.

    The portal answers a missing cookie with HTTP 401, and an expired one with HTTP 200 carrying
    ``{"code": 7, "msg": "未登录或非法访问"}``, so the status alone is never the verdict.
    """
    http = build_requests_session(session, base_url)
    try:
        response = http.get(
            f"{base_url.rstrip('/')}{DATASQUARE_VERIFY_PATH}?page=1&pageSize=1",
            headers={
                "Accept": "application/json",
                "Referer": f"{base_url.rstrip('/')}/",
            },
            timeout=30,
        )
        payload: dict[str, Any] = {"status": response.status_code}
        if response.status_code != 200:
            payload["ok"] = False
            payload["message"] = response.text[:200]
            return payload
        try:
            data = response.json()
        except ValueError:
            payload["ok"] = False
            payload["message"] = "Datasquare verify returned a non-JSON response"
            return payload
        envelope = data if isinstance(data, dict) else {}
        payload["code"] = envelope.get("code")
        payload["message"] = envelope.get("msg") or envelope.get("message")
        payload["ok"] = envelope.get("code") == 0
        return payload
    finally:
        http.close()


def _session_summary(
    session: WebSession,
    *,
    account: str,
    cache_file: Path,
    verification: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = {
        "account": account,
        "login_username": session.login_username,
        "base_url": session.base_url,
        "workspace_id": session.workspace_id,
        "cookie_count": len((session.storage_state or {}).get("cookies") or []),
        "cache_file": str(cache_file),
        "created_at": session.created_at,
        "age_seconds": max(0, int(time.time() - float(session.created_at or 0))),
    }
    if verification is not None:
        payload["verification"] = verification
    return payload


def _import_datasquare_state(args: argparse.Namespace, account: str) -> dict[str, Any]:
    """Import a browser state captured on the data square into its own cache file.

    There is no workspace on this portal, so ``workspace_id`` stays unset unless the caller
    insists on one - inventing the compute platform's placeholder here would only mislead later
    readers of the cache.
    """
    base_url = _resolve_datasquare_base_url(args.base_url)
    storage_state = _load_storage_state(args.state_path.expanduser())
    session = WebSession(
        storage_state=storage_state,
        cookies=_cookies_by_name(storage_state),
        workspace_id=str(args.workspace_id or "").strip() or None,
        login_username=account,
        base_url=base_url,
        created_at=time.time(),
    )

    verification = None
    if args.verify:
        verification = _verify_datasquare_session(session, base_url)
        if not verification.get("ok"):
            raise ApiDebugError(
                "Imported data-square session did not verify: "
                f"{verification.get('message', 'unknown')}"
            )

    cache_file = save_datasquare_session(session, account=account)
    return _session_summary(
        session,
        account=account,
        cache_file=cache_file,
        verification=verification,
    )


def import_state(args: argparse.Namespace) -> dict[str, Any]:
    if args.portal == PORTAL_AIP:
        return _import_datasquare_state(args, _resolve_account(_load_config(), args.account))
    config = _load_qz_config(args.account)
    context = config.get_account_context()
    account = _resolve_account(config, context.cache_key)
    base_url = _resolve_base_url(config, args.base_url)
    storage_state = _load_storage_state(args.state_path.expanduser())
    workspace_id = (
        str(args.workspace_id or "").strip()
        or _extract_workspace_id(storage_state, base_url)
        or DEFAULT_WORKSPACE_ID
    )
    session = WebSession(
        storage_state=storage_state,
        cookies=_cookies_by_name(storage_state),
        workspace_id=workspace_id,
        account_alias=context.alias,
        login_username=context.username,
        base_url=base_url,
        created_at=time.time(),
    )

    verification = None
    if args.verify:
        verification = _verify_web_session(session, base_url)
        if not verification.get("ok"):
            raise ApiDebugError(
                f"Imported web session did not verify: " f"{verification.get('message', 'unknown')}"
            )
        if verification.get("user") != context.username:
            raise ApiDebugError("Imported browser session does not match the configured username")

    session.save(account=account)
    return _session_summary(
        session,
        account=account,
        cache_file=get_session_cache_file(account),
        verification=verification,
    )


def _check_datasquare(args: argparse.Namespace, account: str) -> dict[str, Any]:
    """Verify the cached data-square session, past its TTL, without launching a browser."""
    session = get_datasquare_session(account=account, allow_expired=True)
    if session is None or not (session.storage_state or {}).get("cookies"):
        raise ConfigDebugError(
            f"No cached data-square session found for {account}. Run: {DATASQUARE_LOGIN_HINT}"
        )

    base_url = _resolve_datasquare_base_url(args.base_url, session=session)
    verification = _verify_datasquare_session(session, base_url)
    if not verification.get("ok"):
        raise ApiDebugError(
            f"Cached data-square session did not verify: {verification.get('message', 'unknown')}"
        )

    return _session_summary(
        session,
        account=account,
        cache_file=get_datasquare_session_cache_file(account),
        verification=verification,
    )


def check(args: argparse.Namespace) -> dict[str, Any]:
    if args.portal == PORTAL_AIP:
        return _check_datasquare(args, _resolve_account(_load_config(), args.account))
    config = _load_qz_config(args.account)
    account = _resolve_account(config, config.get_account_context().cache_key)
    session = WebSession.load(allow_expired=True, account=account)
    if session is None or not (session.storage_state or {}).get("cookies"):
        raise ConfigDebugError(
            f"No cached web session found for {account}. Run: "
            "python scripts/debug_web_session.py import-state <state.json>"
        )

    base_url = _resolve_base_url(config, args.base_url, session=session)
    verification = _verify_web_session(session, base_url)
    if not verification.get("ok"):
        raise ApiDebugError(
            f"Cached web session did not verify: {verification.get('message', 'unknown')}"
        )

    return _session_summary(
        session,
        account=account,
        cache_file=get_session_cache_file(account),
        verification=verification,
    )


def refresh(args: argparse.Namespace) -> dict[str, Any]:
    context = _load_qz_config(args.account).get_account_context()
    account = context.cache_key
    session = WebSession.load(allow_expired=True, account=account) if account else None
    if (
        session is None
        or not (session.storage_state or {}).get("cookies")
        or not _session_matches_account_context(session, context)
    ):
        raise ConfigDebugError("Import a browser session for the selected account first")
    try:
        refreshed = refresh_with_requests(session, context.base_url)
    except SessionAuthenticationError:
        raise ApiDebugError(
            "SSO session expired. Log in through the browser and re-import it"
        ) from None
    except (OSError, RuntimeError, ValueError):
        raise ApiDebugError("Could not renew and verify the QZ session") from None
    return {"account": account, "refreshed": refreshed.save_if_current(session, account=account)}


def _add_portal_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--portal",
        choices=(PORTAL_QZ, PORTAL_AIP),
        default=PORTAL_QZ,
        help=(
            "qz: compute platform (web_session-<account>.json, verified through\n                               POST /api/v2/user?Action=GetUserDetail). "
            "aip: SII data square at aip.sii.edu.cn (datasquare_session-<account>.json, "
            "dataset catalogue endpoint, base URL from INSPIRE_DATASQUARE_BASE_URL)."
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Debug imported browser-backed web sessions without CAS login, on either the "
            "compute platform (--portal qz, default) or the SII data square (--portal aip)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import-state",
        help=(
            "Import a Playwright/agent-browser storage-state JSON into the web-session cache "
            "for one portal (--portal qz|aip, default qz)."
        ),
    )
    import_parser.add_argument("state_path", type=Path)
    import_parser.add_argument("--account", default=None)
    import_parser.add_argument("--base-url", default=None)
    import_parser.add_argument("--workspace-id", default=None)
    import_parser.add_argument("--no-verify", dest="verify", action="store_false", default=True)
    _add_portal_option(import_parser)
    import_parser.set_defaults(handler=import_state)

    check_parser = subparsers.add_parser(
        "check",
        help=(
            "Verify an existing cached web session without attempting password login "
            "(--portal qz|aip, default qz)."
        ),
    )
    check_parser.add_argument("--account", default=None)
    check_parser.add_argument("--base-url", default=None)
    _add_portal_option(check_parser)
    check_parser.set_defaults(handler=check)

    refresh_parser = subparsers.add_parser(
        "refresh", help="Renew a cached QZ session using its existing SSO cookies only."
    )
    refresh_parser.add_argument("--account", default=None)
    refresh_parser.set_defaults(handler=refresh)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        payload = args.handler(args)
    except ConfigDebugError as exc:
        return _emit_error("ConfigError", str(exc), 10)
    except ValidationDebugError as exc:
        return _emit_error("ValidationError", str(exc), 2)
    except ApiDebugError as exc:
        return _emit_error("APIError", str(exc), 13)
    except Exception as exc:  # noqa: BLE001
        return _emit_error("UnexpectedError", str(exc), 1)

    print(json.dumps({"success": True, "data": payload}, ensure_ascii=False, indent=2))
    return 0


def _emit_error(error_type: str, message: str, code: int) -> int:
    print(
        json.dumps(
            {
                "success": False,
                "error": {"type": error_type, "message": message},
            },
            ensure_ascii=False,
            indent=2,
        ),
        file=sys.stderr,
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
