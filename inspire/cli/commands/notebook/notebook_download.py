"""Download files from notebooks through Jupyter HTTP, without SSH tunnels."""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import random
import re
import shlex
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional
from urllib.parse import quote

import click

from inspire.bridge.tunnel import load_tunnel_config
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import (
    get_base_url,
    load_config,
    require_web_session,
    resolve_json_output,
)
from inspire.cli.utils.output import emit_progress, emit_warning
from inspire.platform.web.browser_api.playwright_notebooks import run_command_in_notebook
from inspire.platform.web.browser_api.rtunnel._jupyter import (
    _extract_jupyter_token,
    _jupyter_server_base,
)
from inspire.platform.web.browser_api.rtunnel.lab_resolution import resolve_lab_url
from inspire.platform.web.session import WebSession, build_requests_session

from .notebook_lookup import _resolve_notebook_id

DEFAULT_DOWNLOAD_TIMEOUT = 600
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
DEFAULT_RANGE_RETRIES = 5
STAGING_DIR = "inspire-downloads"

logger = logging.getLogger(__name__)


@dataclass
class StageInfo:
    rel_path: str
    abs_path: str
    archive: bool
    method: str
    download_url: str | None = None
    cleanup_command: str | None = None
    failure_cleanup_command: str | None = None


@dataclass
class NotebookDownloadResult:
    notebook_id: str
    remote_path: str
    local_path: str
    bytes: int
    content_length: int | None
    elapsed_s: float
    mib_per_s: float
    transport: str
    staged: bool
    archive: bool
    cleanup_status: str
    stage_method: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "notebook_id": self.notebook_id,
            "remote_path": self.remote_path,
            "local_path": self.local_path,
            "bytes": self.bytes,
            "content_length": self.content_length,
            "elapsed_s": self.elapsed_s,
            "mib_per_s": self.mib_per_s,
            "transport": self.transport,
            "staged": self.staged,
            "archive": self.archive,
            "cleanup_status": self.cleanup_status,
            "stage_method": self.stage_method,
        }


def _auth_headers(token: str | None) -> dict[str, str]:
    if token:
        return {"Authorization": f"token {token}"}
    return {}


def _token_params(token: str | None, **extra: str) -> dict[str, str]:
    params = dict(extra)
    if token:
        params["token"] = token
    return params


def _remote_quote(path: str) -> str:
    return shlex.quote(path)


def _safe_stage_prefix(remote_path: str) -> str:
    name = PurePosixPath(remote_path.rstrip("/") or "download").name or "download"
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
    if not safe or safe in {".", ".."}:
        safe = "download"
    return safe


def _safe_stage_name(remote_path: str, *, suffix: str = "") -> str:
    safe = _safe_stage_prefix(remote_path)
    unique = uuid.uuid4().hex[:12]
    return f"{safe}-{unique}{suffix}"


def _canonical_remote_path(remote_path: str) -> str:
    """Collapse one remote path to the single spelling its digests are computed from.

    The resume ``.part`` digest and the staged archive digest are the two halves of the same
    claim - "these bytes came from this source" - so they must hash the same string. A
    trailing slash or a ``.`` segment is cosmetic: the same directory spelled two ways has
    to produce one digest, or resume and verification disagree about its own download.
    """
    stripped = remote_path.strip()
    if not stripped:
        return ""
    return posixpath.normpath(stripped)


def _stable_stage_name(remote_path: str, *, suffix: str = "") -> str:
    safe = _safe_stage_prefix(remote_path)
    digest = hashlib.sha256(_canonical_remote_path(remote_path).encode("utf-8")).hexdigest()[:12]
    return f"{safe}-{digest}{suffix}"


def _partial_digest(notebook_id: str, remote_path: str) -> str:
    """Digest binding a local ``.part`` file to the notebook and remote path it resumes from."""
    source = f"{notebook_id}\0{_canonical_remote_path(remote_path)}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def _join_remote(root: str, rel_path: str) -> str:
    if not rel_path or rel_path == ".":
        return root.rstrip("/")
    return posixpath.normpath(posixpath.join(root.rstrip("/"), rel_path))


def _local_destination(local_path: str, download_name: str) -> Path:
    raw = Path(local_path)
    treat_as_dir = local_path.endswith((os.sep, "/")) or (raw.exists() and raw.is_dir())
    if treat_as_dir:
        raw.mkdir(parents=True, exist_ok=True)
        return raw / download_name
    raw.parent.mkdir(parents=True, exist_ok=True)
    return raw


def _parse_marker(stdout: str, marker: str) -> int:
    prefix = f"{marker}:"
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith(prefix):
            try:
                return int(stripped[len(prefix) :].strip())
            except ValueError as exc:
                raise RuntimeError(f"Invalid staging marker from notebook: {stripped}") from exc
    raise RuntimeError("Notebook command completed without an exit-code marker.")


def _split_marker(marker: str) -> tuple[str, str]:
    split_at = max(1, len(marker) // 2)
    return marker[:split_at], marker[split_at:]


def _shell_marker_line(marker: str) -> str:
    left, right = _split_marker(marker)
    return f"printf '\\n%s%s\\n' {_remote_quote(left)} {_remote_quote(right)}"


def _shell_exit_marker_line(marker: str) -> str:
    left, right = _split_marker(marker)
    return f"printf '\\n%s%s:%s\\n' {_remote_quote(left)} {_remote_quote(right)} \"$rc\""


PROTOCOL_MARKER_NEEDLE = "__INSPIRE_DOWNLOAD_"


def _output_tail(text: str, limit: int = 500) -> str:
    collapsed = " ".join(text.split())
    if not collapsed:
        return ""
    if len(collapsed) <= limit:
        return collapsed
    return f"… {collapsed[-limit:]}"


def _failure_detail(error: str, stdout: str) -> str:
    # Our marker framing is protocol, not program output; the reader wants the command's own words.
    output = "\n".join(line for line in stdout.splitlines() if PROTOCOL_MARKER_NEEDLE not in line)
    tail = _output_tail(output)
    return f"{error} Output (tail): {tail}" if tail else error


def _run_marked_shell(
    *,
    notebook_id: str,
    session: WebSession,
    command: str,
    timeout: int,
    run_command_fn: Callable[..., dict[str, Any]] = run_command_in_notebook,
) -> str:
    marker = f"__INSPIRE_DOWNLOAD_EXIT_{uuid.uuid4().hex}__"
    wrapped = f'{command}\nrc=$?\n{_shell_exit_marker_line(marker)}\nexit "$rc"'
    result = run_command_fn(
        notebook_id=notebook_id,
        command=f"bash -lc {_remote_quote(wrapped)}",
        session=session,
        timeout=timeout,
        completion_marker=marker,
        capture_output=True,
    )
    stdout = str(result.get("stdout") or "")
    if not result.get("success", False):
        raise RuntimeError(
            _failure_detail(str(result.get("error") or "Notebook command failed."), stdout)
        )
    rc = _parse_marker(stdout, marker)
    if rc != 0:
        raise RuntimeError(
            _failure_detail(f"Notebook staging command failed with exit code {rc}.", stdout)
        )
    return stdout


def _run_marked_shell_text(
    *,
    notebook_id: str,
    session: WebSession,
    command: str,
    timeout: int,
    run_command_fn: Callable[..., dict[str, Any]] = run_command_in_notebook,
) -> str:
    exit_marker = f"__INSPIRE_DOWNLOAD_EXIT_{uuid.uuid4().hex}__"
    begin_marker = f"__INSPIRE_DOWNLOAD_BEGIN_{uuid.uuid4().hex}__"
    end_marker = f"__INSPIRE_DOWNLOAD_END_{uuid.uuid4().hex}__"
    wrapped = (
        f"{_shell_marker_line(begin_marker)}\n"
        f"{command}\n"
        "rc=$?\n"
        f"{_shell_marker_line(end_marker)}\n"
        f"{_shell_exit_marker_line(exit_marker)}\n"
        'exit "$rc"'
    )
    result = run_command_fn(
        notebook_id=notebook_id,
        command=f"bash -lc {_remote_quote(wrapped)}",
        session=session,
        timeout=timeout,
        completion_marker=exit_marker,
        capture_output=True,
    )
    stdout = str(result.get("stdout") or "")
    if not result.get("success", False):
        raise RuntimeError(
            _failure_detail(str(result.get("error") or "Notebook command failed."), stdout)
        )
    rc = _parse_marker(stdout, exit_marker)
    if rc != 0:
        raise RuntimeError(
            _failure_detail(f"Notebook staging command failed with exit code {rc}.", stdout)
        )
    lines = stdout.splitlines()
    try:
        begin_index = next(
            index for index, line in enumerate(lines) if line.strip() == begin_marker
        )
        end_index = next(
            index
            for index, line in enumerate(lines[begin_index + 1 :], start=begin_index + 1)
            if line.strip() == end_marker
        )
    except StopIteration as exc:
        raise RuntimeError("Notebook command completed without output markers.") from exc
    return "\n".join(lines[begin_index + 1 : end_index]).strip()


def _contents_model(http: Any, base_url: str, token: str | None, rel_path: str) -> dict[str, Any]:
    api_path = quote(rel_path.strip("/"), safe="/")
    url = f"{base_url}api/contents/{api_path}" if api_path else f"{base_url}api/contents"
    response = http.get(
        url,
        params=_token_params(token, content="0"),
        headers=_auth_headers(token),
        timeout=30,
    )
    if response.status_code == 404:
        raise FileNotFoundError(f"Remote path not found in Jupyter notebook-dir: {rel_path}")
    if response.status_code != 200:
        raise RuntimeError(f"Jupyter contents metadata returned HTTP {response.status_code}.")
    return response.json()


def _probe_files_url(http: Any, base_url: str, token: str | None, rel_path: str) -> bool:
    response = http.get(
        f"{base_url}files/{quote(rel_path, safe='/')}",
        params=_token_params(token),
        stream=True,
        timeout=(10, 30),
    )
    try:
        return response.status_code == 200
    finally:
        response.close()


def _remove_partial_download(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _range_server_script() -> str:
    return r"""import argparse
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

parser = argparse.ArgumentParser()
parser.add_argument("--file", required=True)
parser.add_argument("--port", required=True, type=int)
args = parser.parse_args()
root = os.getcwd()
filename = os.path.basename(args.file)
path = os.path.abspath(os.path.join(root, filename))

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_HEAD(self):
        self._send(send_body=False)

    def do_GET(self):
        self._send(send_body=True)

    def _send(self, send_body):
        requested = os.path.basename(unquote(urlparse(self.path).path))
        if requested != filename:
            self.send_error(404)
            return
        size = os.path.getsize(path)
        start = 0
        end = size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header:
            try:
                unit, value = range_header.split("=", 1)
                if unit.strip() != "bytes":
                    raise ValueError
                first, last = value.split("-", 1)
                start = int(first) if first else 0
                end = int(last) if last else size - 1
                if start < 0 or end < start or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206
            except Exception:
                self.send_error(400)
                return
        length = 0 if size == 0 else end - start + 1
        self.send_response(status)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not send_body or length <= 0:
            return
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
"""


def _remote_path_type(
    *,
    notebook_id: str,
    session: WebSession,
    remote_path: str,
    timeout: int,
    run_command_fn: Callable[..., dict[str, Any]],
) -> str:
    quoted = _remote_quote(remote_path)
    command = (
        f"if [ -f {quoted} ]; then printf file; "
        f"elif [ -d {quoted} ]; then printf directory; "
        f"elif [ -e {quoted} ]; then printf other; "
        "else printf missing; fi"
    )
    value = _run_marked_shell_text(
        notebook_id=notebook_id,
        session=session,
        command=command,
        timeout=timeout,
        run_command_fn=run_command_fn,
    )
    return value.splitlines()[-1].strip() if value else "missing"


def _notebook_dir(
    *,
    notebook_id: str,
    session: WebSession,
    timeout: int,
    run_command_fn: Callable[..., dict[str, Any]],
) -> str:
    value = _run_marked_shell_text(
        notebook_id=notebook_id,
        session=session,
        command="pwd -P",
        timeout=timeout,
        run_command_fn=run_command_fn,
    )
    path = value.splitlines()[-1].strip() if value else ""
    if not path.startswith("/"):
        raise RuntimeError("Could not determine notebook Jupyter root directory.")
    return path


def _stage_directory(
    *,
    notebook_id: str,
    session: WebSession,
    notebook_dir: str,
    remote_path: str,
    timeout: int,
    run_command_fn: Callable[..., dict[str, Any]],
) -> StageInfo:
    archive_name = _safe_stage_name(remote_path, suffix=".tar.gz")
    rel_path = f"{STAGING_DIR}/{archive_name}"
    abs_path = _join_remote(notebook_dir, rel_path)
    command = (
        f"mkdir -p {_remote_quote(_join_remote(notebook_dir, STAGING_DIR))} && "
        f"tar -C {_remote_quote(posixpath.dirname(remote_path.rstrip('/')) or '/')} "
        f"-czf {_remote_quote(abs_path)} {_remote_quote(posixpath.basename(remote_path.rstrip('/')))}"
    )
    _run_marked_shell(
        notebook_id=notebook_id,
        session=session,
        command=command,
        timeout=timeout,
        run_command_fn=run_command_fn,
    )
    return StageInfo(rel_path=rel_path, abs_path=abs_path, archive=True, method="tar")


def _find_reusable_proxy_archive(
    *,
    notebook_id: str,
    session: WebSession,
    stage_dir: str,
    stable_archive_name: str,
    timeout: int,
    run_command_fn: Callable[..., dict[str, Any]],
) -> str | None:
    stable_archive = _join_remote(stage_dir, stable_archive_name)
    command = (
        f"stage_dir={_remote_quote(stage_dir)}\n"
        f"stable_archive={_remote_quote(stable_archive)}\n"
        'mkdir -p "$stage_dir"\n'
        'if [ -s "$stable_archive" ]; then\n'
        '  basename "$stable_archive"\n'
        "fi"
    )
    output = _run_marked_shell_text(
        notebook_id=notebook_id,
        session=session,
        command=command,
        timeout=timeout,
        run_command_fn=run_command_fn,
    )
    archive_name = output.splitlines()[-1].strip() if output else ""
    if not archive_name:
        return None
    if "/" in archive_name or archive_name in {".", ".."} or not archive_name.endswith(".tar.gz"):
        raise RuntimeError(f"Invalid staged archive name from notebook: {archive_name}")
    return archive_name


def _stage_absolute_directory_via_proxy(
    *,
    notebook_id: str,
    session: WebSession,
    base_url: str,
    remote_path: str,
    timeout: int,
    run_command_fn: Callable[..., dict[str, Any]],
) -> StageInfo:
    parent = posixpath.dirname(remote_path.rstrip("/")) or "/"
    stage_dir = _join_remote(parent, STAGING_DIR)
    stable_archive_name = _stable_stage_name(remote_path, suffix=".tar.gz")
    archive_name = _find_reusable_proxy_archive(
        notebook_id=notebook_id,
        session=session,
        stage_dir=stage_dir,
        stable_archive_name=stable_archive_name,
        timeout=timeout,
        run_command_fn=run_command_fn,
    )
    method = "tar_proxy_reuse" if archive_name else "tar_proxy"
    if archive_name is None:
        archive_name = stable_archive_name
    archive_abs = _join_remote(stage_dir, archive_name)
    pid_path = _join_remote(stage_dir, f".{archive_name}.http.pid")
    log_path = _join_remote(stage_dir, f".{archive_name}.http.log")
    server_path = _join_remote(stage_dir, f".{archive_name}.range_server.py")
    port = random.randint(30000, 59999)
    script = _range_server_script()
    tmp_archive_abs = f"{archive_abs}.tmp.{uuid.uuid4().hex[:12]}"
    archive_ready_command = (
        f"(tmp_archive={_remote_quote(tmp_archive_abs)}; "
        'rm -f "$tmp_archive"; '
        f'tar -C {_remote_quote(parent)} -czf "$tmp_archive" '
        f"{_remote_quote(posixpath.basename(remote_path.rstrip('/')))} && "
        f'mv -f "$tmp_archive" {_remote_quote(archive_abs)}; '
        "rc=$?; "
        'if [ "$rc" -ne 0 ]; then rm -f "$tmp_archive"; fi; '
        'exit "$rc")'
        if method == "tar_proxy"
        else f"test -s {_remote_quote(archive_abs)}"
    )
    command = (
        f"mkdir -p {_remote_quote(stage_dir)} && "
        f"{archive_ready_command} && "
        f"cat > {_remote_quote(server_path)} <<'PY'\n"
        f"{script}\n"
        "PY\n"
        f"cd {_remote_quote(stage_dir)} && "
        f"(python3 {_remote_quote(server_path)} --file {_remote_quote(archive_name)} --port {port} "
        f"> {_remote_quote(log_path)} 2>&1 & echo $! > {_remote_quote(pid_path)}) && "
        "sleep 1 && "
        f"kill -0 $(cat {_remote_quote(pid_path)})"
    )
    _run_marked_shell(
        notebook_id=notebook_id,
        session=session,
        command=command,
        timeout=timeout,
        run_command_fn=run_command_fn,
    )
    cleanup_command = (
        f"if [ -f {_remote_quote(pid_path)} ]; then "
        f"kill $(cat {_remote_quote(pid_path)}) 2>/dev/null || true; "
        "fi; "
        f"rm -f {_remote_quote(archive_abs)} {_remote_quote(pid_path)} "
        f"{_remote_quote(log_path)} {_remote_quote(server_path)}"
    )
    failure_cleanup_command = (
        f"if [ -f {_remote_quote(pid_path)} ]; then "
        f"kill $(cat {_remote_quote(pid_path)}) 2>/dev/null || true; "
        "fi; "
        f"rm -f {_remote_quote(pid_path)} {_remote_quote(log_path)} {_remote_quote(server_path)}"
    )
    download_url = f"{base_url}proxy/{port}/{quote(archive_name)}"
    return StageInfo(
        rel_path=archive_name,
        abs_path=archive_abs,
        archive=True,
        method=method,
        download_url=download_url,
        cleanup_command=cleanup_command,
        failure_cleanup_command=failure_cleanup_command,
    )


def _stage_absolute_file(
    *,
    notebook_id: str,
    session: WebSession,
    http: Any,
    base_url: str,
    token: str | None,
    notebook_dir: str,
    remote_path: str,
    timeout: int,
    run_command_fn: Callable[..., dict[str, Any]],
) -> StageInfo:
    stage_name = _safe_stage_name(remote_path)
    rel_path = f"{STAGING_DIR}/{stage_name}"
    abs_path = _join_remote(notebook_dir, rel_path)
    stage_dir = _join_remote(notebook_dir, STAGING_DIR)
    symlink_command = (
        f"mkdir -p {_remote_quote(stage_dir)} && "
        f"rm -f {_remote_quote(abs_path)} && "
        f"ln -s {_remote_quote(remote_path)} {_remote_quote(abs_path)}"
    )
    _run_marked_shell(
        notebook_id=notebook_id,
        session=session,
        command=symlink_command,
        timeout=timeout,
        run_command_fn=run_command_fn,
    )
    if _probe_files_url(http, base_url, token, rel_path):
        return StageInfo(rel_path=rel_path, abs_path=abs_path, archive=False, method="symlink")

    copy_command = (
        f"rm -f {_remote_quote(abs_path)} && "
        f"cp -f {_remote_quote(remote_path)} {_remote_quote(abs_path)}"
    )
    _run_marked_shell(
        notebook_id=notebook_id,
        session=session,
        command=copy_command,
        timeout=timeout,
        run_command_fn=run_command_fn,
    )
    return StageInfo(rel_path=rel_path, abs_path=abs_path, archive=False, method="copy")


def _resolve_remote_download(
    *,
    notebook_id: str,
    session: WebSession,
    http: Any,
    base_url: str,
    token: str | None,
    remote_path: str,
    timeout: int,
    run_command_fn: Callable[..., dict[str, Any]],
) -> tuple[str, str, StageInfo | None]:
    remote_path = remote_path.strip()
    if not remote_path:
        raise ValueError("Remote path cannot be empty.")

    if remote_path.startswith("/"):
        path_type = _remote_path_type(
            notebook_id=notebook_id,
            session=session,
            remote_path=remote_path,
            timeout=timeout,
            run_command_fn=run_command_fn,
        )
        if path_type == "missing":
            raise FileNotFoundError(f"Remote path not found: {remote_path}")
        if path_type == "other":
            raise RuntimeError(f"Remote path is not a regular file or directory: {remote_path}")
        if path_type == "directory":
            stage = _stage_absolute_directory_via_proxy(
                notebook_id=notebook_id,
                session=session,
                base_url=base_url,
                remote_path=remote_path,
                timeout=timeout,
                run_command_fn=run_command_fn,
            )
            return stage.rel_path, f"{PurePosixPath(remote_path).name}.tar.gz", stage
        direct_rel_path = remote_path.lstrip("/")
        if _probe_files_url(http, base_url, token, direct_rel_path):
            return direct_rel_path, PurePosixPath(remote_path).name, None
        notebook_dir = _notebook_dir(
            notebook_id=notebook_id,
            session=session,
            timeout=timeout,
            run_command_fn=run_command_fn,
        )
        stage = _stage_absolute_file(
            notebook_id=notebook_id,
            session=session,
            http=http,
            base_url=base_url,
            token=token,
            notebook_dir=notebook_dir,
            remote_path=remote_path,
            timeout=timeout,
            run_command_fn=run_command_fn,
        )
        return stage.rel_path, PurePosixPath(remote_path).name, stage

    rel_path = posixpath.normpath(remote_path)
    if rel_path.startswith("../") or rel_path == "..":
        raise ValueError("Remote relative path cannot escape the Jupyter notebook-dir.")
    model = _contents_model(http, base_url, token, rel_path)
    model_type = str(model.get("type") or "")
    if model_type == "file":
        return rel_path, PurePosixPath(rel_path).name, None
    if model_type == "directory":
        notebook_dir = _notebook_dir(
            notebook_id=notebook_id,
            session=session,
            timeout=timeout,
            run_command_fn=run_command_fn,
        )
        stage = _stage_directory(
            notebook_id=notebook_id,
            session=session,
            notebook_dir=notebook_dir,
            remote_path=_join_remote(notebook_dir, rel_path),
            timeout=timeout,
            run_command_fn=run_command_fn,
        )
        return stage.rel_path, f"{PurePosixPath(rel_path).name}.tar.gz", stage
    raise RuntimeError(f"Remote path is not a regular file or directory: {remote_path}")


def _cleanup_stage(
    *,
    notebook_id: str,
    session: WebSession,
    stage: StageInfo | None,
    timeout: int,
    run_command_fn: Callable[..., dict[str, Any]],
) -> str:
    if stage is None:
        return "not_needed"
    try:
        cleanup_command = stage.cleanup_command or f"rm -f {_remote_quote(stage.abs_path)}"
        _run_marked_shell(
            notebook_id=notebook_id,
            session=session,
            command=cleanup_command,
            timeout=timeout,
            run_command_fn=run_command_fn,
        )
        return "deleted"
    except Exception:
        return "failed"


def _cleanup_stage_after_failure(
    *,
    notebook_id: str,
    session: WebSession,
    stage: StageInfo,
    timeout: int,
    run_command_fn: Callable[..., dict[str, Any]],
) -> str:
    cleanup_stage = (
        replace(stage, cleanup_command=stage.failure_cleanup_command)
        if stage.failure_cleanup_command
        else stage
    )
    return _cleanup_stage(
        notebook_id=notebook_id,
        session=session,
        stage=cleanup_stage,
        timeout=timeout,
        run_command_fn=run_command_fn,
    )


def _parse_content_range_total(value: str | None) -> int | None:
    if not value:
        return None
    match = re.match(r"bytes\s+\d+-\d+/(\d+|\*)", value.strip())
    if not match or match.group(1) == "*":
        return None
    return int(match.group(1))


def _download_response_body(
    *,
    response: Any,
    partial: Path,
    chunk_size: int,
) -> tuple[int, int | None, float]:
    if response.status_code != 200:
        raise RuntimeError(f"Jupyter file download returned HTTP {response.status_code}.")
    content_length_text = response.headers.get("content-length")
    content_length = int(content_length_text) if content_length_text else None
    bytes_written = 0
    start = time.monotonic()
    with partial.open("wb") as handle:
        for chunk in response.iter_content(chunk_size):
            if not chunk:
                continue
            handle.write(chunk)
            bytes_written += len(chunk)
    elapsed = max(time.monotonic() - start, 0.001)
    return bytes_written, content_length, elapsed


def _download_range_url(
    *,
    http: Any,
    url: str,
    token: str | None,
    partial: Path,
    chunk_size: int,
    timeout: int,
    retries: int = DEFAULT_RANGE_RETRIES,
) -> tuple[int, int | None, float]:
    for attempt in range(retries + 1):
        probe = None
        try:
            probe = http.get(
                url,
                params=_token_params(token),
                headers={**_auth_headers(token), "Range": "bytes=0-0"},
                stream=True,
                timeout=(10, timeout),
            )
            if probe.status_code != 206:
                raise RuntimeError(
                    f"Jupyter proxy range probe returned HTTP {probe.status_code}; "
                    "cannot safely resume large download."
                )
            content_length = _parse_content_range_total(probe.headers.get("content-range"))
            if content_length is None:
                raise RuntimeError("Jupyter proxy range probe did not return total content length.")
            first_byte = b"".join(probe.iter_content(1))
            break
        except Exception:
            if attempt >= retries:
                raise
            time.sleep(min(2**attempt, 10))
        finally:
            if probe is not None:
                probe.close()

    assert content_length is not None
    start = time.monotonic()
    if content_length == 0:
        partial.write_bytes(b"")
        return 0, 0, max(time.monotonic() - start, 0.001)

    resume_offset = partial.stat().st_size if partial.exists() else 0
    if resume_offset > content_length:
        partial.unlink()
        resume_offset = 0
    if resume_offset == content_length:
        return content_length, content_length, max(time.monotonic() - start, 0.001)

    mode = "r+b" if resume_offset else "wb"
    with partial.open(mode) as handle:
        if resume_offset:
            offset = resume_offset
        elif first_byte:
            handle.write(first_byte[:1])
            offset = 1
        else:
            offset = 0

        while offset < content_length:
            end = min(offset + chunk_size - 1, content_length - 1)
            attempt = 0
            while True:
                response = None
                range_start = offset
                try:
                    response = http.get(
                        url,
                        params=_token_params(token),
                        headers={
                            **_auth_headers(token),
                            "Range": f"bytes={range_start}-{end}",
                        },
                        stream=True,
                        timeout=(10, timeout),
                    )
                    if response.status_code != 206:
                        raise RuntimeError(
                            f"Jupyter proxy range download returned HTTP {response.status_code}."
                        )
                    handle.seek(range_start)
                    for chunk in response.iter_content(chunk_size):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        range_start += len(chunk)
                    offset = range_start
                    if offset > end:
                        break
                    raise RuntimeError("Jupyter proxy range download ended before requested range.")
                except Exception:
                    attempt += 1
                    offset = range_start
                    if attempt > retries:
                        raise
                    time.sleep(min(2**attempt, 10))
                finally:
                    if response is not None:
                        response.close()

    elapsed = max(time.monotonic() - start, 0.001)
    return content_length, content_length, elapsed


def download_from_notebook(
    *,
    notebook_id: str,
    session: WebSession,
    remote_path: str,
    local_path: str,
    timeout: int = DEFAULT_DOWNLOAD_TIMEOUT,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    keep_staged: bool = False,
    run_command_fn: Callable[..., dict[str, Any]] = run_command_in_notebook,
) -> NotebookDownloadResult:
    lab_url = resolve_lab_url(notebook_id, session=session, auth_method="http", timeout=timeout)
    base_url = _jupyter_server_base(lab_url)
    token = _extract_jupyter_token(lab_url)
    http = build_requests_session(session, base_url)

    stage: StageInfo | None = None
    try:
        files_rel_path, download_name, stage = _resolve_remote_download(
            notebook_id=notebook_id,
            session=session,
            http=http,
            base_url=base_url,
            token=token,
            remote_path=remote_path,
            timeout=timeout,
            run_command_fn=run_command_fn,
        )
        destination = _local_destination(local_path, download_name)
        # The digest binds the partial to its source: resuming is only safe when the bytes on disk
        # came from this notebook and this remote path, not merely from a file of the same name.
        partial = destination.with_name(
            f"{destination.name}.{_partial_digest(notebook_id, remote_path)}.part"
        )
        if stage is not None and stage.method == "tar_proxy" and partial.exists():
            # This run packed a fresh archive, so it shares no bytes with the archive an earlier
            # run staged under the same stable name; resuming onto those bytes would stitch two
            # generations together into one archive that untars into neither.
            logger.debug(
                "Discarding stale partial %s: %s was repacked as a new archive generation",
                partial,
                _canonical_remote_path(remote_path),
            )
            _remove_partial_download(partial)
        range_download = bool(stage and stage.download_url)
        try:
            download_url = (
                stage.download_url
                if stage and stage.download_url
                else f"{base_url}files/{quote(files_rel_path, safe='/')}"
            )
            if stage and stage.download_url:
                bytes_written, content_length, elapsed = _download_range_url(
                    http=http,
                    url=download_url,
                    token=token,
                    partial=partial,
                    chunk_size=chunk_size,
                    timeout=timeout,
                )
            else:
                response = http.get(
                    download_url,
                    params=_token_params(token),
                    stream=True,
                    timeout=(10, timeout),
                )
                try:
                    bytes_written, content_length, elapsed = _download_response_body(
                        response=response,
                        partial=partial,
                        chunk_size=chunk_size,
                    )
                finally:
                    response.close()
        except Exception:
            if not range_download:
                _remove_partial_download(partial)
            raise
        partial.replace(destination)
        cleanup_status = "not_needed"
        if stage is not None:
            cleanup_status = (
                "kept"
                if keep_staged
                else _cleanup_stage(
                    notebook_id=notebook_id,
                    session=session,
                    stage=stage,
                    timeout=timeout,
                    run_command_fn=run_command_fn,
                )
            )
        return NotebookDownloadResult(
            notebook_id=notebook_id,
            remote_path=remote_path,
            local_path=str(destination),
            bytes=bytes_written,
            content_length=content_length,
            elapsed_s=round(elapsed, 3),
            mib_per_s=round(bytes_written / 1024 / 1024 / elapsed, 2),
            transport=(
                "jupyter_http_proxy_range" if stage and stage.download_url else "jupyter_http_files"
            ),
            staged=stage is not None,
            archive=bool(stage and stage.archive),
            cleanup_status=cleanup_status,
            stage_method=stage.method if stage is not None else None,
        )
    except Exception:
        if stage is not None and not keep_staged:
            _cleanup_stage_after_failure(
                notebook_id=notebook_id,
                session=session,
                stage=stage,
                timeout=timeout,
                run_command_fn=run_command_fn,
            )
        raise


def _resolve_target_notebook(
    ctx: Context,
    *,
    notebook: str | None,
    bridge: str | None,
    session: WebSession,
    json_output: bool,
) -> tuple[str, str | None]:
    if notebook and bridge:
        _handle_error(
            ctx,
            "ConfigError",
            "Use either --notebook/-n or --bridge/-b, not both.",
            EXIT_CONFIG_ERROR,
        )
    if notebook:
        config = load_config(ctx)
        notebook_id, _ = _resolve_notebook_id(
            ctx,
            session=session,
            config=config,
            base_url=get_base_url(),
            identifier=notebook,
            json_output=json_output,
        )
        return notebook_id, None
    if bridge:
        tunnel_config = load_tunnel_config()
        profile = tunnel_config.get_bridge(bridge)
        if profile is None:
            _handle_error(
                ctx,
                "ConfigError",
                f"Bridge profile '{bridge}' not found.",
                EXIT_CONFIG_ERROR,
                hint="Run 'inspire tunnel list' to see available bridges.",
            )
        profile_notebook_id = getattr(profile, "notebook_id", None)
        if not profile_notebook_id:
            _handle_error(
                ctx,
                "ConfigError",
                f"Bridge profile '{bridge}' has no notebook_id binding.",
                EXIT_CONFIG_ERROR,
                hint="Use --notebook <id-or-name> or re-create the bridge with notebook ssh.",
            )
        return str(profile_notebook_id), bridge
    _handle_error(
        ctx,
        "ConfigError",
        "Must specify either --notebook/-n or --bridge/-b.",
        EXIT_CONFIG_ERROR,
        hint="Use --notebook <id-or-name> for no-tunnel Jupyter HTTP download.",
    )
    raise AssertionError("unreachable")


@click.command("download")
@click.argument("remote_path")
@click.argument("local_path")
@click.option(
    "--notebook",
    "-n",
    "notebook",
    default=None,
    help="Notebook ID or name to download from (no SSH tunnel required)",
)
@click.option(
    "--bridge",
    "-b",
    "bridge",
    default=None,
    help="Bridge profile name (uses its notebook_id only; no SSH tunnel transfer)",
)
@click.option(
    "--timeout",
    type=int,
    default=DEFAULT_DOWNLOAD_TIMEOUT,
    show_default=True,
    help="Timeout in seconds for Jupyter setup, staging, and stream reads",
)
@click.option(
    "--chunk-size",
    type=int,
    default=DEFAULT_CHUNK_SIZE,
    show_default=True,
    help="HTTP streaming chunk size in bytes",
)
@click.option(
    "--keep-staged",
    is_flag=True,
    help=f"Keep temporary remote files under {STAGING_DIR}/ after download",
)
@pass_context
def download_notebook_cmd(
    ctx: Context,
    remote_path: str,
    local_path: str,
    notebook: Optional[str],
    bridge: Optional[str],
    timeout: int,
    chunk_size: int,
    keep_staged: bool,
) -> None:
    """Download a file or directory from a notebook via Jupyter HTTP.

    This does not use SSH, bridge scp, or rtunnel. Files under the Jupyter
    root stream through /files. Absolute directories are tarred in place and
    streamed through Jupyter /proxy using range requests so large checkpoints
    do not need free space under the Jupyter root. Interrupted proxy downloads
    can resume from the local .part file, whose name carries a digest of the
    source notebook and remote path, and reuse a staged archive only when it is
    the archive this source would have written itself. A run that packs a fresh
    archive starts its local download over instead of resuming superseded bytes.
    """
    json_output = resolve_json_output(ctx, False)
    if chunk_size <= 0:
        _handle_error(ctx, "ValidationError", "--chunk-size must be positive", EXIT_GENERAL_ERROR)

    session = require_web_session(
        ctx,
        hint=(
            "Notebook download requires web authentication. "
            "Set [auth].username/password in config.toml or INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )
    notebook_id, via_bridge = _resolve_target_notebook(
        ctx,
        notebook=notebook,
        bridge=bridge,
        session=session,
        json_output=json_output,
    )

    if not json_output:
        emit_progress(ctx, f"Downloading from notebook '{notebook_id}' via Jupyter HTTP...")

    try:
        result = download_from_notebook(
            notebook_id=notebook_id,
            session=session,
            remote_path=remote_path,
            local_path=local_path,
            timeout=timeout,
            chunk_size=chunk_size,
            keep_staged=keep_staged,
        )
    except FileNotFoundError as exc:
        _handle_error(ctx, "NotFound", str(exc), EXIT_API_ERROR)
    except ValueError as exc:
        _handle_error(ctx, "ValidationError", str(exc), EXIT_GENERAL_ERROR)
    except Exception as exc:
        _handle_error(
            ctx,
            "DownloadError",
            f"Failed to download from notebook: {exc}",
            EXIT_API_ERROR,
            hint="Ensure the notebook is RUNNING and JupyterLab is accessible.",
        )

    payload = result.as_payload()
    payload["bridge"] = via_bridge
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return

    if result.cleanup_status == "failed":
        emit_warning(ctx, "Downloaded file, but failed to delete the temporary staged remote file.")
    click.echo(f"Downloaded {result.bytes} bytes to {result.local_path}")


__all__ = [
    "NotebookDownloadResult",
    "download_from_notebook",
    "download_notebook_cmd",
]
