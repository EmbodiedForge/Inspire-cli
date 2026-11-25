"""Copy a local file into a running training job through the exec channel.

Why this command exists instead of ``inspire storage put``: the platform's WebDAV surface answers
``403 Forbidden`` to ``PUT``/``MKCOL`` for our file-service identity (in every scope tried), so the
laptop cannot write ``/inspire/hdd/...`` directly - while the pod can, because it has the same
filesystem mounted. Streaming bytes over PTY exec and letting the pod write them is therefore the
upload path that works today. See Codeberg issues #26 and #37.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Callable

import click

from inspire.cli.context import EXIT_GENERAL_ERROR, Context, pass_context
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.output import emit_success
from inspire.platform.transport.exec_upload import (
    ExecUploadError,
    Runner,
    upload_via_exec,
)
from inspire.platform.web.browser_api import execute_job_command, open_job_command_session

logger = logging.getLogger(__name__)


def job_session_runner(
    job_id: str,
    *,
    worker: int = 0,
    instance_name: str | None = None,
    attempt: int | None = None,
) -> "contextlib.AbstractContextManager[Any]":
    """A runner backed by ONE PTY session for the whole operation.

    This is the difference between an upload that works and one that cannot. Per command, `execute_job_command`
    pays an instance lookup plus a WebSocket establishment; a 200 KiB transfer is ~70 commands, so the old
    pricing meant ~70 draws on a channel measured this morning as losing per-command sessions. On the same pod,
    one session then carried 83 commands, a 12 KiB and a 200 KiB upload, both verified, with zero drops.
    """
    return open_job_command_session(
        job_id, worker=worker, instance_name=instance_name, attempt=attempt
    )


def job_command_runner(
    *,
    job_id: str,
    worker: int = 0,
    instance_name: str | None = None,
    attempt: int | None = None,
) -> Runner:
    """Adapt platform PTY exec to the ``(command, timeout) -> (exit_code, stdout)`` runner.

    A missing exit code is reported as ``-1`` rather than ``0``: this session has already established
    that "we stopped waiting" is not "the command failed", so an unknown code must never be mistaken
    for a successful write.
    """

    def run(command: str, timeout: int) -> tuple[int, str]:
        result: dict[str, Any] = execute_job_command(
            job_id,
            command,
            worker=worker,
            instance_name=instance_name,
            attempt=attempt,
            timeout=float(timeout),
        )
        code = result.get("exit_code")
        stdout = str(result.get("stdout") or result.get("output") or "")
        if code is None:
            logger.debug("exec upload command finished without a reported exit code")
            return -1, stdout
        return int(code), stdout

    return run


def _human_bytes(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value} B"


@click.command("upload")
@click.argument("job_id")
@click.argument("local", type=click.Path(exists=True, dir_okay=False, allow_dash=False))
@click.argument("remote")
@click.option("--worker", default=0, show_default=True, help="Worker index to write through.")
@click.option(
    "--instance",
    "instance_name",
    default=None,
    help="Exact instance name (mutually exclusive with --worker).",
)
@click.option("--attempt", default=None, type=int, help="Fault-tolerance attempt number to target.")
@click.option(
    "--overwrite", is_flag=True, help="Replace REMOTE if it already exists (refused by default)."
)
@click.option(
    "--max-mb",
    default=32,
    show_default=True,
    type=click.IntRange(1, 2048),
    help="Refuse files larger than this; the exec channel is not a bulk transfer path.",
)
@click.option(
    "--chunk-kb",
    default=3,
    show_default=True,
    type=click.IntRange(1, 512),
    help="Kibibytes of raw content per exec call (3 means 3072 bytes). A live pod refused 64 KiB "
    "without returning an exit code, and a PTY in canonical mode refuses lines past ~4096 bytes, so "
    "the safe size sits just under that; the exact ceiling is unmeasured.",
)
@click.option(
    "--pace",
    default=0.7,
    show_default=True,
    type=click.FloatRange(0.0, 30.0),
    help="Seconds between chunk exec calls. This throttles the client; it does NOT make the job exec "
    "channel reliable. Measured on one live pod: the transfer died at chunk 3 with 0.7s, at chunk 1 "
    "with 2.5s and at chunk 3 with 10s - not monotonic, so spacing is not the cause and a larger value "
    "is not a fix. Failures name the .part path they left; clean those exact names yourself.",
)
@click.option(
    "--max-attempts",
    default=3,
    show_default=True,
    type=click.IntRange(1, 10),
    help="Whole-file attempts after a transient drop. A retry always restarts from chunk 1.",
)
@click.option(
    "--timeout",
    default=60,
    show_default=True,
    type=int,
    help="Per-command timeout. Note: a timeout does NOT undo a chunk already written remotely.",
)
@pass_context
def upload(
    ctx: Context,
    job_id: str,
    local: str,
    remote: str,
    worker: int,
    instance_name: str | None,
    attempt: int | None,
    overwrite: bool,
    max_mb: int,
    chunk_kb: int,
    pace: float,
    max_attempts: int,
    timeout: int,
) -> None:
    """Copy LOCAL into a running job at REMOTE, verified before it appears under its final name.

    The pod writes the bytes; this command streams them over the same PTY exec channel that
    `inspire job exec` uses. Transfer goes to a `<REMOTE>.part.<id>` file, the remote sha256 is
    compared with the local one, and only then is it renamed into place. An existing REMOTE is left
    untouched unless --overwrite is given, because these paths are shared with people and other
    agents.

    \b
    What is proven, and what is not, as of 2026-09-28:
      * Proven end to end over the NOTEBOOK exec transport against a running pod: 12 KiB in 4 chunks and
        200 KiB in 67 chunks each came back with a remote sha256sum equal to the local digest; re-upload
        without --overwrite refused; --overwrite replaced and re-verified; zero .part.* left behind.
      * NOT proven on the transport this command uses. The job PTY exec channel has never completed an
        upload, because no fixture job stayed admitted long enough - and the smallest legal request shape
        (`job offers`: 1 GPU forces 20 CPU and 200 GiB RAM) is exactly the shape a memory-fragmented GPU
        node cannot place.
    So the honest sentence is "the primitive is proven on one exec transport; this verb is unproven on its
    own", never "upload works".

    \b
    Size reality: each chunk is one exec round trip. At the default 3 KiB, a 3 MiB file costs about a
    thousand round trips. This is the path for small inputs - a config, a patch, a script - not for
    datasets and checkpoints; those belong on the web console or a writable file-service credential.

    \b
    Examples:
      inspire job upload job-abc ./arms.tar /inspire/hdd/project/p/own/arms.tar
      inspire job upload job-abc ./x.bin /tmp/x.bin --overwrite --max-mb 200
    """

    def _progress(sent: int, total: int) -> None:
        if sent == total or sent % (1024 * 1024) < chunk_kb * 1024:
            click.secho(
                f"uploading {_human_bytes(sent)} / {_human_bytes(total)}",
                err=True,
                fg="cyan",
                nl=sent == total,
            )

    progress: Callable[[int, int], None] | None = _progress

    try:
        # One PTY session for the whole transfer. This is not a micro-optimisation: per-command sessions were
        # measured losing the channel on this exact pod, while a single session then carried 83 commands and two
        # verified uploads without a drop.
        with job_session_runner(
            job_id, worker=worker, instance_name=instance_name, attempt=attempt
        ) as channel:

            def _run(command: str, command_timeout: int) -> tuple[int, str]:
                payload: dict[str, Any] = channel.run(command, command_timeout)
                code = payload.get("exit_code")
                return (-1 if code is None else int(code)), str(payload.get("stdout") or "")

            result = upload_via_exec(
                _run,
                local,
                remote,
                overwrite=overwrite,
                max_bytes=max_mb * 1024 * 1024,
                chunk_bytes=chunk_kb * 1024,
                command_timeout=timeout,
                chunk_delay=max(0.0, pace),
                max_attempts=max_attempts,
                transport="pty-exec",
                progress=progress,
            )
    except ExecUploadError as exc:
        # The error above now names the real partial path, so the hint must not offer a placeholder that
        # cannot be pasted into a command. Debris is acceptable; silent debris is not.
        # What may be claimed is limited to what this process observed. `ExecUploadError` carries the partial
        # path and whether the failure was transport-shaped; it carries no proof that cleanup ran - and when
        # the channel is the thing that failed, the `rm` used to be unable to run at all (tests/
        # test_exec_upload_abort.py shows a surviving `.part`). The previous hint said "the half-written file
        # was removed" on the strength of `transient` alone, which is the one case where debris is likely:
        # users stopped looking for inert files on a shared pod because we told them there were none.
        rename_claim = "nothing was renamed into place, so the target keeps its previous content."
        if exc.remote_path:
            debris = (
                f"{exc.remote_path} may still exist and is inert: remove just that name with "
                "`inspire job exec <job> -- rm -f <path>`. It was not verified as deleted."
            )
            hint = f"{rename_claim} {debris}"
        else:
            hint = (
                f"{rename_claim} No incomplete remote path was reported, so nothing here names a file to "
                "clean; re-run with a larger --chunk-kb only after checking the channel."
            )
        if exc.transient:
            # Appended, not replaced: the first draft rebuilt `hint` here and dropped the partial path the
            # reader needs, which is the same failure one level down - a correct sentence that hides the
            # actionable one.
            hint = (
                "the exec channel dropped; each retry restarted from chunk 1, so no chunk was appended "
                "twice. A drop is exactly when cleanup may not have run, so treat any partial path as "
                f"present until you remove it yourself. {hint}"
            )
        _handle_error(ctx, "UploadFailed", str(exc), EXIT_GENERAL_ERROR, hint=hint)
        return
    except ValueError as exc:  # instance selection problems from the transport layer
        _handle_error(ctx, "InstanceSelection", str(exc), EXIT_GENERAL_ERROR)
        return

    if ctx.json_output:
        # `--json` is a global contract in this CLI, and this command was born without a JSON branch: an
        # agent asking for machine-readable output got prose. The payload states what was verified, not just
        # what was attempted - the remote digest is read back from the pod before the rename.
        emit_success(
            ctx,
            payload={
                "job_id": job_id,
                "local": str(local),
                "remote_path": result.remote_path,
                "bytes": result.bytes,
                "chunks": result.chunks,
                "local_sha256": result.local_sha256,
                "attempts": result.attempts,
                "transport": result.transport,
                "remote_sha256": result.remote_sha256,
                "verified_by": "remote sha256sum compared to the local digest before the rename",
            },
        )
        return

    click.echo(
        f"{result.remote_path}  {result.bytes} bytes in {result.chunks} chunk(s)  "
        f"sha256 {result.remote_sha256}"
    )
    if result.chunks > 1:
        click.secho(
            "note: if this command is interrupted, an inert '<target>.part.<id>' file may remain on the "
            "pod; failures print the exact path, and it is ours to remove - never the target's",
            err=True,
            fg="yellow",
        )
    return


__all__ = ["job_command_runner", "upload"]
