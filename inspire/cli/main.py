"""Inspire CLI - Main entry point.

Usage:
    inspire job create --name "pr-123" --resource "4xH200" --command "bash train.sh"
    inspire job status <job-id>
    inspire job logs <job-id> --tail 100
    inspire resources list
"""

import logging
import shlex
import sys
from typing import Sequence

import click

from inspire import __version__
from inspire.config import SOURCE_CLI, reset_account_override, set_account_override
from inspire.config.permissions import harden_private_dirs
from inspire.cli.utils.profile import apply_env_profile
from inspire.cli.logging_setup import (
    _configure_json_logging,
    clear_debug_logging,
    configure_debug_logging,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_GENERAL_ERROR,
)
from inspire.cli.risk import apply_risk_labels
from inspire.cli.commands.api import api
from inspire.cli.commands.bridge import bridge
from inspire.cli.commands.config import config
from inspire.cli.commands.dataset import dataset
from inspire.cli.commands.hpc import hpc
from inspire.cli.commands.image import image
from inspire.cli.commands.init import init
from inspire.cli.commands.job import job
from inspire.cli.commands.model import model
from inspire.cli.commands.model_hub import model_hub
from inspire.cli.commands.notebook import notebook
from inspire.cli.commands.project import project
from inspire.cli.commands.ray import ray
from inspire.cli.commands.resources import resources
from inspire.cli.commands.run import run
from inspire.cli.commands.runtime import runtime
from inspire.cli.commands.schema_cmd import schema
from inspire.cli.commands.skills_cmd import skills
from inspire.cli.commands.storage import storage
from inspire.cli.commands.sync import sync
from inspire.cli.commands.tunnel import tunnel
from inspire.cli.commands.user import user
from inspire.cli.commands.workspace import workspace
from inspire.cli.commands.resource_shortcuts import (
    cache_cmd,
    exec_shortcut,
    logs_shortcut,
    resolve_cmd,
    status_shortcut,
    stop_shortcut,
)


def _apply_profile_option(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> str | None:
    if value:
        apply_env_profile(value)
    return value


_AGENT_QUICKSTART = """
\b
AGENT QUICKSTART
    --json is global and goes before the group: inspire --json job list
    inspire schema              every command, its risk level, its parameters
    inspire schema job list     one command's full contract, human-readable
    inspire --json schema job list
                                 the same contract as JSON
    inspire skills list         agent skill docs packaged with this CLI version
    --dry-run                   write commands print the platform request and send nothing
    inspire api GET <path>      one-shot raw platform JSON API request
    inspire api eval @script.js authenticated page: your JS/fetch/DOM, no wrapper
    inspire --json job list | jq -r '.data.items[].job_id'
"""


@click.group(epilog=_AGENT_QUICKSTART)
@click.option(
    "--profile",
    help="Apply env profile (INSPIRE_PROFILE_<NAME>_*)",
    expose_value=False,
    is_eager=True,
    callback=_apply_profile_option,
)
@click.version_option(version=__version__, prog_name="inspire")
@click.option(
    "--account",
    "account_alias",
    metavar="ALIAS",
    help="Use a configured account alias for this invocation",
)
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
def main(ctx: Context, account_alias: str | None, json_output: bool, debug: bool) -> None:
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
    ctx.account_alias = str(account_alias or "").strip() or None
    ctx.debug = debug

    if debug:
        ctx.debug_report_path = configure_debug_logging(argv=sys.argv)
    else:
        clear_debug_logging()

    # Repair permissions on our own credential files before anything reads them. Releases
    # before v0.2 wrote bridge state from the process umask, so a desktop umask(002) left
    # notebook tokens 0664, and files we do not write ourselves (timestamped config backups,
    # which can hold a plaintext password) stay loose until something repairs them.
    # Best-effort by contract: never fatal, silent unless --debug.
    try:
        harden_private_dirs()
    except Exception as exc:  # pragma: no cover - defensive; a chmod problem must not block work
        logging.getLogger(__name__).debug("Could not repair state permissions: %s", exc)

    click_ctx = click.get_current_context(silent=True)
    if click_ctx is not None:
        click_ctx.call_on_close(clear_debug_logging)
        if ctx.account_alias:
            account_token = set_account_override(ctx.account_alias, source=SOURCE_CLI)
            click_ctx.call_on_close(lambda: reset_account_override(account_token))

    ctx.json_output = json_output
    if ctx.json_output:
        _configure_json_logging()


def _global_json_requested(argv: Sequence[str]) -> bool:
    """Return whether the top-level ``--json`` flag is enabled."""
    root_ctx = main.make_context(
        "inspire",
        list(argv),
        resilient_parsing=True,
        allow_extra_args=True,
        ignore_unknown_options=True,
    )
    return bool(root_ctx.params.get("json_output"))


#: The options only the root group accepts. A copy placed after the group is a Click usage error
#: ("No such option"), and the fix is always the same mechanical move, so the error says it.
_GLOBAL_ONLY_OPTIONS = ("--json", "--debug", "--account", "--profile")
#: Globals that consume a value; their value moves with the flag when it is relocated.
_GLOBAL_VALUE_OPTIONS = ("--account", "--profile")


def _misplaced_global_option(exc: click.ClickException) -> str | None:
    """The global-only flag a ``NoSuchOption`` named, when it is one of ours, else None."""
    if not isinstance(exc, click.exceptions.NoSuchOption):
        return None
    option = str(getattr(exc, "option_name", "") or "")
    return option if option in _GLOBAL_ONLY_OPTIONS else None


def _corrected_global_invocation(argv: Sequence[str], option: str) -> str | None:
    """The same command line with ``option`` relocated to its global position, or None.

    Click stops parsing at the misplaced flag, so the rest of the line is intact and the fix is a
    move: take the flag (and its value, for ``--account``/``--profile``) out and put it in front of
    the group. Any other global flags already in front simply stay there; globals commute.
    """
    args = list(argv)
    index = next(
        (
            position
            for position in range(len(args) - 1, -1, -1)
            if args[position] == option or args[position].startswith(option + "=")
        ),
        None,
    )
    if index is None:
        return None
    token = args.pop(index)
    value = token[len(option) + 1 :] if token.startswith(option + "=") else None
    if (
        value is None
        and option in _GLOBAL_VALUE_OPTIONS
        and index < len(args)
        and not str(args[index]).startswith("-")
    ):
        value = args.pop(index)
    already_present = any(arg == option or arg.startswith(option + "=") for arg in args[:index])
    relocated = [] if already_present else [option] + ([value] if value is not None else [])
    return shlex.join(["inspire", *relocated, *args])


def _misplaced_global_hint(argv: Sequence[str], exc: click.ClickException) -> str | None:
    """An actionable hint for a misplaced global flag, or None for every other usage error."""
    option = _misplaced_global_option(exc)
    if option is None:
        return None
    corrected = _corrected_global_invocation(argv, option)
    if corrected is None:
        return f"{option} is a global option and goes before the command group"
    return f"{option} is a global option and goes before the command group: {corrected}"


# Register command groups
main.add_command(api)
main.add_command(job)
main.add_command(resources)
main.add_command(config)
main.add_command(dataset)
main.add_command(sync)
main.add_command(bridge)
main.add_command(tunnel)
main.add_command(run)
main.add_command(notebook)
main.add_command(init)
main.add_command(image)
main.add_command(project)
main.add_command(model)
main.add_command(model_hub)
main.add_command(runtime)
main.add_command(schema)
main.add_command(skills)
main.add_command(storage)
main.add_command(hpc)
main.add_command(ray)
main.add_command(user)
main.add_command(workspace)
main.add_command(resolve_cmd)
main.add_command(cache_cmd)
main.add_command(status_shortcut)
main.add_command(stop_shortcut)
main.add_command(logs_shortcut)
main.add_command(exec_shortcut)

# Every registered command now resolves to a risk level in its rendered help, straight from
# inspire/cli/risk.py. This runs once at import, after the tree exists, so no command's own help text
# mentions risk and a new command is labelled the moment it is registered.
apply_risk_labels(main)


def cli(argv: Sequence[str] | None = None) -> None:
    """Entry point for the CLI."""
    run_argv = list(argv) if argv is not None else sys.argv[1:]
    json_requested = _global_json_requested(run_argv)

    try:
        main.main(args=run_argv, prog_name="inspire", standalone_mode=False, obj=Context())
    except click.ClickException as e:
        # A global flag typed after the group is the one usage error whose fix is mechanical, so
        # the error carries the corrected line; every other usage error stays exactly as Click
        # wrote it.
        hint = _misplaced_global_hint(run_argv, e)
        if json_requested:
            click.echo(
                json_formatter.format_json_error(
                    type(e).__name__, e.format_message(), e.exit_code, hint=hint
                ),
                err=True,
            )
        else:
            e.show(file=sys.stderr)
            if hint:
                click.echo(f"Hint: {hint}", err=True)
        sys.exit(e.exit_code)
    except click.Abort:
        if json_requested:
            click.echo(
                json_formatter.format_json_error("Abort", "Aborted!", EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo("Aborted!", err=True)
        sys.exit(EXIT_GENERAL_ERROR)
    except Exception as e:  # pragma: no cover - top-level safety net
        logging.getLogger(__name__).exception("Unhandled exception in inspire CLI")
        if json_requested:
            click.echo(
                json_formatter.format_json_error("UnhandledError", str(e), EXIT_GENERAL_ERROR),
                err=True,
            )
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_GENERAL_ERROR)
    finally:
        clear_debug_logging()


if __name__ == "__main__":  # pragma: no cover
    cli()
