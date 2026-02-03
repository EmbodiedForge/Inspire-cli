"""Notebook/Interactive instance commands.

Usage:
    inspire notebook list
    inspire notebook status <instance-id>
    inspire notebook create --resource 1xH200
    inspire notebook stop <instance-id>
"""

from __future__ import annotations

import click

from inspire.cli.commands.notebook_create import create_notebook_cmd
from inspire.cli.commands.notebook_lifecycle import start_notebook_cmd, stop_notebook_cmd
from inspire.cli.commands.notebook_list import list_notebooks
from inspire.cli.commands.notebook_ssh import ssh_notebook_cmd
from inspire.cli.commands.notebook_status import notebook_status


@click.group()
def notebook():
    """Manage notebook/interactive instances.

    \b
    Examples:
        inspire notebook list              # List all instances
        inspire notebook list --json       # List as JSON
    """
    pass


notebook.add_command(list_notebooks)
notebook.add_command(notebook_status)
notebook.add_command(create_notebook_cmd)
notebook.add_command(stop_notebook_cmd)
notebook.add_command(start_notebook_cmd)
notebook.add_command(ssh_notebook_cmd)
