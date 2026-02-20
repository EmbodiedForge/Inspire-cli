"""Bridge commands for executing raw commands on the Bridge runner."""

from __future__ import annotations

import click

from .exec_cmd import exec_command
from .scp_cmd import bridge_scp
from .ssh_cmd import bridge_ssh


@click.group()
def bridge() -> None:
    """Run commands on the Bridge runner (executes in INSPIRE_TARGET_DIR)."""


bridge.add_command(exec_command)
bridge.add_command(bridge_scp)
bridge.add_command(bridge_ssh)

__all__ = ["bridge"]
