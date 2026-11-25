"""Bridge sync command -- copy a directory tree onto a bridge without deleting the destination."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from inspire.bridge.tunnel import (
    BridgeNotFoundError,
    is_tunnel_available,
    load_tunnel_config,
)
from inspire.bridge.tunnel import tree_sync
from inspire.cli.context import Context, EXIT_GENERAL_ERROR, EXIT_VALIDATION_ERROR, pass_context
from inspire.cli.utils.bridge_transport import select_bridge_transport
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.jupyter_bridge import JupyterBridgeClient
from inspire.cli.utils.notebook_cli import require_web_session, resolve_json_output
from inspire.cli.utils.output import emit_info, emit_success, emit_warning

#: Remote exits that deserve a specific remedy rather than the generic retry hint.
_REMOTE_HINTS = {
    tree_sync.EXIT_SHA_MISMATCH: (
        "The upload was truncated or corrupted in transit. Retry, and check that the bridge "
        "has free space in /tmp."
    ),
    tree_sync.EXIT_MISSING_TREE_ROOT: (
        "The staged tree vanished before it could be moved. Check that the bridge's /tmp is "
        "not mounted noexec or cleaned mid-run."
    ),
    tree_sync.EXIT_REQUIRE_FAILED: (
        "A --require path was missing from what arrived. Check the local tree, or drop that "
        "--require."
    ),
    tree_sync.EXIT_BACKUP_EXISTS: (
        "A backup from the same second already exists at the destination. Wait a second, or "
        "clear the stale <dest>.inspire-bak-* sibling."
    ),
}


@click.command("sync")
@click.argument("local")
@click.option("--to", "remote_path", required=True, help="Destination directory on the bridge")
@click.option(
    "--engine",
    type=click.Choice(["auto", "rsync", "tar"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Transfer engine: auto uses rsync when the bridge has it, otherwise tar",
)
@click.option(
    "--swap",
    is_flag=True,
    help="With the rsync engine, stage through a sibling and move it into place atomically",
)
@click.option(
    "--require",
    multiple=True,
    help="Tree-relative path that must exist before the destination is touched (repeatable)",
)
@click.option("--bridge", "-b", help="Bridge profile to use")
@click.option(
    "--timeout", "-t", type=int, default=120, show_default=True, help="Timeout in seconds"
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview the payload and engine plan without writing anything to the bridge "
    "(the bridge may still be probed for availability)",
)
@json_option
@pass_context
def bridge_sync(
    ctx: Context,
    local: str,
    remote_path: str,
    engine: str,
    swap: bool,
    require: tuple[str, ...],
    bridge: Optional[str],
    timeout: int,
    dry_run: bool,
    json_output: bool = False,
) -> None:
    """Sync a local directory tree to a bridge, never deleting the destination.

    `inspire sync` moves commits and resets the remote worktree, so uncommitted work cannot travel
    with it. This command moves a tree instead: a fresh destination is created, an existing one is
    moved aside to <dest>.inspire-bak-<UTC>, and nothing at the destination is ever removed.
    Pass --require to gate the swap on paths that must have arrived.

    On a bridge reachable only through Jupyter, the tar engine stages the archive through the
    Jupyter Contents API instead of scp; --engine rsync stays SSH-only and is refused there.

    \b
    Examples:
        inspire bridge sync ./experiment --to /root/work/experiment
        inspire bridge sync ./pkg --to /root/work/pkg --require pyproject.toml
        inspire bridge sync ./ckpt --to /root/ckpt --swap --engine rsync
        inspire bridge sync ./data --to /data/incoming --dry-run
    """

    json_output = resolve_json_output(ctx, json_output)

    local_dir = Path(local)
    if not local_dir.exists() or not local_dir.is_dir():
        _handle_error(
            ctx,
            "Validation",
            f"Local directory not found: {local}",
            EXIT_VALIDATION_ERROR,
            hint="LOCAL must be a directory; use 'inspire bridge scp' for single files.",
        )

    try:
        tree_sync._validate_relative_paths(require)
    except ValueError as error:
        _handle_error(
            ctx,
            "Validation",
            str(error),
            EXIT_VALIDATION_ERROR,
            hint="--require paths are relative to the synced tree, never absolute.",
        )

    tunnel_config = load_tunnel_config()
    try:
        transport_selection = select_bridge_transport(
            requested="auto",
            bridge_name=bridge,
            tunnel_config=tunnel_config,
            ssh_probe=is_tunnel_available,
        )
    except (BridgeNotFoundError, ValueError) as error:
        if isinstance(error, BridgeNotFoundError) and bridge is None:
            _handle_error(
                ctx,
                "TunnelError",
                "SSH tunnel not available",
                EXIT_GENERAL_ERROR,
                hint="Run 'inspire tunnel status' to troubleshoot the tunnel.",
            )
        _handle_error(
            ctx,
            "BridgeNotFound" if isinstance(error, BridgeNotFoundError) else "ConfigError",
            str(error),
            EXIT_GENERAL_ERROR,
            hint="Run 'inspire tunnel list' to see available bridge profiles.",
        )

    bridge_name = transport_selection.bridge.name
    via_jupyter = transport_selection.selected == "jupyter"
    requested_engine = engine.lower().strip()
    engine_note: Optional[str] = None
    run_command_fn: Optional[tree_sync.RemoteCommandRunner] = None
    transfer_fn: Optional[tree_sync.FileTransferRunner] = None

    if via_jupyter:
        if requested_engine == "rsync":
            _handle_error(
                ctx,
                "TunnelError",
                f"Bridge '{bridge_name}' has no usable SSH tunnel; rsync needs the SSH transport.",
                EXIT_VALIDATION_ERROR,
                hint=(
                    "Use --engine tar, which stages the archive through the Jupyter Contents API, "
                    "or a bridge with SSH ingress (see 'inspire tunnel ssh --help')."
                ),
            )
        # The tar engine needs one staging path and one remote shell, and the Jupyter APIs provide
        # both; rsync is the only engine that genuinely requires SSH.
        engine_note = "jupyter transport: archive staged via the Jupyter Contents API"
        if not dry_run:
            client = JupyterBridgeClient(
                notebook_id=str(transport_selection.bridge.notebook_id or ""),
                session=require_web_session(
                    ctx,
                    hint=(
                        "Tree sync over Jupyter requires web authentication for the selected "
                        "account."
                    ),
                ),
            )
            run_command_fn = client.run_command
            transfer_fn = client.upload_raw

    if dry_run:
        chosen_engine = "rsync" if requested_engine == "rsync" else "tar"
        if requested_engine == "auto":
            note = "engine auto is decided on the bridge; dry-run reports the tar engine"
            engine_note = f"{engine_note}; {note}" if engine_note else note
    elif requested_engine == "tar":
        chosen_engine = "tar"
    elif requested_engine == "rsync":
        if not tree_sync.ensure_remote_rsync(
            bridge_name=bridge_name, config=tunnel_config, timeout=max(timeout, 300)
        ):
            _handle_error(
                ctx,
                "DependencyMissing",
                f"rsync is not available on bridge '{bridge_name}' and could not be installed.",
                EXIT_GENERAL_ERROR,
                hint="Retry with '--engine tar', which only needs tar and sha256sum.",
            )
        chosen_engine = "rsync"
    elif via_jupyter:
        chosen_engine = "tar"
    elif tree_sync.probe_remote_rsync(bridge_name=bridge_name, config=tunnel_config):
        chosen_engine = "rsync"
    else:
        chosen_engine = "tar"
        note = (
            "rsync is not installed on the bridge, so the tar engine was used; "
            "'--engine rsync' installs it"
        )
        engine_note = f"{engine_note}; {note}" if engine_note else note

    if ctx.debug and not ctx.json_output:
        emit_info(ctx, f"Tree sync: {local} -> {bridge_name}:{remote_path}")
        emit_info(ctx, f"Bridge: {bridge_name}")
        emit_info(ctx, f"Engine: {chosen_engine}")
        if swap:
            emit_info(ctx, "Mode: swap (staged displacement)")
    if swap and chosen_engine == "tar":
        emit_warning(
            ctx,
            "--swap only changes how the rsync engine stages; the tar engine always "
            "displaces an existing destination to <dest>.inspire-bak-<UTC>.",
        )

    try:
        if chosen_engine == "rsync":
            result = tree_sync.sync_tree_via_rsync(
                local_dir=str(local_dir),
                remote_dir=remote_path,
                bridge_name=bridge_name,
                config=tunnel_config,
                timeout=timeout,
                swap=swap,
                require=require,
                dry_run=dry_run,
            )
        else:
            result = tree_sync.sync_tree_via_tar(
                local_dir=str(local_dir),
                remote_dir=remote_path,
                bridge_name=bridge_name,
                config=tunnel_config,
                timeout=timeout,
                require=require,
                dry_run=dry_run,
                run_command_fn=run_command_fn,
                transfer_fn=transfer_fn,
            )
    except ValueError as error:
        _handle_error(ctx, "Validation", str(error), EXIT_VALIDATION_ERROR)

    payload = result.as_payload()
    payload["bridge"] = bridge_name
    notes = [note for note in (result.engine_note, engine_note) if note]
    payload["engine_note"] = "; ".join(notes) if notes else None

    if not result.success:
        message = result.error or "tree sync failed"
        _handle_error(
            ctx,
            "SyncFailed",
            message,
            EXIT_GENERAL_ERROR,
            hint=_REMOTE_HINTS.get(result.exit_code or 0),
            data=payload,
        )

    text = f"tree sync {'planned' if dry_run else 'completed'} via {result.engine}: "
    text += f"{local} -> {bridge_name}:{remote_path}"
    if result.displaced_backup:
        text += f" (previous destination kept at {result.displaced_backup})"
    emit_success(ctx, payload=payload, text=text)


__all__ = ["bridge_sync"]
