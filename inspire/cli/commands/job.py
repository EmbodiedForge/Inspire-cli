"""Job commands for Inspire CLI.

Commands:
    inspire job create - Create a new training job
    inspire job status - Check job status
    inspire job command - Show job start command
    inspire job stop   - Stop a running job
    inspire job wait   - Wait for job completion
    inspire job list   - List recent jobs from local cache
    inspire job update - Update cached job statuses
    inspire job logs   - View job logs
"""

from __future__ import annotations

import sys as _sys
import time  # re-exported for tests

import click

from inspire.cli.commands.job_command import build_command_command
from inspire.cli.commands.job_create import build_create_command
from inspire.cli.commands.job_list import build_list_command
from inspire.cli.commands.job_logs import build_logs_command
from inspire.cli.commands.job_status import build_status_command
from inspire.cli.commands.job_stop import build_stop_command
from inspire.cli.commands.job_update import build_update_command
from inspire.cli.commands.job_wait import build_wait_command
from inspire.cli.utils.gitea import fetch_remote_log_via_bridge  # re-exported for tests
from inspire.cli.utils.job_cache import JobCache  # re-exported for tests

__all__ = [
    "job",
    "time",
    "JobCache",
    "fetch_remote_log_via_bridge",
]


@click.group()
def job():
    """Manage training jobs on the Inspire platform."""
    pass


_deps = _sys.modules[__name__]
job.add_command(build_create_command(_deps))
job.add_command(build_status_command(_deps))
job.add_command(build_command_command(_deps))
job.add_command(build_stop_command(_deps))
job.add_command(build_wait_command(_deps))
job.add_command(build_list_command(_deps))
job.add_command(build_update_command(_deps))
job.add_command(build_logs_command(_deps))
