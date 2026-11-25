"""File upload helpers for notebook Jupyter transports."""

from __future__ import annotations

import base64
import hashlib
import posixpath
import shlex
import subprocess
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from inspire.platform.web.browser_api.notebook_shell import (
    NotebookShellResult,
    execute_notebook_shell_command,
)
from inspire.platform.web.browser_api.playwright_notebooks import get_jupyter_token
from inspire.platform.web.session import WebSession, build_requests_session


@dataclass(frozen=True)
class NotebookUploadResult:
    notebook_id: str
    local_path: str
    remote_path: str
    bytes: int
    sha256: str
    elapsed_s: float
    transport: str = "jupyter_contents"
    local_bytes: int = -1

    def as_payload(self) -> dict[str, Any]:
        return {
            "notebook_id": self.notebook_id,
            "local_path": self.local_path,
            "remote_path": self.remote_path,
            "bytes": self.bytes,
            "local_bytes": self.local_bytes,
            "sha256": self.sha256,
            "elapsed_s": self.elapsed_s,
            "transport": self.transport,
        }


def _auth_headers(http: Any, token: str | None) -> dict[str, str]:
    if token:
        return {"Authorization": f"token {token}"}
    for cookie in http.cookies:
        if cookie.name == "_xsrf":
            return {"X-XSRFToken": cookie.value}
    return {}


def _create_archive(local_path: Path, archive_path: Path) -> int:
    """Archive *local_path* and return the total size of the files that went into it.

    In write mode `getmembers()` is the list added so far, so the uncompressed size costs no
    second pass over the tree. A single file therefore reports its own size.
    """
    with tarfile.open(archive_path, mode="w:gz") as archive:
        archive.add(local_path, arcname=local_path.name, recursive=True)
        return sum(member.size for member in archive.getmembers() if member.isfile())


def _quote_remote_path(path: str) -> str:
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        suffix = path[2:]
        return f'"$HOME"/{shlex.quote(suffix)}' if suffix else '"$HOME"/'
    return shlex.quote(path)


def _install_uploaded_archive_command(
    *,
    staging_name: str,
    source_name: str,
    remote_path: str,
    archive_sha256: str,
) -> str:
    marker = uuid.uuid4().hex[:12]
    extract_dir = f".inspire-upload-extract-{marker}"
    q_stage_name = shlex.quote(staging_name)
    q_extract = shlex.quote(extract_dir)
    q_source = shlex.quote(posixpath.join(extract_dir, source_name))
    q_remote = _quote_remote_path(remote_path)
    q_sha = shlex.quote(archive_sha256)
    destination_has_slash = remote_path.endswith("/")
    copy_command = (
        f"mkdir -p {q_remote} && cp -a {q_source} {q_remote}"
        if destination_has_slash
        else (
            f"if [ -d {q_remote} ]; then cp -a {q_source} {q_remote}/; "
            f"else mkdir -p $(dirname {q_remote}) && cp -a {q_source} {q_remote}; fi"
        )
    )
    return (
        "set -e; "
        "__inspire_stage=; "
        f'for __inspire_candidate in /tmp/{q_stage_name} "$(pwd -P)/tmp/{staging_name}"; do '
        f'if [ -f "$__inspire_candidate" ] && '
        f'[ "$(sha256sum "$__inspire_candidate" | awk \'{{print $1}}\')" = {q_sha} ]; '
        "then __inspire_stage=$__inspire_candidate; break; fi; done; "
        f'if [ -z "$__inspire_stage" ]; then rm -f /tmp/{q_stage_name} '
        f'"$(pwd -P)/tmp/{staging_name}"; exit 1; fi; '
        f"__inspire_extract={q_extract}; "
        'trap \'rm -rf "$__inspire_extract" "$__inspire_stage"\' EXIT; '
        f"rm -rf {q_extract}; mkdir -p {q_extract}; "
        f'tar -xzf "$__inspire_stage" -C {q_extract}; '
        f"{copy_command}; "
        f'rm -rf {q_extract} "$__inspire_stage"; trap - EXIT'
    )


def _ensure_contents_tmp_directory(http: Any, base_url: str, headers: dict[str, str]) -> None:
    url = f"{base_url}api/contents/tmp"
    response = http.get(url, headers=headers, params={"content": "0"}, timeout=10)
    if response.status_code == 404:
        response = http.put(
            url,
            headers=headers,
            json={"type": "directory"},
            timeout=10,
        )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Jupyter Contents tmp directory returned HTTP {response.status_code}.")


def upload_bytes_to_notebook_contents(
    *,
    notebook_id: str,
    session: WebSession,
    rel_path: str,
    content_bytes: bytes,
    timeout: int,
) -> None:
    """Write *content_bytes* to *rel_path* through the Jupyter Contents API.

    *rel_path* is server-root-relative (``tmp/name``), which is what the Contents API takes: the
    notebook server roots every path at its own directory, so the ``/tmp`` a shell sees is only the
    same directory when the shell starts in the server root.
    """
    base_url, token, _host = get_jupyter_token(notebook_id, session, timeout=timeout)
    http = build_requests_session(session, base_url)
    try:
        headers = _auth_headers(http, token)
        _ensure_contents_tmp_directory(http, base_url, headers)
        response = http.put(
            f"{base_url}api/contents/{quote(rel_path, safe='/')}",
            headers=headers,
            json={
                "type": "file",
                "format": "base64",
                "content": base64.b64encode(content_bytes).decode("ascii"),
            },
            timeout=(10, timeout),
        )
        if response.status_code not in (200, 201):
            body = str(response.text or "").strip()[:400]
            suffix = f": {body}" if body else ""
            raise RuntimeError(
                f"Jupyter Contents upload returned HTTP {response.status_code}{suffix}"
            )
    finally:
        http.close()


def upload_path_to_notebook(
    *,
    notebook_id: str,
    session: WebSession,
    local_path: str | Path,
    remote_path: str,
    timeout: int,
    run_command_fn: Callable[..., NotebookShellResult] = execute_notebook_shell_command,
) -> NotebookUploadResult:
    """Upload a local file or directory and install it at *remote_path*.

    The payload is archived locally, uploaded once through Jupyter Contents,
    verified by SHA-256 in the notebook, then copied to the requested path.
    """
    source = Path(local_path).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"Local path not found: {source}")
    if not remote_path.strip():
        raise ValueError("Remote path cannot be empty.")

    started = time.monotonic()
    with tempfile.NamedTemporaryFile(
        prefix="inspire-upload-", suffix=".tar.gz", delete=False
    ) as tmp:
        archive_path = Path(tmp.name)
    try:
        local_bytes = _create_archive(source, archive_path)
        archive_bytes = archive_path.read_bytes()
        archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        staging_name = f"inspire-upload-{uuid.uuid4().hex}.tar.gz"
        staging_rel_path = f"tmp/{staging_name}"
        upload_bytes_to_notebook_contents(
            notebook_id=notebook_id,
            session=session,
            rel_path=staging_rel_path,
            content_bytes=archive_bytes,
            timeout=timeout,
        )

        install = run_command_fn(
            notebook_id=notebook_id,
            command=_install_uploaded_archive_command(
                staging_name=staging_name,
                source_name=source.name,
                remote_path=remote_path,
                archive_sha256=archive_sha256,
            ),
            session=session,
            timeout=timeout,
        )
        if install.returncode != 0:
            raise RuntimeError(
                install.stdout.strip()
                or (install.stderr or "").strip()
                or f"Remote upload install exited with {install.returncode}."
            )
        return NotebookUploadResult(
            notebook_id=notebook_id,
            local_path=str(source),
            remote_path=remote_path,
            bytes=len(archive_bytes),
            sha256=archive_sha256,
            elapsed_s=round(time.monotonic() - started, 3),
            local_bytes=local_bytes,
        )
    except subprocess.TimeoutExpired:
        raise
    finally:
        archive_path.unlink(missing_ok=True)


__all__ = [
    "NotebookUploadResult",
    "upload_bytes_to_notebook_contents",
    "upload_path_to_notebook",
]
