"""Resource commands for Inspire CLI."""

from __future__ import annotations

import click

from inspire.cli.commands.resources_list import list_resources
from inspire.cli.commands.resources_nodes import list_nodes


@click.group()
def resources() -> None:
    """View available compute resources."""
    pass


resources.add_command(list_resources)
resources.add_command(list_nodes)
