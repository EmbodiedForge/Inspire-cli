"""One confirmation gate for every `high-risk-write` command.

docs/CLI-CONTRACT.md states the rule twice, from two directions: a high-risk command "requires --yes
(alias --force) outside an interactive TTY", and "`--json` mode never prompts". Writing that branch by
hand per command is how `notebook delete` and `model delete` ended up with opposite rules about what
`--force` means (issue #66 family), so the rule lives here once and every destructive command calls it.

The refusal is an exit-12 `ValidationError` that names the flag, never a prompt nobody can answer: a
caller with no terminal (a script, an agent, CI) cannot reply to a question, and a command that blocked
on stdin would hang it instead of telling it what to type. A person at a terminal keeps the prompt.
"""

from __future__ import annotations

import sys
from typing import Optional

import click

from inspire.cli.context import EXIT_VALIDATION_ERROR, Context
from inspire.cli.utils.errors import exit_with_error


def _stdin_is_interactive() -> bool:
    """Whether a human could answer a prompt here."""
    stdin = sys.stdin
    if stdin is None:
        return False
    try:
        return bool(stdin.isatty())
    except (AttributeError, ValueError, OSError):
        # A replaced, closed, or half-initialised stdin is not a terminal, and treating it as one
        # would put a prompt in front of a caller that cannot answer it.
        return False


def confirmed_or_error(
    ctx: Optional[Context],
    *,
    assume_yes: bool,
    subject: str,
) -> bool:
    """Gate a destructive action behind `--yes`, the prompt, or a refusal that exits.

    Args:
        ctx: The command's Context, or None for a command that has none. Only ``ctx.json_output`` is
            read; with None the terminal check is the only non-interactive signal available.
        assume_yes: The command's `--yes`/`--force` flag. Set, and this returns True without asking.
        subject: What the call does, as a verb phrase without a leading capital - ``stop job
            job-1234``, ``delete the job profile 'fast' from global config``. It is quoted back in
            both the prompt and the refusal, so a caller reading an error knows what was at stake.

    Returns:
        True when the caller may go ahead. False only when a person at a terminal answered the prompt
        with no, so the caller should stop quietly.

    Exits:
        12 with a `ValidationError` naming `--yes` when confirmation is impossible - `--json` mode, or
        no terminal on stdin. The exit is the point: a refusal that returned normally would let the
        caller report success for a command that never ran.
    """
    if assume_yes:
        return True

    if (ctx is not None and ctx.json_output) or not _stdin_is_interactive():
        exit_with_error(
            ctx if ctx is not None else Context(),
            "ValidationError",
            (
                f"Refusing to {subject} without confirmation. "
                "Pass --yes to run it without an interactive prompt."
            ),
            EXIT_VALIDATION_ERROR,
            hint="In a script or agent run, --yes is the confirmation; leave it out to stop here.",
        )

    question = f"{subject[:1].upper()}{subject[1:]}?"
    return bool(click.confirm(question, default=False))


__all__ = ["confirmed_or_error"]
