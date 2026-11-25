"""Bridge scp command -- transfer files to/from Bridge via SCP."""

from __future__ import annotations

import posixpath
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_TIMEOUT,
    pass_context,
)
from inspire.bridge.tunnel import (
    TunnelNotAvailableError,
    BridgeNotFoundError,
    is_tunnel_available,
    load_tunnel_config,
)
from inspire.bridge.tunnel.scp import run_scp_transfer
from inspire.cli.utils.common import json_option
from inspire.cli.utils.bridge_transport import BRIDGE_TRANSPORTS, select_bridge_transport
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.notebook_cli import require_web_session
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.output import emit_info, emit_success
from inspire.config import (
    Config,
    ConfigError,
    load_runtime_session_state_for_config,
    resolve_effective_remote_runtime,
)
from inspire.cli.commands.notebook.notebook_download import download_from_notebook
from inspire.platform.web import browser_api as browser_api_module


def _safe_extract_archive(archive_path: Path, destination: Path) -> list[Path]:
    destination_root = destination.resolve()
    with tarfile.open(archive_path, mode="r:*") as archive:
        members = archive.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if target != destination_root and destination_root not in target.parents:
                raise RuntimeError(f"Unsafe path in notebook archive: {member.name}")
        if hasattr(tarfile, "data_filter"):
            archive.extractall(destination, filter="data")
        else:  # Python 3.10 compatibility
            if any(member.issym() or member.islnk() for member in members):
                raise RuntimeError("Notebook archive links require Python 3.11 or newer.")
            archive.extractall(destination)  # noqa: S202 - paths and links validated above
    return [destination / member.name for member in members if member.name]


def _install_jupyter_download(
    *,
    archive_path: Path,
    destination: str,
    remote_path: str,
    is_archive: bool,
) -> Path:
    requested = Path(destination)
    treat_as_dir = destination.endswith(("/", "\\")) or (requested.exists() and requested.is_dir())
    remote_name = Path(remote_path.rstrip("/")).name or "download"

    if not is_archive:
        final_path = requested / remote_name if treat_as_dir else requested
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(archive_path), str(final_path))
        return final_path

    with tempfile.TemporaryDirectory(prefix="inspire-download-extract-") as temp_dir:
        extract_root = Path(temp_dir)
        members = _safe_extract_archive(archive_path, extract_root)
        roots = list(dict.fromkeys(path.parts[len(extract_root.parts)] for path in members))
        if len(roots) != 1:
            raise RuntimeError(
                "Notebook directory archive must contain exactly one top-level entry."
            )
        extracted = extract_root / roots[0]
        final_path = requested / extracted.name if treat_as_dir else requested
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.exists() and final_path.is_dir() and extracted.is_dir():
            shutil.copytree(extracted, final_path, dirs_exist_ok=True, symlinks=True)
        else:
            if final_path.exists():
                if final_path.is_dir():
                    shutil.rmtree(final_path)
                else:
                    final_path.unlink()
            shutil.move(str(extracted), str(final_path))
        return final_path


def _scp_failure_details(result: object) -> str | None:
    for attr in ("stderr", "stdout"):
        value = getattr(result, attr, None)
        text = str(value or "").strip()
        if not text:
            continue
        line = text.splitlines()[-1].strip()
        if line:
            return line[:400]
    return None


@click.command("scp")
@click.argument("source")
@click.argument("destination")
@click.option("--download", "-d", is_flag=True, help="Download from remote (default is upload)")
@click.option("--recursive", "-r", is_flag=True, help="Copy directories recursively")
@click.option("--bridge", "-b", help="Bridge profile to use")
@click.option(
    "transport_name",
    "--transport",
    type=click.Choice(BRIDGE_TRANSPORTS, case_sensitive=False),
    default="auto",
    show_default=True,
    help="Transfer transport: prefer healthy SSH, otherwise use notebook Jupyter",
)
@click.option("--timeout", "-t", type=int, default=None, help="Timeout in seconds")
@json_option
@pass_context
def bridge_scp(
    ctx: Context,
    source: str,
    destination: str,
    download: bool,
    recursive: bool,
    bridge: Optional[str],
    transport_name: str,
    timeout: Optional[int],
    json_output: bool = False,
) -> None:
    """Transfer files to/from Bridge via SCP.

    By default, uploads SOURCE (local) to DESTINATION (remote).
    Use --download to download SOURCE (remote) to DESTINATION (local).

    \b
    Examples:
        inspire bridge scp ./model.py /tmp/model.py
        inspire bridge scp ./data/ /tmp/data/ -r
        inspire bridge scp -d /tmp/results.tar.gz ./results.tar.gz
        inspire bridge scp -d /tmp/checkpoints/ ./checkpoints/ -r
        inspire bridge scp ./bundle.tar /tmp/ --bridge gpu-main
    """

    json_output = resolve_json_output(ctx, json_output)
    # Validate local path exists for uploads
    if not download:
        local = Path(source)
        if not local.exists():
            msg = f"Local path not found: {source}"
            _handle_error(ctx, "FileNotFound", msg, EXIT_GENERAL_ERROR)

        # Auto-enable recursive for directories
        if local.is_dir() and not recursive:
            recursive = True

    tunnel_config = load_tunnel_config()
    try:
        transport_selection = select_bridge_transport(
            requested=transport_name,
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

    selected_transport = transport_selection.selected
    resolved_bridge_name = transport_selection.bridge.name
    if selected_transport == "ssh" and not is_tunnel_available(
        bridge_name=resolved_bridge_name, config=tunnel_config
    ):
        hint = (
            "Run 'inspire tunnel status' to troubleshoot. "
            "For a notebook-backed bridge, retry with '--transport jupyter'."
        )
        _handle_error(ctx, "TunnelError", "SSH tunnel not available", EXIT_GENERAL_ERROR, hint=hint)

    if download:
        local_path, remote_path = destination, source
    else:
        local_path, remote_path = source, destination

    original_remote_path = remote_path
    if (
        remote_path
        and remote_path != "~"
        and not remote_path.startswith("/")
        and not remote_path.startswith("~/")
    ):
        remote_path_had_trailing_slash = remote_path.endswith("/")
        try:
            config, _ = Config.from_files_and_env(
                require_target_dir=False,
                require_credentials=False,
            )
            runtime = resolve_effective_remote_runtime(
                config,
                require_cwd=False,
                session_state=load_runtime_session_state_for_config(config),
            )
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)

        cwd = str(runtime.cwd or "").strip()
        resolved_remote_path: str | None = None
        if cwd == "~":
            if selected_transport == "jupyter":
                resolved_remote_path = posixpath.join("~", remote_path)
        elif cwd.startswith("~/"):
            resolved_remote_path = posixpath.normpath(posixpath.join(cwd[2:], remote_path))
        elif cwd:
            resolved_remote_path = posixpath.normpath(posixpath.join(cwd, remote_path))
        if resolved_remote_path is not None:
            if remote_path_had_trailing_slash and not resolved_remote_path.endswith("/"):
                resolved_remote_path = f"{resolved_remote_path}/"
            remote_path = resolved_remote_path

    direction = "download" if download else "upload"

    if not ctx.json_output and ctx.debug:
        emit_info(ctx, f"SCP {direction}: {source} -> {destination}")
        if remote_path != original_remote_path:
            emit_info(ctx, f"Remote path resolved: {original_remote_path} -> {remote_path}")
        emit_info(ctx, f"Bridge: {resolved_bridge_name}")
        emit_info(ctx, f"Transport: {selected_transport}")
        if recursive:
            emit_info(ctx, "Mode: recursive")

    try:
        if selected_transport == "jupyter":
            notebook_id = str(transport_selection.bridge.notebook_id or "").strip()
            session = require_web_session(
                ctx,
                hint="Jupyter transfer requires web authentication for the selected account.",
            )
            action_timeout = timeout or 600
            if download:
                with tempfile.TemporaryDirectory(prefix="inspire-bridge-download-") as temp_dir:
                    temporary_path = Path(temp_dir) / "payload"
                    download_result = download_from_notebook(
                        notebook_id=notebook_id,
                        session=session,
                        remote_path=remote_path,
                        local_path=str(temporary_path),
                        timeout=action_timeout,
                    )
                    final_path = _install_jupyter_download(
                        archive_path=Path(download_result.local_path),
                        destination=local_path,
                        remote_path=remote_path,
                        is_archive=download_result.archive,
                    )
                payload = download_result.as_payload()
                payload.update(
                    {
                        "status": "success",
                        "direction": direction,
                        "source": source,
                        "destination": destination,
                        "local_path": str(final_path),
                        "bridge": resolved_bridge_name,
                        "method": "jupyter",
                    }
                )
            else:
                upload_result = browser_api_module.upload_path_to_notebook(
                    notebook_id=notebook_id,
                    session=session,
                    local_path=local_path,
                    remote_path=remote_path,
                    timeout=action_timeout,
                )
                payload = upload_result.as_payload()
                payload.update(
                    {
                        "status": "success",
                        "direction": direction,
                        "source": source,
                        "destination": destination,
                        "recursive": recursive,
                        "bridge": resolved_bridge_name,
                        "method": "jupyter",
                    }
                )
            emit_success(ctx, payload=payload, text=f"SCP {direction} completed via Jupyter")
            return

        result = run_scp_transfer(
            local_path=local_path,
            remote_path=remote_path,
            download=download,
            recursive=recursive,
            bridge_name=resolved_bridge_name,
            config=tunnel_config,
            timeout=timeout,
        )

        if result.returncode != 0:
            detail = _scp_failure_details(result)
            message = f"SCP {direction} failed with exit code {result.returncode}"
            if detail:
                message = f"{message}: {detail}"
            _handle_error(
                ctx,
                "SCPFailed",
                message,
                EXIT_GENERAL_ERROR,
            )

        emit_success(
            ctx,
            payload={
                "status": "success",
                "direction": direction,
                "source": source,
                "destination": destination,
                "recursive": recursive,
                "bridge": resolved_bridge_name,
                "method": "ssh",
            },
            text=f"SCP {direction} completed",
        )

    except BridgeNotFoundError as e:
        _handle_error(ctx, "BridgeNotFound", str(e), EXIT_GENERAL_ERROR)
    except TunnelNotAvailableError as e:
        _handle_error(ctx, "TunnelError", str(e), EXIT_GENERAL_ERROR)
    except subprocess.TimeoutExpired:
        msg = f"SCP {direction} timed out after {timeout}s"
        _handle_error(ctx, "Timeout", msg, EXIT_TIMEOUT)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as e:
        _handle_error(ctx, "TransferError", str(e), EXIT_GENERAL_ERROR)
