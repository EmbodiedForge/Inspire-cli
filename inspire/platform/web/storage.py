"""WebDAV-backed storage helpers."""

from __future__ import annotations

import posixpath
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import quote
from urllib.parse import unquote
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth

from inspire.platform.web.browser_api.sftpgo import (
    DEFAULT_STORAGE_NAME,
    SFTPGoConnectionInfo,
    get_sftpgo_connection_info,
)
from inspire.platform.web.session import WebSession


class WebDAVStorageError(RuntimeError):
    """Raised when a WebDAV storage operation fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = _redact_webdav_text(body or "")
        suffix = f": {self.body}" if self.body else ""
        super().__init__(f"{message}{suffix}")


class WebDAVStorageProbeError(WebDAVStorageError):
    """Raised when a storage probe fails after cleanup has been attempted."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        operations: list["WebDAVOperation"] | None = None,
        cleanup_ok: bool = False,
        probe_dir: str = "",
        probe_file: str = "",
        created_paths: tuple[str, ...] = (),
    ) -> None:
        self.operations = operations or []
        self.cleanup_ok = cleanup_ok
        self.probe_dir = probe_dir
        self.probe_file = probe_file
        self.created_paths = created_paths
        super().__init__(message, status_code=status_code)

    @property
    def cleanup_needed(self) -> bool:
        return bool(self.created_paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "status_code": self.status_code,
            "cleanup_ok": self.cleanup_ok,
            "cleanup_needed": self.cleanup_needed,
            "created_paths": list(self.created_paths),
            "probe_dir": self.probe_dir,
            "probe_file": self.probe_file,
            "operations": [operation.__dict__ for operation in self.operations],
        }


class _HTTPResponse(Protocol):
    status_code: int
    text: str
    content: bytes


class _HTTPClient(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> _HTTPResponse: ...


@dataclass(frozen=True)
class WebDAVOperation:
    method: str
    path: str
    status_code: int
    ok: bool
    detail: str | None = None


@dataclass(frozen=True)
class WebDAVEntry:
    path: str
    name: str
    is_dir: bool
    size: int | None = None
    modified_at: str | None = None
    modified_ts: int | None = None
    etag: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "is_dir": self.is_dir,
            "size": self.size,
            "modified_at": self.modified_at,
            "modified_ts": self.modified_ts,
            "etag": self.etag,
        }


@dataclass(frozen=True)
class WebDAVProbeResult:
    storage_name: str
    base_path: str
    probe_dir: str
    probe_file: str
    content_verified: bool
    cleanup_ok: bool
    operations: list[WebDAVOperation] = field(default_factory=list)
    connection: dict[str, Any] = field(default_factory=dict)
    #: Paths this probe actually created. Empty when the very first operation was denied - in which
    #: case "cleanup OK" would be a claim about work that was never needed (issue #37 drive-by).
    created_paths: tuple[str, ...] = ()

    @property
    def cleanup_needed(self) -> bool:
        return bool(self.created_paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "storage_name": self.storage_name,
            "base_path": self.base_path,
            "probe_dir": self.probe_dir,
            "probe_file": self.probe_file,
            "content_verified": self.content_verified,
            "cleanup_ok": self.cleanup_ok,
            "cleanup_needed": self.cleanup_needed,
            "created_paths": list(self.created_paths),
            "connection": self.connection,
            "operations": [operation.__dict__ for operation in self.operations],
        }


def _redact_webdav_text(text: str, *, secrets: tuple[str, ...] = ()) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    for marker in ("Authorization:", "authorization:"):
        if marker in redacted:
            lines = []
            for line in redacted.splitlines():
                if line.lower().startswith("authorization:"):
                    lines.append("Authorization: <redacted>")
                else:
                    lines.append(line)
            redacted = "\n".join(lines)
    return redacted[:500]


def _join_remote_path(base_path: str, *parts: str) -> str:
    base = str(base_path or "").strip()
    if not base.startswith("/"):
        raise ValueError(f"WebDAV base path must be absolute, got {base_path!r}")
    current = base.rstrip("/") or "/"
    for part in parts:
        current = posixpath.join(current, part.strip("/"))
    return current


def normalize_remote_path(path: str) -> str:
    """Validate a user-provided WebDAV path and return a normalized absolute path."""
    value = str(path or "").strip()
    if not value:
        raise ValueError("WebDAV path is empty")
    if "\x00" in value:
        raise ValueError("WebDAV path contains NUL")
    if not value.startswith("/"):
        raise ValueError(
            f"WebDAV path must be absolute, got {path!r}. " "Use a platform path like /inspire/..."
        )
    parts = [part for part in value.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError(f"WebDAV path must not contain . or .. segments, got {path!r}")
    normalized = posixpath.normpath(value)
    if normalized == ".":
        normalized = "/"
    if value.endswith("/") and normalized != "/":
        return normalized + "/"
    return normalized


def _quote_remote_path(path: str) -> str:
    path = normalize_remote_path(path)
    return quote(path, safe="/@:+,=._-")


_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<D:propfind xmlns:D="DAV:">'
    "<D:prop>"
    "<D:resourcetype/>"
    "<D:getcontentlength/>"
    "<D:getlastmodified/>"
    "<D:displayname/>"
    "<D:getetag/>"
    "</D:prop>"
    "</D:propfind>"
)


def _parse_propfind(xml_text: str) -> list[WebDAVEntry]:
    ns = {"d": "DAV:"}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise WebDAVStorageError(f"webdav PROPFIND returned invalid XML: {exc}") from exc

    entries: list[WebDAVEntry] = []
    for response in root.findall("d:response", ns):
        href_el = response.find("d:href", ns)
        href_text = (href_el.text or "").strip() if href_el is not None else ""
        if not href_text:
            continue
        href = unquote(href_text)
        is_dir = False
        size: int | None = None
        modified_at: str | None = None
        modified_ts: int | None = None
        etag: str | None = None
        for propstat in response.findall("d:propstat", ns):
            status_el = propstat.find("d:status", ns)
            if status_el is not None and "200" not in (status_el.text or ""):
                continue
            prop = propstat.find("d:prop", ns)
            if prop is None:
                continue
            if prop.find("d:resourcetype/d:collection", ns) is not None:
                is_dir = True
            length_el = prop.find("d:getcontentlength", ns)
            length_text = (length_el.text or "").strip() if length_el is not None else ""
            if length_text.isdigit():
                size = int(length_text)
            modified_el = prop.find("d:getlastmodified", ns)
            modified_text = (modified_el.text or "").strip() if modified_el is not None else ""
            if modified_text:
                modified_at = modified_text
                try:
                    modified_ts = int(parsedate_to_datetime(modified_at).timestamp())
                except (TypeError, ValueError):
                    modified_ts = None
            etag_el = prop.find("d:getetag", ns)
            etag_text = (etag_el.text or "").strip() if etag_el is not None else ""
            if etag_text:
                etag = etag_text.strip('"')
        entry_path = href.rstrip("/") if is_dir else href
        name = entry_path.rstrip("/").rsplit("/", 1)[-1] if entry_path.rstrip("/") else "/"
        entries.append(
            WebDAVEntry(
                path=entry_path,
                name=name,
                is_dir=is_dir,
                size=size,
                modified_at=modified_at,
                modified_ts=modified_ts,
                etag=etag,
            )
        )
    return entries


class WebDAVStorageClient:
    """Small WebDAV Adapter for storage operations."""

    def __init__(
        self,
        connection: SFTPGoConnectionInfo,
        *,
        http: _HTTPClient | None = None,
        timeout: float = 120,
    ) -> None:
        self.connection = connection
        self.http = http or requests.Session()
        self.timeout = timeout

    def url_for(self, path: str) -> str:
        return f"{self.connection.webdav_base_url}{_quote_remote_path(path)}"

    def request(
        self,
        method: str,
        path: str,
        *,
        ok: tuple[int, ...],
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> int:
        path = normalize_remote_path(path)
        response = self.http.request(
            method,
            self.url_for(path),
            headers=headers,
            data=data,
            auth=HTTPBasicAuth(self.connection.username, self.connection.password),
            timeout=self.timeout,
            allow_redirects=False,
        )
        if response.status_code not in ok:
            body = getattr(response, "text", "") or ""
            raise WebDAVStorageError(
                f"webdav {method} {path} -> HTTP {response.status_code}",
                status_code=response.status_code,
                body=_redact_webdav_text(
                    body,
                    secrets=(
                        self.connection.password,
                        self.connection.auth,
                    ),
                ),
            )
        return response.status_code

    def mkcol(self, path: str, *, exist_ok: bool = False) -> int:
        path = normalize_remote_path(path).rstrip("/") + "/"
        try:
            return self.request("MKCOL", path, ok=(201,))
        except WebDAVStorageError as exc:
            if exist_ok and exc.status_code == 405:
                return 405
            raise

    def put(self, path: str, content: bytes) -> int:
        path = normalize_remote_path(path)
        return self.request(
            "PUT",
            path,
            ok=(200, 201, 204),
            data=content,
            headers={"Content-Type": "application/octet-stream"},
        )

    def get(self, path: str) -> tuple[int, bytes]:
        path = normalize_remote_path(path)
        response = self.http.request(
            "GET",
            self.url_for(path),
            auth=HTTPBasicAuth(self.connection.username, self.connection.password),
            timeout=self.timeout,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise WebDAVStorageError(
                f"webdav GET {path} -> HTTP {response.status_code}",
                status_code=response.status_code,
                body=_redact_webdav_text(
                    getattr(response, "text", "") or "",
                    secrets=(
                        self.connection.password,
                        self.connection.auth,
                    ),
                ),
            )
        return response.status_code, bytes(response.content)

    def propfind(self, path: str, *, depth: int) -> tuple[int, list[WebDAVEntry]]:
        path = normalize_remote_path(path)
        request_path = path if depth == 0 else path.rstrip("/") + "/"
        response = self.http.request(
            "PROPFIND",
            self.url_for(request_path),
            headers={"Depth": str(depth), "Content-Type": "application/xml"},
            data=_PROPFIND_BODY.encode(),
            auth=HTTPBasicAuth(self.connection.username, self.connection.password),
            timeout=self.timeout,
            allow_redirects=False,
        )
        if response.status_code != 207:
            raise WebDAVStorageError(
                f"webdav PROPFIND {path} -> HTTP {response.status_code}",
                status_code=response.status_code,
                body=_redact_webdav_text(
                    getattr(response, "text", "") or "",
                    secrets=(
                        self.connection.password,
                        self.connection.auth,
                    ),
                ),
            )
        return response.status_code, _parse_propfind(response.text)

    def listdir(self, path: str) -> list[WebDAVEntry]:
        path = normalize_remote_path(path).rstrip("/") or "/"
        _, entries = self.propfind(path, depth=1)
        children = [entry for entry in entries if entry.path.rstrip("/") != path.rstrip("/")]
        children.sort(key=lambda entry: (not entry.is_dir, entry.name))
        return children

    def stat(self, path: str) -> WebDAVEntry:
        path = normalize_remote_path(path)
        _, entries = self.propfind(path, depth=0)
        if not entries:
            raise WebDAVStorageError(f"webdav PROPFIND {path} returned no entries")
        entry = entries[0]
        normalized_path = path.rstrip("/") if entry.is_dir else path
        return WebDAVEntry(
            path=normalized_path,
            name=entry.name,
            is_dir=entry.is_dir,
            size=entry.size,
            modified_at=entry.modified_at,
            modified_ts=entry.modified_ts,
            etag=entry.etag,
        )

    def mkdir_parents(self, path: str, *, exist_ok: bool = False) -> None:
        path = normalize_remote_path(path).rstrip("/")
        if not path or path == "/":
            if exist_ok:
                return
            raise ValueError("Cannot create WebDAV root directory")

        try:
            self.mkcol(path, exist_ok=exist_ok)
            return
        except WebDAVStorageError as exc:
            if exist_ok and exc.status_code == 405:
                return
            if exc.status_code != 409:
                raise

        parent = posixpath.dirname(path)
        if parent in ("", "/", path):
            self.mkcol(path, exist_ok=exist_ok)
            return

        self.mkdir_parents(parent, exist_ok=True)
        try:
            self.mkcol(path, exist_ok=exist_ok)
        except WebDAVStorageError as exc:
            if exist_ok and exc.status_code == 405:
                return
            raise

    def move(self, source: str, dest: str, *, overwrite: bool = False) -> int:
        source = normalize_remote_path(source)
        dest = normalize_remote_path(dest)
        return self.request(
            "MOVE",
            source,
            ok=(201, 204),
            headers={
                "Destination": self.url_for(dest),
                "Overwrite": "T" if overwrite else "F",
            },
        )

    def delete(self, path: str, *, missing_ok: bool = False) -> int:
        path = normalize_remote_path(path)
        try:
            return self.request("DELETE", path, ok=(200, 204))
        except WebDAVStorageError as exc:
            if missing_ok and exc.status_code == 404:
                return 404
            raise


def _probe_suffix() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f".inspire-probe-{stamp}-{secrets.token_hex(3)}"


def probe_webdav_storage(
    *,
    base_path: str,
    storage_name: str = DEFAULT_STORAGE_NAME,
    session: WebSession | None = None,
    content: bytes = b"inspire webdav probe\n",
    http: _HTTPClient | None = None,
) -> WebDAVProbeResult:
    """Create, verify, and clean up a tiny WebDAV file under ``base_path``."""
    connection = get_sftpgo_connection_info(storage_name=storage_name, session=session)
    client = WebDAVStorageClient(connection, http=http)
    operations: list[WebDAVOperation] = []
    probe_dir = _join_remote_path(base_path, _probe_suffix())
    probe_file = _join_remote_path(probe_dir, "probe.txt")
    cleanup_ok = True
    content_verified = False
    created: list[str] = []
    failure: WebDAVStorageError | None = None
    cleanup_failure: WebDAVStorageError | None = None

    def record(
        method: str, path: str, status_code: int, ok: bool, detail: str | None = None
    ) -> None:
        operations.append(
            WebDAVOperation(
                method=method,
                path=path,
                status_code=status_code,
                ok=ok,
                detail=detail,
            )
        )

    try:
        status = client.mkcol(probe_dir)
        created.append(probe_dir)
        record("MKCOL", probe_dir, status, True)
        status = client.put(probe_file, content)
        created.append(probe_file)
        record("PUT", probe_file, status, True)
        status, downloaded = client.get(probe_file)
        content_verified = downloaded == content
        record("GET", probe_file, status, content_verified)
        if not content_verified:
            raise WebDAVStorageError("webdav GET content did not match uploaded probe content")
    except WebDAVStorageError as exc:
        failure = exc
        record("ERROR", probe_file, exc.status_code or 0, False, str(exc))
    finally:
        for method, path in (("DELETE", probe_file), ("DELETE", probe_dir)):
            try:
                status = client.delete(path, missing_ok=True)
                record(method, path, status, True)
            except WebDAVStorageError as exc:
                cleanup_ok = False
                if cleanup_failure is None:
                    cleanup_failure = exc
                record(method, path, exc.status_code or 0, False, str(exc))

    if failure is not None:
        raise WebDAVStorageProbeError(
            str(failure),
            status_code=failure.status_code,
            operations=operations,
            cleanup_ok=cleanup_ok,
            probe_dir=probe_dir,
            probe_file=probe_file,
            created_paths=tuple(created),
        ) from failure
    if cleanup_failure is not None:
        raise WebDAVStorageProbeError(
            f"webdav probe cleanup failed: {cleanup_failure}",
            status_code=cleanup_failure.status_code,
            operations=operations,
            cleanup_ok=False,
            probe_dir=probe_dir,
            probe_file=probe_file,
        ) from cleanup_failure

    return WebDAVProbeResult(
        storage_name=storage_name,
        base_path=base_path.rstrip("/"),
        probe_dir=probe_dir,
        probe_file=probe_file,
        content_verified=content_verified,
        cleanup_ok=cleanup_ok,
        operations=operations,
        connection=connection.redacted(),
        created_paths=tuple(created),
    )


__all__ = [
    "WebDAVEntry",
    "WebDAVOperation",
    "WebDAVProbeResult",
    "WebDAVStorageClient",
    "WebDAVStorageError",
    "WebDAVStorageProbeError",
    "normalize_remote_path",
    "probe_webdav_storage",
]
