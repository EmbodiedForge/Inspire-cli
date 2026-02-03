"""Tunnel commands for SSH access to Bridge via ProxyCommand."""

from __future__ import annotations

import click

from inspire.cli.commands.tunnel_add import tunnel_add
from inspire.cli.commands.tunnel_list import tunnel_list
from inspire.cli.commands.tunnel_remove import tunnel_remove
from inspire.cli.commands.tunnel_set_default import tunnel_set_default
from inspire.cli.commands.tunnel_ssh_config import tunnel_ssh_config
from inspire.cli.commands.tunnel_status import tunnel_status
from inspire.cli.commands.tunnel_test import tunnel_test
from inspire.cli.commands.tunnel_update import tunnel_update


@click.group()
def tunnel() -> None:
    """Manage SSH tunnels for fast Bridge access.

    Supports multiple bridge profiles. Commands like 'bridge exec' and
    'job logs' automatically use SSH when a bridge is configured.

    \b
    Quick Start:
        1. Set up rtunnel server on Bridge
        2. inspire tunnel add mybridge "https://nat-notebook.../proxy/31337/"
        3. inspire tunnel status              # Verify connection
        4. inspire bridge exec "hostname"     # Now uses fast SSH!

    \b
    Multiple bridges:
        inspire tunnel add bridge1 "https://..."
        inspire tunnel add bridge2 "https://..."
        inspire tunnel list
        inspire bridge exec --bridge bridge2 "hostname"

    \b
    For direct SSH access (scp, rsync, git):
        inspire tunnel ssh-config --install
        ssh bridge1
    """


tunnel.add_command(tunnel_status)
tunnel.add_command(tunnel_add)
tunnel.add_command(tunnel_remove)
tunnel.add_command(tunnel_update)
tunnel.add_command(tunnel_set_default)
tunnel.add_command(tunnel_list)
tunnel.add_command(tunnel_ssh_config)
tunnel.add_command(tunnel_test)
