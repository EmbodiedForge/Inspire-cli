"""Inspire CLI - Main entry point.

Usage:
    inspire job create --name "pr-123" --resource "4xH200" --command "bash train.sh"
    inspire job status <job-id>
    inspire job logs <job-id> --tail 100
    inspire resources list
"""

import logging
import sys
import click

from inspire import __version__
from inspire.cli.utils.profile import apply_env_profile
from inspire.cli.logging_setup import clear_debug_logging, configure_debug_logging
from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_GENERAL_ERROR,
)
from inspire.cli.commands import (
    job,
    resources,
    config,
    sync,
    bridge,
    tunnel,
    run,
    notebook,
    init,
    image,
    project,
)


def _apply_profile_option(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> str | None:
    if value:
        apply_env_profile(value)
    return value


@click.group()
@click.option(
    "--profile",
    help="Apply env profile (INSPIRE_PROFILE_<NAME>_*)",
    expose_value=False,
    is_eager=True,
    callback=_apply_profile_option,
)
@click.version_option(version=__version__, prog_name="inspire")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Output as JSON (machine-readable)",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug logging",
)
@pass_context
def main(ctx: Context, json_output: bool, debug: bool) -> None:
    """Inspire Training Platform CLI.

    Interact with the Inspire HPC platform to submit training jobs,
    monitor their status, and retrieve logs.

    \b
    Examples:
        inspire job create --name "pr-123" --resource "4xH200" --command "bash train.sh"
        inspire job status job-abc-123
        inspire job logs job-abc-123 --tail 100
        inspire resources list
    """
    ctx.json_output = json_output
    ctx.debug = debug

    if debug:
        ctx.debug_report_path = configure_debug_logging(argv=sys.argv)
    else:
        clear_debug_logging()


# Register command groups
main.add_command(job)
main.add_command(resources)
main.add_command(config)
main.add_command(sync)
main.add_command(bridge)
main.add_command(tunnel)
main.add_command(run)
main.add_command(notebook)
main.add_command(init)
main.add_command(image)
main.add_command(project)


def cli() -> None:
    """Entry point for the CLI."""
    try:
        main()
    except Exception as e:  # pragma: no cover - top-level safety net
        logging.getLogger(__name__).exception("Unhandled exception in inspire CLI")
        click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_GENERAL_ERROR)


if __name__ == "__main__":  # pragma: no cover
    cli()
