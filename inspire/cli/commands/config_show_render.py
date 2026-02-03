"""Rendering helpers for `inspire config show`."""

from __future__ import annotations

import json
from pathlib import Path

import click

from inspire.cli.utils.config import (
    Config,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_GLOBAL,
    SOURCE_PROJECT,
)
from inspire.cli.utils.config_schema import ConfigOption, get_categories, get_options_by_category

# Source display labels with color
SOURCE_LABELS = {
    SOURCE_DEFAULT: ("default", "white"),
    SOURCE_GLOBAL: ("global", "cyan"),
    SOURCE_PROJECT: ("project", "green"),
    SOURCE_ENV: ("env", "yellow"),
}


def _get_field_value(cfg: Config, option: ConfigOption) -> tuple[str | None, bool]:
    """Get config value for a given option.

    Returns:
        Tuple of (value_string, is_set) where is_set indicates if value differs from default
    """
    field_name = option.field_name
    if not field_name or not hasattr(cfg, field_name):
        return None, False

    value = getattr(cfg, field_name)

    # Check if value is set (not None and not empty for strings)
    is_set = value is not None and value != "" and value != []

    # Format value for display
    if option.secret and value:
        return "********", is_set
    if value is None:
        return "(not set)", False
    if isinstance(value, list):
        return ", ".join(value) if value else "(empty)", is_set
    return str(value), is_set


def _get_source_for_option(sources: dict[str, str], option: ConfigOption) -> str:
    """Get source label for a config option."""
    field_name = option.field_name
    return sources.get(field_name, SOURCE_DEFAULT) if field_name else SOURCE_DEFAULT


def _show_table(
    cfg: Config,
    sources: dict[str, str],
    global_path: Path | None,
    project_path: Path | None,
    compact: bool,
    filter_category: str | None,
) -> None:
    """Display configuration in table format."""
    click.echo(click.style("Configuration Overview", bold=True))
    click.echo()

    # Show config file locations
    click.echo("Config files:")
    if global_path:
        click.echo(f"  Global:  {global_path} " + click.style("(found)", fg="green"))
    else:
        click.echo(
            "  Global:  ~/.config/inspire/config.toml " + click.style("(not found)", fg="white")
        )
    if project_path:
        click.echo(f"  Project: {project_path} " + click.style("(found)", fg="green"))
    else:
        click.echo("  Project: ./inspire/config.toml " + click.style("(not found)", fg="white"))
    click.echo()

    # Display options by category
    categories = get_categories()
    if filter_category:
        # Find matching category (case-insensitive)
        filter_category = filter_category.lower()
        categories = [c for c in categories if filter_category in c.lower()]
        if not categories:
            click.echo(click.style(f"No category matching '{filter_category}'", fg="red"))
            return

    # First pass: collect all options to display and find max value length
    display_data: list[tuple[str, list[tuple[ConfigOption, str, str, str]]]] = []
    max_value_len = 40  # minimum width

    for category in categories:
        options = get_options_by_category(category)
        if not options:
            continue

        # Filter to hide unset options when --compact is used
        if compact:
            options = [opt for opt in options if _get_field_value(cfg, opt)[1]]
            if not options:
                continue

        category_items = []
        for option in options:
            value_str, _is_set = _get_field_value(cfg, option)
            source = _get_source_for_option(sources, option)
            source_label, source_color = SOURCE_LABELS.get(source, ("?", "white"))
            value_display = value_str or "(not set)"
            max_value_len = max(max_value_len, len(value_display))
            category_items.append((option, value_display, source_label, source_color))

        display_data.append((category, category_items))

    # Second pass: display with proper alignment
    for category, items in display_data:
        click.echo(click.style(category, bold=True, fg="blue"))

        for option, value_display, source_label, source_color in items:
            key_display = option.env_var.ljust(30)
            value_padded = value_display.ljust(max_value_len)
            source_display = click.style(f"[{source_label}]", fg=source_color)

            click.echo(f"  {key_display} {value_padded} {source_display}")

        click.echo()

    # Legend
    click.echo(click.style("Legend:", dim=True))
    legend_parts = []
    for source, (label, color) in SOURCE_LABELS.items():
        legend_parts.append(click.style(f"[{label}]", fg=color))
    click.echo("  " + " ".join(legend_parts))


def _show_json(
    cfg: Config,
    sources: dict[str, str],
    global_path: Path | None,
    project_path: Path | None,
    compact: bool,
    filter_category: str | None,
) -> None:
    """Display configuration as JSON."""
    result = {
        "config_files": {
            "global": str(global_path) if global_path else None,
            "project": str(project_path) if project_path else None,
        },
        "values": {},
    }

    categories = get_categories()
    if filter_category:
        filter_category = filter_category.lower()
        categories = [c for c in categories if filter_category in c.lower()]

    for category in categories:
        options = get_options_by_category(category)
        if not options:
            continue

        for option in options:
            value_str, is_set = _get_field_value(cfg, option)
            if compact and not is_set:
                continue

            source = _get_source_for_option(sources, option)
            result["values"][option.env_var] = {
                "value": (
                    value_str
                    if not option.secret
                    else ("********" if value_str != "(not set)" else None)
                ),
                "source": source,
                "toml_key": option.toml_key,
                "description": option.description,
            }

    click.echo(json.dumps(result, indent=2))


def _show_env(
    cfg: Config,
    compact: bool,
    filter_category: str | None,
) -> None:
    """Display configuration as environment variables."""
    categories = get_categories()
    if filter_category:
        filter_category = filter_category.lower()
        categories = [c for c in categories if filter_category in c.lower()]

    for category in categories:
        options = get_options_by_category(category)
        if not options:
            continue

        # Filter to hide unset options when --compact is used
        if compact:
            options = [opt for opt in options if _get_field_value(cfg, opt)[1]]
            if not options:
                continue

        click.echo(f"# {category}")
        for option in options:
            value_str, _is_set = _get_field_value(cfg, option)
            if option.secret:
                click.echo(f"# {option.env_var}=<secret>")
            elif value_str and value_str != "(not set)":
                # Quote values with spaces
                if " " in value_str or "," in value_str:
                    click.echo(f'{option.env_var}="{value_str}"')
                else:
                    click.echo(f"{option.env_var}={value_str}")
            else:
                click.echo(f"# {option.env_var}=")
        click.echo()


__all__ = ["_show_env", "_show_json", "_show_table"]
