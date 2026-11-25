"""Notebook/Interactive instance commands.

Usage:
    inspire notebook list
    inspire notebook status <instance-id>
    inspire notebook resource <instance-id>
    inspire notebook create --resource 1xH200
    inspire notebook stop <instance-id>
"""

from __future__ import annotations

import click

from inspire.cli.commands.workload_profile import build_profile_group
from inspire.cli.commands.workload_catalog import (
    build_availability_command,
    build_offers_command,
)
from inspire.platform.web import browser_api

from .notebook_commands import (
    create_notebook_cmd,
    delete_notebook_cmd,
    exec_notebook_cmd,
    list_notebooks,
    notebook_status,
    notebook_url_cmd,
    ssh_notebook_cmd,
    start_notebook_cmd,
    stop_notebook_cmd,
    update_notebook_cmd,
    wait_notebook_cmd,
)
from .notebook_download import download_notebook_cmd
from .notebook_upload import upload_notebook_cmd
from .notebook_doctor import notebook_doctor_cmd
from .notebook_resource import notebook_resource_cmd


@click.group()
def notebook():
    """Manage notebook/interactive instances.

    \b
    Examples:
        inspire notebook list              # List all instances
        inspire --json notebook list       # List as JSON
    """
    pass


notebook.add_command(list_notebooks)
notebook.add_command(notebook_status)
notebook.add_command(wait_notebook_cmd)
notebook.add_command(notebook_resource_cmd)
notebook.add_command(create_notebook_cmd)
notebook.add_command(stop_notebook_cmd)
notebook.add_command(delete_notebook_cmd)
notebook.add_command(start_notebook_cmd)
notebook.add_command(update_notebook_cmd)
notebook.add_command(notebook_url_cmd)
notebook.add_command(ssh_notebook_cmd)
notebook.add_command(exec_notebook_cmd)
notebook.add_command(download_notebook_cmd)
notebook.add_command(upload_notebook_cmd)
notebook.add_command(notebook_doctor_cmd)
notebook.add_command(build_profile_group("notebook"))
notebook.add_command(
    build_offers_command("notebook", lambda **kwargs: browser_api.list_notebook_offers(**kwargs))
)
notebook.add_command(
    build_availability_command(
        "notebook", lambda **kwargs: browser_api.list_notebook_offers(**kwargs)
    )
)
