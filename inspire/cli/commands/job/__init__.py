"""Job commands for Inspire CLI."""

from __future__ import annotations

import click

from inspire.cli.commands.workload_profile import build_profile_group
from inspire.cli.commands.workload_catalog import (
    build_availability_command,
    build_offers_command,
)
from inspire.platform.web import browser_api

from .job_commands import (
    events,
    instances,
    list_jobs,
    metrics,
    show_command,
    status,
    stop,
    wait,
)
from .job_create import create
from .job_exec import exec_job
from .job_logs import logs
from .job_quota import quota
from .job_quota_usage import quota_usage
from .job_upload import upload


@click.group()
def job() -> None:
    """Manage training jobs."""


job.add_command(create)
job.add_command(status)
job.add_command(logs)
job.add_command(list_jobs)
job.add_command(instances)
job.add_command(events)
job.add_command(metrics)
job.add_command(exec_job)
job.add_command(upload)
job.add_command(stop)
job.add_command(wait)
job.add_command(show_command)
job.add_command(quota)
job.add_command(quota_usage)
job.add_command(build_profile_group("job"))
job.add_command(build_offers_command("job", lambda **kwargs: browser_api.list_job_offers(**kwargs)))
job.add_command(
    build_availability_command("job", lambda **kwargs: browser_api.list_job_offers(**kwargs))
)


__all__ = ["job"]
