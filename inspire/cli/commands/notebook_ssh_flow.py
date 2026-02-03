"""Flow implementation for `inspire notebook ssh`."""

from __future__ import annotations

import os
from typing import Optional

import click

from inspire.cli.commands.notebook_ssh_keys import load_ssh_public_key
from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR
from inspire.cli.utils import browser_api as browser_api_module
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import require_web_session


def run_notebook_ssh(
    ctx: Context,
    *,
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
    from inspire.cli.utils.tunnel import (
        BridgeProfile,
        get_ssh_command_args,
        has_internet_for_gpu_type,
        load_tunnel_config,
        save_tunnel_config,
    )

    session = require_web_session(
        ctx,
        hint=(
            "Notebook SSH requires web authentication. "
            "Set INSPIRE_USERNAME and INSPIRE_PASSWORD."
        ),
    )

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

    gpu_info = (notebook_detail.get("resource_spec_price") or {}).get("gpu_info") or {}
    gpu_type = gpu_info.get("gpu_product_simple", "")
    has_internet = has_internet_for_gpu_type(gpu_type)

    profile_name = save_as or f"notebook-{notebook_id[:8]}"
    cached_config = load_tunnel_config()

    if profile_name in cached_config.bridges:
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
                args = get_ssh_command_args(
                    bridge_name=profile_name,
                    config=cached_config,
                    remote_command=command,
                )
                os.execvp("ssh", args)
                return
        except (subprocess.TimeoutExpired, Exception):
            pass

    try:
        ssh_public_key = load_ssh_public_key(pubkey)
    except ValueError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

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

    bridge = BridgeProfile(
        name=profile_name,
        proxy_url=proxy_url,
        ssh_user="root",
        ssh_port=ssh_port,
        has_internet=has_internet,
    )

    config = load_tunnel_config()
    config.add_bridge(bridge)
    save_tunnel_config(config)

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

    os.execvp("ssh", args)


__all__ = ["run_notebook_ssh"]
