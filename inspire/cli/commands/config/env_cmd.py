"""Config env command – generate .env template file."""

from __future__ import annotations

from pathlib import Path

import click

from inspire.cli.context import (
    Context,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_success
from inspire.config import (
    get_categories,
    get_options_by_category,
)

# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@click.command("env")
@click.option(
    "--template",
    "-t",
    type=click.Choice(["full", "minimal"]),
    default="minimal",
    help="Template type: full (all options) or minimal (essential only)",
)
@click.option(
    "--output",
    "-o",
    "output_file",
    type=click.Path(),
    help="Write to file instead of stdout (refuses an existing file without --force)",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Overwrite the --output file if it already exists",
)
@json_option
@pass_context
def generate_env(
    ctx: Context,
    template: str,
    output_file: str | None,
    force: bool,
    json_output: bool = False,
) -> None:
    """Print a starter template of the CLI's environment variables, grouped by category.

    Generated from the CLI's option registry, so it lists what exists rather than what is currently set.
    Values are left as placeholders to customise. `--template minimal` keeps the essential categories
    (authentication, API, paths, Gitea); `--template full` includes every category. `--output FILE` writes
    to a file instead of stdout, and refuses to replace an existing FILE unless `--force` is given;
    `--force` is required in JSON mode too (nothing is prompted).
    """
    json_output = resolve_json_output(ctx, json_output)

    lines: list[str] = []
    lines.append("# Inspire CLI Environment Variables")
    lines.append("# Generated template - customize values as needed")
    lines.append("")

    essential_categories = {"Authentication", "API", "Paths", "Gitea"}

    categories = get_categories()
    for category in categories:
        if template == "minimal" and category not in essential_categories:
            continue

        options = get_options_by_category(category)
        if not options:
            continue

        lines.append(f"# === {category} ===")

        for option in options:
            lines.append(f"# {option.description}")

            if option.secret:
                lines.append(f"# {option.env_var}=<your-secret-here>")
            elif option.default is not None:
                default_str = str(option.default)
                if isinstance(option.default, list):
                    default_str = ",".join(option.default) if option.default else ""
                if " " in default_str or "," in default_str:
                    lines.append(f'# {option.env_var}="{default_str}"')
                else:
                    lines.append(f"# {option.env_var}={default_str}")
            else:
                lines.append(f"# {option.env_var}=")

        lines.append("")

    content = "\n".join(lines)

    if output_file:
        output_path = Path(output_file)
        if output_path.exists() and not force:
            _handle_error(
                ctx,
                "FileExistsError",
                f"Refusing to overwrite existing file: {output_path}",
                EXIT_GENERAL_ERROR,
                hint=f"Re-run with --force to replace {output_path}.",
            )
            return
        output_path.write_text(content)
        if json_output:
            emit_success(
                ctx,
                payload={"template": template, "output_file": str(output_path)},
            )
        else:
            click.echo(click.style(f"Created {output_path}", fg="green"))
    elif json_output:
        emit_success(ctx, payload={"template": template, "content": content})
    else:
        click.echo(content)
