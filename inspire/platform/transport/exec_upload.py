"""Upload bytes to a remote filesystem through an exec transport.

Why this exists
---------------
The platform's storage surface (WebDAV, ``file-server.sii.edu.cn:81``) denies ``PUT`` for ordinary
accounts - measured again on 2026-09-28: ``MKCOL`` under our own user path **succeeds**, ``PUT`` returns
``403`` - and the vendor's own docs show the web console shipping bytes over that same ``PUT``, so there
is no native upload API waiting to be found (Codeberg issues #26, #37). What *is* writable is the pod
itself: it has the shared filesystem mounted, and we have a command channel into it (PTY exec for
training jobs, Jupyter for notebooks). So an upload is:

    stream bytes over the exec channel, let the pod write them, verify, then rename.

Design rules, each traceable to a measured failure
--------------------------------------------------
* **Chunked**, one exec call per chunk, so no command grows with the file.
* **Paced, and restartable.** On a live pod, exec round trips succeed at 45s spacing (6 ok / 1 drop) but
  go bad after back-to-back calls (2 ok then 3 drops), and the bad state persists for minutes. A naive
  chunk loop *is* a burst, i.e. the uploader would create its own failure. So chunk calls are spaced, and
  a transient drop restarts the transfer from chunk 1 rather than resuming mid-file.
* **Never resume a partially appended chunk.** Retrying ``>>`` after a lost session can duplicate bytes
  silently, and the whole point of this module is that a truncated upload must never look like success.
* **Integrity-checked**: the remote digest is compared with the local one before the file appears at its
  final name.
* **``.part`` then rename**, so a reader never observes a half-written file.
* **Refuse to overwrite by default.** These paths are shared with humans and other agents.
* **Size cap enforced before touching the remote.**
* **Errors never carry content - including its base64.** The first version of this module quoted the
  failing command in chunk errors, which meant a chunk of the file's base64 went to stderr. Verified live
  on 2026-09-28. Commands are therefore described, never echoed.

The transport is injected as a callable, which is what makes all of this testable without a cluster.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import posixpath
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

#: Refuse to upload larger files by default; the exec channel is not a bulk transfer path.
DEFAULT_MAX_BYTES = 32 * 1024 * 1024

#: Raw bytes per exec call. The binding limit is not ARG_MAX: a live pod refused a 64 KiB base64 line,
#: which matches a PTY in canonical mode hitting its line limit (`MAX_CANON`, typically 4096 bytes). The
#: exact ceiling is still unmeasured because the channel, not the size, was the failing variable.
DEFAULT_CHUNK_BYTES = 3 * 1024

#: Per-command wall clock.
DEFAULT_COMMAND_TIMEOUT = 60

#: Seconds between chunk calls. 0 keeps the primitive neutral; the CLI paces by default, because
#: back-to-back exec calls are what break the channel.
DEFAULT_CHUNK_DELAY = 0.0

#: Whole-transfer attempts after a transient transport drop.
DEFAULT_MAX_ATTEMPTS = 3

TRANSIENT_MARKERS = (
    "connection to remote host was lost",
    "connection reset",
    "broken pipe",
    "temporarily unavailable",
    "connection refused",
)

#: ``(command, timeout_seconds) -> (exit_code, stdout)``.
Runner = Callable[[str, int], "tuple[int, str]"]


class ExecUploadError(RuntimeError):
    """An upload failed, or was refused before it could be trusted."""

    def __init__(
        self, message: str, *, remote_path: str | None = None, transient: bool = False
    ) -> None:
        super().__init__(message)
        self.remote_path = remote_path
        self.transient = transient


@dataclass(frozen=True)
class ExecUploadResult:
    """What an upload actually did - enough to report it without re-reading the file."""

    remote_path: str
    bytes: int
    chunks: int
    local_sha256: str
    remote_sha256: str
    transport: str = "exec"
    attempts: int = 1


def looks_transient(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in TRANSIENT_MARKERS)


def _clip(value: str, limit: int = 160) -> str:
    """Shorten remote text without cutting the part that identifies the failure."""
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit // 2]} … {collapsed[-limit // 2 :]}"


def _run_ok(
    runner: Runner,
    command: str,
    *,
    timeout: int,
    what: str,
    include_output: bool = False,
    attempts: int = 1,
    retry_delay: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Run one remote command. Never mentions the command text: it may contain file content.

    ``attempts`` applies only to calls that are safe to repeat: read-only probes and ``mkdir -p``. The field
    team's pod showed the exec channel dropping sessions even 45 seconds apart, so a single unlucky drop in
    the *pre-flight* used to abort an upload that had not written a byte - while the chunk loop, which is the
    part that actually accumulates state, had a retry budget. That is backwards. A rename is still never
    retried here: a lost response to `mv` cannot be told apart from a rename that happened.
    """
    last_code, last_stdout = 0, ""
    for attempt in range(1, max(1, attempts) + 1):
        last_code, last_stdout = runner(command, timeout)
        if last_code == 0:
            return last_stdout
        transient = looks_transient(last_stdout) or last_code == -1
        if not transient or attempt == attempts:
            detail = f": {_clip(last_stdout)}" if include_output and last_stdout.strip() else ""
            suffix = "" if attempt == 1 else f" after {attempt} attempts"
            raise ExecUploadError(
                f"{what} failed (exit {last_code}){detail}{suffix}",
                transient=transient,
            )
        sleep(retry_delay)
    raise ExecUploadError(
        f"{what} failed (exit {last_code})"
    )  # unreachable; keeps the return type honest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def upload_via_exec(
    runner: Runner,
    local_path: str | Path,
    remote_path: str,
    *,
    overwrite: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    command_timeout: int = DEFAULT_COMMAND_TIMEOUT,
    chunk_delay: float = DEFAULT_CHUNK_DELAY,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base: float = 2.0,
    backoff_cap: float = 30.0,
    transport: str = "exec",
    progress: "Callable[[int, int], None] | None" = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ExecUploadResult:
    """Write ``local_path`` to ``remote_path`` by having the far side write it.

    ``runner`` executes one shell command remotely and returns ``(exit_code, stdout)``.

    A transient transport drop restarts the transfer from the first chunk. That is deliberately wasteful:
    resuming after a lost session means re-issuing an append whose bytes may or may not have landed, and
    "may have landed twice" is exactly the silent corruption this module exists to prevent.
    """

    source = Path(local_path).expanduser()
    if not source.is_file():
        raise ExecUploadError(f"local file not found: {source}")
    if not remote_path.startswith("/"):
        raise ExecUploadError(f"remote path must be absolute, got: {remote_path}")

    size = source.stat().st_size
    if size > max_bytes:
        raise ExecUploadError(
            f"refusing to upload {size} bytes over the exec channel (limit {max_bytes}); use the web "
            "console, or raise --max-mb only if you know the channel can take it"
        )

    remote = posixpath.normpath(remote_path)
    remote_dir = posixpath.dirname(remote) or "/"
    local_sha = sha256_file(source)
    # A random marker per call means a watchdog that retries this transfer N times leaves N inert `.part`
    # files on the pod, and the cleanup is itself an exec call - unavailable exactly when it is needed. So
    # the name is derived from the transfer instead: same source bytes, same destination, same hour reuses
    # one name, which the restart-from-chunk-1 truncates rather than adding to. It deliberately excludes the
    # pid, because the retry that needs the dedup runs in a new process.
    # Two concurrent uploads of the same file to the same target can therefore collide on one partial name.
    # That costs a failed digest comparison, never a corrupted destination: the rename happens only after the
    # remote digest matches, which is the invariant this whole module is built on.
    hour_bucket = int(time.time() // 3600)
    marker = hashlib.sha256(f"{remote}|{local_sha}|{size}|{hour_bucket}".encode()).hexdigest()[:8]
    part = f"{remote}.part.{marker}"

    # Refuse to clobber before creating anything, so a refusal leaves no traces either.
    exists = _run_ok(
        runner,
        f"test -e {shlex.quote(remote)} && echo EXISTS || echo ABSENT",
        timeout=command_timeout,
        what="remote existence check",
        include_output=True,
        attempts=3,
        sleep=sleep,
    )
    if "EXISTS" in exists and not overwrite:
        raise ExecUploadError(
            f"remote path already exists: {remote} (pass overwrite to replace it)",
            remote_path=remote,
        )

    _run_ok(
        runner,
        f"mkdir -p {shlex.quote(remote_dir)}",
        timeout=command_timeout,
        what="remote mkdir",
        include_output=True,
        attempts=3,
        sleep=sleep,
    )
    writable = _run_ok(
        runner,
        f"test -w {shlex.quote(remote_dir)} && echo WRITABLE || echo NOT_WRITABLE",
        timeout=command_timeout,
        what="remote writability check",
        include_output=True,
        attempts=3,
        sleep=sleep,
    )
    if "WRITABLE" not in writable or "NOT_WRITABLE" in writable:
        raise ExecUploadError(
            f"remote directory is not writable by the pod: {remote_dir}", remote_path=remote
        )

    last_error: ExecUploadError | None = None

    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            sent, chunks = _send_and_verify(
                runner,
                source=source,
                size=size,
                local_sha=local_sha,
                part=part,
                remote=remote,
                chunk_bytes=chunk_bytes,
                command_timeout=command_timeout,
                chunk_delay=chunk_delay,
                transport=transport,
                progress=progress,
                sleep=sleep,
            )
            return ExecUploadResult(
                remote_path=remote,
                bytes=sent,
                chunks=chunks,
                local_sha256=local_sha,
                remote_sha256=local_sha,
                transport=transport,
                attempts=attempt,
            )
        except ExecUploadError as exc:
            # Only ever our own .part name - never a path the caller named.
            if not exc.transient or attempt >= max_attempts:
                _cleanup(runner, part, command_timeout)
                raise
            last_error = exc
            delay = min(backoff_cap, backoff_base * (2 ** (attempt - 1)))
            logger.debug(
                "exec upload attempt %d dropped (%s); restarting from chunk 1 in %.1fs",
                attempt,
                exc,
                delay,
            )
            if progress is not None:
                progress(0, size)
            sleep(delay)

    raise ExecUploadError(  # pragma: no cover - the loop raises or returns first
        f"upload failed after {max_attempts} attempts: {last_error}"
    )


def _send_and_verify(
    runner: Runner,
    *,
    source: Path,
    size: int,
    local_sha: str,
    part: str,
    remote: str,
    chunk_bytes: int,
    command_timeout: int,
    chunk_delay: float,
    transport: str,
    progress: "Callable[[int, int], None] | None",
    sleep: Callable[[float], None],
) -> "tuple[int, int]":
    sent = 0
    chunks = 0
    with source.open("rb") as handle:
        while True:
            block = handle.read(chunk_bytes)
            if not block:
                break
            chunks += 1
            if chunks > 1 and chunk_delay > 0:
                # Back-to-back exec calls are what break the channel; the delay is cheap insurance.
                sleep(chunk_delay)
            encoded = base64.b64encode(block).decode("ascii")
            redirect = ">" if chunks == 1 else ">>"
            try:
                _run_ok(
                    runner,
                    f"printf %s '{encoded}' | base64 -d {redirect} {shlex.quote(part)}",
                    timeout=command_timeout,
                    what=f"remote chunk {chunks} ({len(block)} bytes)",
                )
            except ExecUploadError as exc:
                # Name the partial file. Debris is an acceptable outcome when the channel is down - the
                # command that would remove it is itself an exec call - but SILENT debris is not: nobody on
                # the far side can delete a path they were never given. The path carries no file content, so
                # unlike the command text this is safe to surface.
                raise ExecUploadError(
                    f"{exc}; incomplete remote file left at {part}",
                    remote_path=part,
                    transient=exc.transient,
                ) from None
            sent += len(block)
            if progress is not None:
                progress(sent, size)

    digest_out = _run_ok(
        runner,
        # Read-only, so a dropped session is worth one more ask: this is the call whose answer decides whether
        # the bytes are trustworthy, and losing it to a transient aborts an otherwise complete transfer.
        f"sha256sum {shlex.quote(part)} 2>/dev/null || openssl dgst -sha256 {shlex.quote(part)}",
        timeout=command_timeout,
        what="remote checksum",
        include_output=True,
        attempts=3,
        sleep=sleep,
    )
    remote_sha = _parse_digest(digest_out)
    if remote_sha != local_sha:
        raise ExecUploadError(
            f"checksum mismatch after upload: local {local_sha} remote {remote_sha or 'unreadable'}; "
            f"left the incomplete file at {part} for inspection",
            remote_path=part,
        )
    _run_ok(
        runner,
        f"mv {shlex.quote(part)} {shlex.quote(remote)}",
        timeout=command_timeout,
        what="remote rename into place",
        include_output=True,
    )
    return sent, chunks


def _cleanup(runner: Runner, part: str, timeout: int) -> None:
    try:
        runner(f"rm -f {shlex.quote(part)}", timeout)
    except Exception:  # pragma: no cover - cleanup is best effort
        logger.debug("exec upload cleanup failed for %s", part, exc_info=True)


def _parse_digest(stdout: str) -> str:
    """Read a sha256 out of ``sha256sum`` or ``openssl dgst -sha256`` output."""
    for line in stdout.splitlines():
        for token in reversed(line.split()):
            if re.fullmatch(r"[0-9a-f]{64}", token.lower()):
                return token.lower()
    return ""


__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_CHUNK_DELAY",
    "DEFAULT_COMMAND_TIMEOUT",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_BYTES",
    "ExecUploadError",
    "ExecUploadResult",
    "Runner",
    "looks_transient",
    "sha256_file",
    "upload_via_exec",
]
