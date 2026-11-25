"""Tunnel update command."""

from __future__ import annotations


import click

from ._ssh_config_sync import sync_installed_ssh_config
from inspire.bridge.tunnel import load_tunnel_config, save_tunnel_config
from inspire.bridge.tunnel.models import _proxy_port_from_url
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_info, emit_success, emit_warning
from inspire.platform.web.browser_api.rtunnel import redact_proxy_url


@click.command("update")
@click.argument("name")
@click.option("--url", help="Update the proxy URL")
@click.option("--ssh-user", help="Update the SSH user")
@click.option("--ssh-port", type=int, help="Update the SSH port")
@click.option(
    "--gpu-type",
    default=None,
    help=(
        "GPU type this bridge's notebook runs on (e.g. 4090, H100, H200). "
        "Internet access is derived from it: H100/H200 are offline, everything else is online."
    ),
)
@json_option
@pass_context
def tunnel_update(
    ctx: Context,
    name: str,
    url: str,
    ssh_user: str,
    ssh_port: int,
    gpu_type: str,
    json_output: bool = False,
) -> None:
    """Update an existing bridge profile.

    \b
    Examples:
        inspire tunnel update mybridge --gpu-type 4090
        inspire tunnel update mybridge --gpu-type H100
        inspire tunnel update mybridge --url "https://new-url.../proxy/31337/"
        inspire tunnel update mybridge --ssh-port 22223
    """

    json_output = resolve_json_output(ctx, json_output)
    config = load_tunnel_config()

    if name not in config.bridges:
        _handle_error(
            ctx,
            "NotFound",
            f"Bridge '{name}' not found",
            EXIT_CONFIG_ERROR,
        )

    bridge = config.bridges[name]
    updated_fields: list[str] = []

    if url is not None:
        bridge.proxy_url = url
        # The explicit port is what the rest of the stack trusts, so a URL that moves
        # the proxy to a new port must move rtunnel_port with it.
        proxy_port = _proxy_port_from_url(url)
        if proxy_port is not None:
            bridge.rtunnel_port = proxy_port
        updated_fields.append("url")
    if ssh_user is not None:
        bridge.ssh_user = ssh_user
        updated_fields.append("ssh_user")
    if ssh_port is not None:
        bridge.ssh_port = ssh_port
        updated_fields.append("ssh_port")
    if gpu_type is not None:
        bridge.gpu_type = gpu_type
        updated_fields.append("gpu_type")

    if not updated_fields:
        _handle_error(
            ctx,
            "ValidationError",
            "No fields to update. Use --url, --ssh-user, --ssh-port, or --gpu-type.",
            EXIT_CONFIG_ERROR,
        )

    save_tunnel_config(config)
    ssh_synced, ssh_sync_error = sync_installed_ssh_config(config)

    bridge_payload = bridge.to_dict()
    if bridge_payload.get("proxy_url"):
        bridge_payload["proxy_url"] = redact_proxy_url(bridge_payload["proxy_url"])

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "status": "updated",
                    "name": name,
                    "updated_fields": updated_fields,
                    "bridge": bridge_payload,
                    "ssh_config_synced": ssh_synced,
                }
            )
        )
        return

    emit_success(
        ctx,
        payload={
            "status": "updated",
            "name": name,
            "updated_fields": updated_fields,
            "bridge": bridge_payload,
            "ssh_config_synced": ssh_synced,
        },
        text=f"Updated bridge: {name}",
    )
    for field in updated_fields:
        if field == "url":
            emit_info(ctx, f"  URL: {redact_proxy_url(bridge.proxy_url)}")
        elif field == "ssh_user":
            emit_info(ctx, f"  SSH user: {bridge.ssh_user}")
        elif field == "ssh_port":
            emit_info(ctx, f"  SSH port: {bridge.ssh_port}")
        elif field == "gpu_type":
            emit_info(ctx, f"  GPU type: {bridge.gpu_type}")
            emit_info(
                ctx, f"  Internet: {'has-internet' if bridge.has_internet else 'no-internet'}"
            )
    if ssh_synced:
        emit_info(ctx, "  SSH config: synced")
    elif ssh_sync_error:
        emit_warning(ctx, f"  SSH config sync skipped: {ssh_sync_error}")
