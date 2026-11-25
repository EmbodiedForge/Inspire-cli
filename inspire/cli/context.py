"""Shared CLI context and exit codes for Inspire CLI.

This module avoids circular imports between the main CLI entry point
and individual command modules by centralizing common definitions.
"""

import click

# Exit codes
EXIT_SUCCESS = 0
EXIT_GENERAL_ERROR = 1
#: An option value this CLI cannot act on. It is Click's own usage-error code -
#: main.py lets a click.UsageError through with exactly this number - so a refusal
#: that reads as "you passed something unusable" exits with the same code a bad
#: flag would, rather than with the runtime codes below.
EXIT_USAGE_ERROR = 2
EXIT_CONFIG_ERROR = 10
EXIT_AUTH_ERROR = 11
EXIT_VALIDATION_ERROR = 12
EXIT_API_ERROR = 13
EXIT_TIMEOUT = 14
EXIT_LOG_NOT_FOUND = 15
EXIT_JOB_NOT_FOUND = 16


class Context:
    """CLI context passed to all commands.

    Stores global CLI options such as JSON output and debug mode.
    """

    def __init__(self) -> None:
        self.json_output: bool = False
        self.debug: bool = False
        self.debug_report_path: str | None = None
        self.account_alias: str | None = None


# Click decorator to pass the shared Context instance into commands
pass_context = click.make_pass_decorator(Context, ensure=True)
