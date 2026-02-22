"""Notebook/Interactive instance commands.

Usage:
    inspire notebook list
    inspire notebook status <instance-id>
    inspire notebook top
    inspire notebook create --resource 1xH200
    inspire notebook stop <instance-id>
"""

from __future__ import annotations

import click

from .notebook_commands import (
    create_notebook_cmd,
    list_notebooks,
    notebook_status,
    ssh_notebook_cmd,
    start_notebook_cmd,
    stop_notebook_cmd,
)
from .top import notebook_top


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
notebook.add_command(notebook_top)
