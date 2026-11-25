"""Browser API access for SFTPGo/WebDAV connection information."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.browser_api.errors import ensure_browser_api_success
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session

DEFAULT_STORAGE_NAME = "hdd"


class SFTPGoConnectionInfoError(ValueError):
    """Raised when the platform returns unusable SFTPGo connection info."""


@dataclass(frozen=True)
class SFTPGoConnectionInfo:
    """Connection info for WebDAV operations.

    Secret-bearing fields are excluded from repr so accidental CLI/test output
    does not expose WebDAV credentials.
    """

    address: str
    webdav_port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    auth: str = field(repr=False)
    storage_name: str = DEFAULT_STORAGE_NAME
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def webdav_base_url(self) -> str:
        parts = urlsplit(self.address)
        host = parts.hostname
        if not host:
            raise SFTPGoConnectionInfoError(
                f"connection_info address has no host: {self.address!r}"
            )
        return urlunsplit((parts.scheme or "https", f"{host}:{self.webdav_port}", "", "", ""))

    def redacted(self) -> dict[str, Any]:
        """Return a redaction-safe representation for JSON output."""
        return {
            "address": self.address,
            "webdav_port": self.webdav_port,
            "webdav_base_url": self.webdav_base_url,
            "username": self.username,
            "password": "<redacted>",
            "auth": "<redacted>",
            "storage_name": self.storage_name,
        }


def _decode_auth(encoded_auth: str) -> tuple[str, str]:
    try:
        decoded = base64.b64decode(encoded_auth).decode()
    except Exception as exc:  # noqa: BLE001
        raise SFTPGoConnectionInfoError(f"connection_info auth is not valid base64: {exc}") from exc
    if ":" not in decoded:
        raise SFTPGoConnectionInfoError("connection_info auth is not 'username:password'")
    username, password = decoded.split(":", 1)
    return username, password


def normalize_sftpgo_connection_info(
    raw: dict[str, Any],
    *,
    storage_name: str = DEFAULT_STORAGE_NAME,
) -> SFTPGoConnectionInfo:
    """Normalize platform connection info into explicit WebDAV credentials."""
    address = str(raw.get("address") or "").rstrip("/")
    if not address:
        raise SFTPGoConnectionInfoError("connection_info missing address")

    port = raw.get("webdav_port")
    if port in (None, "", 0):
        raise SFTPGoConnectionInfoError("connection_info missing webdav_port")
    try:
        webdav_port = int(port)
    except (TypeError, ValueError) as exc:
        raise SFTPGoConnectionInfoError(
            f"connection_info webdav_port is not an integer: {port!r}"
        ) from exc

    auth = str(raw.get("auth") or "")
    username = str(raw.get("username") or "")
    password = str(raw.get("password") or "")
    if auth and not (username and password):
        username, password = _decode_auth(auth)
    if not (username and password):
        raise SFTPGoConnectionInfoError("connection_info missing username/password")
    if not auth:
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()

    return SFTPGoConnectionInfo(
        address=address,
        webdav_port=webdav_port,
        username=username,
        password=password,
        auth=auth,
        storage_name=storage_name,
        raw=dict(raw),
    )


def get_sftpgo_connection_info(
    *,
    storage_name: str = DEFAULT_STORAGE_NAME,
    usage: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> SFTPGoConnectionInfo:
    """Fetch WebDAV connection info from the browser API."""
    if session is None:
        session = get_web_session()

    body: dict[str, Any] = {"storage_name": storage_name}
    if usage:
        body["usage"] = usage

    payload = _request_json(
        session,
        "POST",
        _browser_api_path("/file/sftpgo/connection_info"),
        referer=f"{_get_base_url()}/files",
        body=body,
        timeout=30,
    )
    ensure_browser_api_success(payload, action="Get SFTPGo connection info")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise SFTPGoConnectionInfoError("connection_info data is not an object")
    return normalize_sftpgo_connection_info(data, storage_name=storage_name)


def list_system_storage_types(
    *,
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict[str, Any]]:
    """List platform storage types available in a workspace."""
    if session is None:
        session = get_web_session()
    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    body = {"filter": {"workspace_id": workspace_id}}
    payload = _request_json(
        session,
        "POST",
        _browser_api_path("/file/get_system_storage_type_list"),
        referer=f"{_get_base_url()}/files",
        body=body,
        timeout=30,
    )
    ensure_browser_api_success(payload, action="List storage types")
    data = payload.get("data") or {}
    storages = data.get("system_storages", [])
    return storages if isinstance(storages, list) else []


def list_storage_roots(
    *,
    workspace_id: Optional[str] = None,
    storage_name: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict[str, Any]]:
    """Discover root directories exposed by the platform file surface."""
    if session is None:
        session = get_web_session()
    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    roots: list[dict[str, Any]] = []
    storages = list_system_storage_types(workspace_id=workspace_id, session=session)
    if storage_name:
        storages = [item for item in storages if item.get("name") == storage_name]

    errors: list[str] = []
    for storage in storages:
        name = str(storage.get("name") or "").strip()
        if not name:
            continue
        cluster_id = str(storage.get("cluster_id") or "").strip()
        for category in ("project", "global_user", "global_public"):
            body = {
                "filter": {
                    "workspace_id": workspace_id,
                    "system_storage_type": name,
                    "cluster_id": cluster_id,
                    "name": category,
                }
            }
            try:
                payload = _request_json(
                    session,
                    "POST",
                    _browser_api_path("/file/dir/list"),
                    referer=f"{_get_base_url()}/files",
                    body=body,
                    timeout=30,
                )
                ensure_browser_api_success(payload, action="List storage roots")
            except ValueError as exc:
                errors.append(f"{name}/{category}: {exc}")
                continue
            files = (payload.get("data") or {}).get("files", [])
            if not isinstance(files, list):
                continue
            for entry in files:
                if not isinstance(entry, dict):
                    continue
                directory = str(entry.get("directory") or "").strip()
                if not directory:
                    continue
                roots.append(
                    {
                        "workspace_id": workspace_id,
                        "storage": name,
                        "cluster_id": cluster_id or None,
                        "category": category,
                        "path": directory,
                        "raw": entry,
                    }
                )
    if not roots and errors:
        raise ValueError("; ".join(errors))
    return roots


__all__ = [
    "DEFAULT_STORAGE_NAME",
    "SFTPGoConnectionInfo",
    "SFTPGoConnectionInfoError",
    "get_sftpgo_connection_info",
    "list_storage_roots",
    "list_system_storage_types",
    "normalize_sftpgo_connection_info",
]
