"""Project management commands.

Usage:
    inspire project list
"""

from __future__ import annotations

import click

from .project_commands import list_projects_cmd


@click.group()
def project():
    """View project information and GPU quota.

    \b
    Examples:
        inspire project list          # Show project quota table
        inspire project list --json   # JSON output with all fields
    """
    pass


project.add_command(list_projects_cmd)
