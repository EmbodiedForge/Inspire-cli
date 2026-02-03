"""Notebook SSH command."""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.commands.notebook_ssh_flow import run_notebook_ssh
from inspire.cli.context import Context, pass_context


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
    run_notebook_ssh(
        ctx,
        notebook_id=notebook_id,
        wait=wait,
        pubkey=pubkey,
        save_as=save_as,
        port=port,
        ssh_port=ssh_port,
        command=command,
        rtunnel_bin=rtunnel_bin,
        debug_playwright=debug_playwright,
        setup_timeout=setup_timeout,
    )
