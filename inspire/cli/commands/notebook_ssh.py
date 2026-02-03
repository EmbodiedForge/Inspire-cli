"""Notebook SSH command."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import click

from inspire.cli.commands.notebook_common import _require_web_session
from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils.errors import exit_with_error as _handle_error


def _load_ssh_public_key(pubkey_path: Optional[str] = None) -> str:
    """Load an SSH public key to authorize notebook SSH access."""
    candidates: list[Path]

    if pubkey_path:
        candidates = [Path(pubkey_path).expanduser()]
    else:
        candidates = [
            Path.home() / ".ssh" / "id_ed25519.pub",
            Path.home() / ".ssh" / "id_rsa.pub",
        ]

    for path in candidates:
        if path.exists():
            key = path.read_text(encoding="utf-8", errors="ignore").strip()
            if key:
                return key

    raise ValueError(
        "No SSH public key found. Provide --pubkey PATH or generate one with 'ssh-keygen'."
    )


@click.command("ssh")
@click.argument("notebook_id")
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Wait for notebook to reach RUNNING status",
)
@click.option(
    "--pubkey",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help="SSH public key path to authorize (defaults to ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub)",
)
@click.option(
    "--save-as",
    help="Save this notebook tunnel as a named profile (usable with 'ssh <name>' after 'inspire tunnel ssh-config --install')",
)
@click.option(
    "--port",
    default=31337,
    show_default=True,
    help="rtunnel server listen port inside notebook",
)
@click.option(
    "--ssh-port",
    default=22222,
    show_default=True,
    help="sshd port inside notebook",
)
@click.option(
    "--command",
    help="Optional remote command to run (if omitted, opens an interactive shell)",
)
@click.option(
    "--rtunnel-bin",
    help="Path to pre-cached rtunnel binary (e.g., /inspire/.../rtunnel)",
)
@click.option(
    "--debug-playwright",
    is_flag=True,
    help="Run browser automation with visible window for debugging",
)
@click.option(
    "--timeout",
    "setup_timeout",
    default=300,
    show_default=True,
    help="Timeout in seconds for rtunnel setup to complete",
)
@pass_context
def ssh_notebook_cmd(
    ctx: Context,
    notebook_id: str,
    wait: bool,
    pubkey: Optional[str],
    save_as: Optional[str],
    port: int,
    ssh_port: int,
    command: Optional[str],
    rtunnel_bin: Optional[str],
    debug_playwright: bool,
    setup_timeout: int,
) -> None:
    """SSH into a running notebook instance via rtunnel ProxyCommand."""
    from inspire.cli.utils.tunnel import (
        BridgeProfile,
        get_ssh_command_args,
        has_internet_for_gpu_type,
        load_tunnel_config,
        save_tunnel_config,
    )

    session = _require_web_session(
        ctx,
        hint=(
            "Notebook SSH requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )

    # Wait for running (optional) and get notebook detail for GPU info
    try:
        if wait:
            notebook_detail = browser_api_module.wait_for_notebook_running(
                notebook_id=notebook_id, session=session
            )
        else:
            notebook_detail = browser_api_module.get_notebook_detail(
                notebook_id=notebook_id, session=session
            )
    except TimeoutError as e:
        _handle_error(
            ctx,
            "Timeout",
            f"Timed out waiting for notebook to reach RUNNING: {e}",
            EXIT_API_ERROR,
        )
        return
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    # Extract GPU type from notebook detail for internet capability detection
    gpu_info = (notebook_detail.get("resource_spec_price") or {}).get("gpu_info") or {}
    gpu_type = gpu_info.get("gpu_product_simple", "")
    has_internet = has_internet_for_gpu_type(gpu_type)

    # Fast-path: Check if we have a cached profile for this notebook and test connectivity
    profile_name = save_as or f"notebook-{notebook_id[:8]}"
    cached_config = load_tunnel_config()
    if profile_name in cached_config.bridges:
        # Test if the cached tunnel still works by trying a quick SSH connection
        import subprocess

        test_args = get_ssh_command_args(
            bridge_name=profile_name,
            config=cached_config,
            remote_command="echo ok",
        )
        try:
            result = subprocess.run(
                test_args,
                capture_output=True,
                timeout=10,
                text=True,
            )
            if result.returncode == 0 and "ok" in result.stdout:
                click.echo("Using cached tunnel connection (fast path).", err=True)
                # Reuse the cached config
                args = get_ssh_command_args(
                    bridge_name=profile_name,
                    config=cached_config,
                    remote_command=command,
                )
                os.execvp("ssh", args)
                return  # execvp doesn't return, but for clarity
        except (subprocess.TimeoutExpired, Exception):
            pass  # Fall through to full setup

    # Load SSH public key
    try:
        ssh_public_key = _load_ssh_public_key(pubkey)
    except ValueError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    # Set up rtunnel + sshd in notebook and derive proxy URL from Jupyter
    # Pass rtunnel_bin to setup function via environment variable if specified
    if rtunnel_bin:
        os.environ["INSPIRE_RTUNNEL_BIN"] = rtunnel_bin
    try:
        proxy_url = browser_api_module.setup_notebook_rtunnel(
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_public_key=ssh_public_key,
            session=session,
            headless=not debug_playwright,
            timeout=setup_timeout,
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to set up notebook tunnel: {e}", EXIT_API_ERROR)
        return

    # Build a bridge profile for this notebook
    # profile_name already set above in fast-path check
    bridge = BridgeProfile(
        name=profile_name,
        proxy_url=proxy_url,
        ssh_user="root",
        ssh_port=ssh_port,
        has_internet=has_internet,
    )

    # Always save the profile for future fast-path use
    config = load_tunnel_config()
    config.add_bridge(bridge)
    save_tunnel_config(config)

    # Show profile info with internet status
    internet_status = "yes" if has_internet else "no"
    gpu_label = gpu_type if gpu_type else "CPU"
    click.echo(
        f"Added bridge '{profile_name}' (internet: {internet_status}, GPU: {gpu_label})", err=True
    )

    args = get_ssh_command_args(
        bridge_name=profile_name,
        config=config,
        remote_command=command,
    )

    # Replace current process with ssh for interactive behavior
    os.execvp("ssh", args)
