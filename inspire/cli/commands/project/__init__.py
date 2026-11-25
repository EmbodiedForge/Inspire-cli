"""Project management commands.

Usage:
    inspire project list
    inspire project select
"""

from __future__ import annotations

import click

from .project_commands import list_projects_cmd, use_project_cmd, init_project_cmd
from .select import select_projects


@click.group()
def project():
    """View project information and GPU quota.

    \b
    Examples:
        inspire project list          # Show project quota table
        inspire --json project list   # JSON output with all fields
        inspire project select        # Interactive project priority selector
        inspire project use <name>    # Quickly set active project
        inspire project init [name]   # Initialize project config
    """
    pass


project.add_command(list_projects_cmd)
project.add_command(select_projects)
project.add_command(use_project_cmd)
project.add_command(init_project_cmd)
