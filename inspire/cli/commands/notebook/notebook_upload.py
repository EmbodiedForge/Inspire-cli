"""`inspire notebook upload` - the upload path that does not depend on a job's PTY channel.

Why this exists as a user-facing command: `upload_path_to_notebook` has been in the codebase the whole time
and is used internally by `bridge exec --file`, but there was no verb for it. When the field team asked how to
get a file into a container, I told them to use `inspire notebook upload` - a command that does not exist in
v0.4.0 or in HEAD. That advice was wrong in exactly the way this project keeps cataloguing: I named the
capability the plumbing implies instead of the surface the CLI actually offers.

The distinction that matters operationally: this transport is Jupyter Contents over the notebook's web proxy,
verified by SHA-256 inside the container before installation. It is NOT the job PTY exec channel, which is the
one that has never completed a transfer here and is unavailable whenever it degrades. For notebooks, this is
the path; for jobs, the shared-disk route from inside the pod remains the honest recommendation.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Optional

import click

from inspire.cli.context import EXIT_API_ERROR, EXIT_GENERAL_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.notebook_cli import require_web_session, resolve_json_output
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.output import emit_progress
from inspire.platform.web import browser_api as browser_api_module

from .notebook_download import _resolve_target_notebook


from inspire.platform.web.browser_api.playwright_notebooks import (
    run_command_in_notebook as _run_command_in_notebook,
)


@click.command("upload")
@click.argument("local_path")
@click.argument("remote_path")
@click.option("--notebook", "-n", "notebook", default=None, help="Notebook ID or name to upload to")
@click.option(
    "--bridge", "-b", "bridge", default=None, help="Bridge profile name (uses its notebook_id)"
)
@click.option(
    "--timeout", type=int, default=120, show_default=True, help="Timeout in seconds per remote step"
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Resolve and report the destination without writing anything. Use this when the destination is a "
    "shared checkout: the answer you want before an upload is 'where would this land, and is something "
    "already there'.",
)
@click.option(
    "--overwrite",
    is_flag=True,
    help="Replace REMOTE_PATH if it already exists. Off by default: a notebook's working directory is "
    "frequently somebody else's repository, and an unasked-for overwrite is unrecoverable.",
)
@pass_context
def upload_notebook_cmd(
    ctx,
    local_path: str,
    remote_path: str,
    notebook: Optional[str],
    bridge: Optional[str],
    timeout: int,
    overwrite: bool,
    dry_run: bool,
) -> None:
    """Copy LOCAL_PATH into a notebook at REMOTE_PATH, verified by SHA-256 before it is installed.

    Transfer goes through Jupyter Contents over the notebook's web proxy - no SSH tunnel, no rtunnel, and not
    the job exec PTY. The payload is archived locally, uploaded once, checksummed inside the container, and
    only then copied to REMOTE_PATH, so a truncated transfer cannot masquerade as the real file. An existing
    REMOTE_PATH is left untouched unless --overwrite is given, because notebook working directories are often
    live repositories with uncommitted work in them.
    """
    json_output = resolve_json_output(ctx, False)
    if timeout <= 0:
        _handle_error(ctx, "ValidationError", "--timeout must be positive", EXIT_GENERAL_ERROR)
        return

    session = require_web_session(
        ctx,
        hint="Notebook upload requires web authentication. Configure the Account Catalog entry and retry.",
    )
    notebook_id, via_bridge = _resolve_target_notebook(
        ctx,
        notebook=notebook,
        bridge=bridge,
        session=session,
        json_output=json_output,
    )

    import os

    source = Path(local_path).expanduser()
    if not source.exists():
        _handle_error(ctx, "NotFound", f"local path not found: {source}", EXIT_GENERAL_ERROR)
        return
    if source.is_dir():
        _handle_error(
            ctx,
            "ValidationError",
            "this verb takes a single file; for directories use `inspire sync` or the shared disk",
            EXIT_GENERAL_ERROR,
            hint="a directory upload that overwrites a tree is the one mistake this command must make impossible",
        )
        return
    dry_probe = dry_run or not overwrite
    if dry_probe:
        # The transport underneath ends in `cp -a`, which overwrites silently, so the refusal has to happen
        # here. Probing costs one round trip; the alternative is that the default behaviour of a new verb is
        # to clobber uncommitted work in somebody else's repository.
        try:
            probe = browser_api_module.execute_notebook_shell_command(
                notebook_id=notebook_id,
                command=f"test -e {shlex.quote(remote_path)} && echo EXISTS || echo ABSENT",
                session=session,
                timeout=timeout,
                run_command_fn=_run_command_in_notebook,
            )
        except Exception as exc:  # noqa: BLE001 - cannot tell the probe apart from the transport
            _handle_error(
                ctx,
                "UploadError",
                f"could not check whether {remote_path} already exists: {exc}",
                EXIT_API_ERROR,
                hint="refusing to upload rather than risk overwriting a file we could not see",
            )
            return
        exists = "EXISTS" in str(getattr(probe, "stdout", ""))
        if dry_run:
            click.echo(
                json_formatter.format_json(
                    {
                        "notebook_id": notebook_id,
                        "local": os.fspath(source),
                        "local_bytes": source.stat().st_size,
                        "remote_path": remote_path,
                        "remote_exists": exists,
                        # One word instead of three booleans: `refuse` is the case a caller must notice, and
                        # a table of flags is how a script ends up writing where a human meant to look.
                        "action": (
                            "refuse"
                            if (exists and not overwrite)
                            else ("overwrite" if exists else "create")
                        ),
                        "transport": "Jupyter Contents (not the job exec PTY)",
                    }
                )
                if json_output
                else (
                    f"{'would write' if not (exists and not overwrite) else 'would REFUSE'} "
                    f"{source.stat().st_size} bytes -> {remote_path}  "
                    f"[{('exists; action=' + ('overwrite' if overwrite else 'refuse')) if exists else 'absent; action=create'}]"
                )
            )
            return
        if exists:
            _handle_error(
                ctx,
                "ValidationError",
                f"refusing to overwrite an existing remote path: {remote_path}",
                EXIT_GENERAL_ERROR,
                hint="pass --overwrite if you have confirmed the destination is yours to replace",
            )
            return

    if not json_output:
        emit_progress(ctx, f"Uploading to notebook '{notebook_id}' via Jupyter Contents...")

    try:
        result = browser_api_module.upload_path_to_notebook(
            notebook_id=notebook_id,
            session=session,
            local_path=source,
            remote_path=remote_path,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        _handle_error(ctx, "NotFound", str(exc), EXIT_API_ERROR)
        return
    except ValueError as exc:
        _handle_error(ctx, "ValidationError", str(exc), EXIT_GENERAL_ERROR)
        return
    except (
        Exception
    ) as exc:  # noqa: BLE001 - transport failures arrive as whatever the client raised
        _handle_error(
            ctx,
            "UploadError",
            f"Failed to upload to notebook: {exc}",
            EXIT_API_ERROR,
            hint=(
                "Ensure the notebook is RUNNING and JupyterLab is reachable. Nothing is installed under "
                "REMOTE_PATH unless its checksum matched inside the container."
            ),
        )
        return

    payload = {
        "notebook_id": notebook_id,
        "bridge": via_bridge,
        "local": os.fspath(source),
        "remote_path": remote_path,
        "bytes": getattr(result, "bytes", None),
        "local_bytes": getattr(result, "local_bytes", -1),
        "verified_by": "SHA-256 computed inside the notebook before the file is installed",
        "overwrote": overwrite,
    }
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return

    size = payload["bytes"]
    local_size = payload["local_bytes"]
    if isinstance(local_size, int) and local_size >= 0:
        # `bytes` is the gzip archive that crossed the wire, not the file the user pointed at;
        # leading with it reported a number nobody had uploaded.
        click.echo(f"Uploaded {local_size} bytes to {remote_path} ({size} archive)")
    else:
        click.echo(f"Uploaded {size if size is not None else '?'} bytes to {remote_path}")


__all__ = ["upload_notebook_cmd"]
