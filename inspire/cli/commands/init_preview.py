"""Init helpers: preview formatting."""

from __future__ import annotations

import click

from inspire.config import CATEGORY_ORDER, ConfigOption


def _format_preview_by_scope(
    detected: list[tuple[ConfigOption, str]],
) -> None:
    """Display migration preview grouped by destination (global/project).

    Secrets are always shown as excluded.
    """
    click.echo(click.style("Detected environment variables (grouped by destination):", bold=True))
    click.echo()

    # Split by scope
    global_opts = [(opt, val) for opt, val in detected if opt.scope == "global"]
    project_opts = [(opt, val) for opt, val in detected if opt.scope == "project"]

    # Display global options
    if global_opts:
        click.echo(
            click.style("Global config (~/.config/inspire/config.toml):", fg="cyan", bold=True)
        )

        # Group by category within global
        by_category: dict[str, list[tuple[ConfigOption, str]]] = {}
        for option, value in global_opts:
            if option.category not in by_category:
                by_category[option.category] = []
            by_category[option.category].append((option, value))

        for category in CATEGORY_ORDER:
            if category not in by_category:
                continue

            click.echo(click.style(f"  {category}", fg="blue"))
            for option, value in by_category[category]:
                env_display = option.env_var.ljust(32)
                if option.secret:
                    value_display = click.style("(excluded - use env var)", fg="white", dim=True)
                else:
                    value_display = value[:40] + "..." if len(value) > 40 else value
                click.echo(f"    {env_display} {value_display}")
            click.echo()

    # Display project options
    if project_opts:
        click.echo(click.style("Project config (./.inspire/config.toml):", fg="green", bold=True))

        # Group by category within project
        by_category = {}
        for option, value in project_opts:
            if option.category not in by_category:
                by_category[option.category] = []
            by_category[option.category].append((option, value))

        for category in CATEGORY_ORDER:
            if category not in by_category:
                continue

            click.echo(click.style(f"  {category}", fg="blue"))
            for option, value in by_category[category]:
                env_display = option.env_var.ljust(32)
                if option.secret:
                    value_display = click.style("(excluded - use env var)", fg="white", dim=True)
                else:
                    value_display = value[:40] + "..." if len(value) > 40 else value
                click.echo(f"    {env_display} {value_display}")
            click.echo()
