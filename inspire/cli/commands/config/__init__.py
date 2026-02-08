"""Configuration commands for Inspire CLI.

Commands:
    inspire config show    - Display merged configuration with sources
    inspire config check   - Validate environment and authentication
    inspire config env     - Generate .env template
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import click

from inspire.cli.context import (
    Context,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import (
    Config,
    ConfigError,
    ConfigOption,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_GLOBAL,
    SOURCE_PROJECT,
    get_categories,
    get_options_by_category,
)


@click.group()
def config() -> None:
    """Inspect and validate Inspire CLI configuration."""


# ---------------------------------------------------------------------------
# `inspire config check`
# ---------------------------------------------------------------------------


_PLACEHOLDER_HOSTS = {
    "api.example.com",
    "example.com",
    "example.org",
    "example.net",
}
_PLACEHOLDER_HOST_SUFFIXES = (
    ".example.com",
    ".example.org",
    ".example.net",
)
_HOST_VALIDATION_FIELDS = (
    ("base_url", "INSPIRE_BASE_URL"),
    ("gitea_server", "INSP_GITEA_SERVER"),
    ("github_server", "INSP_GITHUB_SERVER"),
    ("docker_registry", "INSPIRE_DOCKER_REGISTRY"),
    ("rtunnel_download_url", "INSPIRE_RTUNNEL_DOWNLOAD_URL"),
    ("apt_mirror_url", "INSPIRE_APT_MIRROR_URL"),
    ("pip_index_url", "INSPIRE_PIP_INDEX_URL"),
)


def _describe_precedence(prefer_source: str) -> str:
    if prefer_source == "toml":
        return "project TOML wins on conflict"
    return "env vars win on conflict (default)"


def _extract_hostname(value: str | None) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text or text.startswith("/"):
        return None

    if "://" in text:
        parsed = urlsplit(text)
        return parsed.hostname.lower() if parsed.hostname else None

    if text.startswith("//"):
        parsed = urlsplit(f"https:{text}")
        return parsed.hostname.lower() if parsed.hostname else None

    candidate = text.split("/", 1)[0].strip()
    if not candidate or " " in candidate:
        return None
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[-1]
    if ":" in candidate:
        candidate = candidate.split(":", 1)[0]
    if "." not in candidate:
        return None
    return candidate.lower()


def _is_placeholder_host(host: str) -> bool:
    if host in _PLACEHOLDER_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in _PLACEHOLDER_HOST_SUFFIXES)


def _find_placeholder_host_issues(cfg: Config, sources: dict[str, str]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for field_name, env_var in _HOST_VALIDATION_FIELDS:
        raw_value = getattr(cfg, field_name, None)
        if raw_value in (None, ""):
            continue

        value = str(raw_value)
        host = _extract_hostname(value)
        if not host:
            continue
        if not _is_placeholder_host(host):
            continue

        issues.append(
            {
                "field": field_name,
                "env_var": env_var,
                "value": value,
                "host": host,
                "source": sources.get(field_name, SOURCE_DEFAULT),
            }
        )
    return issues


def _format_placeholder_issue_message(issues: list[dict[str, str]]) -> str:
    lines = ["Placeholder host values detected in configuration:"]
    for issue in issues:
        lines.append(
            f"  - {issue['env_var']} ({issue['field']}): "
            f"{issue['value']} [source: {issue['source']}]"
        )
    lines.append("Use real host values in config files or environment variables.")
    lines.append("Path-only defaults such as /auth/token are allowed.")
    return "\n".join(lines)


def _validate_required_credentials(cfg: Config) -> None:
    if not cfg.username:
        raise ConfigError(
            "Missing username configuration.\n"
            "Set INSPIRE_USERNAME env var or add to config.toml:\n"
            "  [auth]\n"
            "  username = 'your_username'"
        )
    if not cfg.password:
        raise ConfigError(
            "Missing password configuration.\n"
            "Set INSPIRE_PASSWORD env var or add a global account password:\n"
            "  [accounts.\"your_username\"]\n"
            "  password = 'your_password'"
        )


def _validate_required_registry(cfg: Config) -> None:
    if not cfg.docker_registry:
        raise ConfigError(
            "Missing docker registry configuration.\n"
            "Set INSPIRE_DOCKER_REGISTRY env var or add to config.toml:\n"
            "  [api]\n"
            "  docker_registry = 'your-registry.example.com'"
        )


def _validate_project_base_url_shape(project_path: Path | None) -> None:
    if not project_path or not project_path.exists():
        return

    try:
        project_raw = Config._load_toml(project_path)
    except Exception as e:
        raise ConfigError(f"Failed to read project config at {project_path}: {e}") from e

    if "base_url" in project_raw:
        raise ConfigError(
            f"Invalid project config at {project_path}.\n"
            "Found top-level `base_url`; this key must be under [api].\n"
            "Use:\n"
            "  [api]\n"
            "  base_url = 'https://your-inspire-host'"
        )


def _build_base_url_resolution(
    cfg: Config,
    sources: dict[str, str],
    global_path: Path | None,
    project_path: Path | None,
) -> dict[str, object]:
    env_base_url = os.environ.get("INSPIRE_BASE_URL")
    return {
        "value": cfg.base_url,
        "source": sources.get("base_url", SOURCE_DEFAULT),
        "prefer_source": getattr(cfg, "prefer_source", "env"),
        "precedence": _describe_precedence(getattr(cfg, "prefer_source", "env")),
        "env_present": bool(env_base_url),
        "global_config_path": str(global_path) if global_path else None,
        "project_config_path": str(project_path) if project_path else None,
    }


@click.command("check")
@pass_context
def check_config(ctx: Context) -> None:
    """Check environment configuration and API authentication.

    Verifies configuration (from files and environment) and attempts to
    authenticate with the Inspire API.
    """
    try:
        cfg, sources = Config.from_files_and_env(
            require_credentials=False,
            require_target_dir=False,
        )
        global_path, project_path = Config.get_config_paths()
        _validate_project_base_url_shape(project_path)

        placeholder_issues = _find_placeholder_host_issues(cfg, sources)
        if placeholder_issues:
            raise ConfigError(_format_placeholder_issue_message(placeholder_issues))

        _validate_required_credentials(cfg)
        _validate_required_registry(cfg)

        auth_ok = True
        auth_error = None

        try:
            AuthManager.get_api(cfg)
        except AuthenticationError as e:
            auth_ok = False
            auth_error = str(e)

        base_url_resolution = _build_base_url_resolution(cfg, sources, global_path, project_path)
        default_base_url_hint = None
        if base_url_resolution["source"] == SOURCE_DEFAULT:
            default_base_url_hint = (
                "Base URL is using default fallback. Set [api] base_url in "
                "./.inspire/config.toml or export INSPIRE_BASE_URL."
            )

        result = {
            "username": cfg.username,
            "base_url": cfg.base_url,
            "target_dir": cfg.target_dir,
            "job_cache_path": cfg.get_expanded_cache_path(),
            "log_pattern": cfg.log_pattern,
            "timeout": cfg.timeout,
            "max_retries": cfg.max_retries,
            "retry_delay": cfg.retry_delay,
            "auth_ok": auth_ok,
            "base_url_resolution": base_url_resolution,
            "validation": {
                "placeholder_host_issues": placeholder_issues,
                "base_url_default_hint": default_base_url_hint,
            },
        }
        if auth_error:
            result["auth_error"] = auth_error

        if ctx.json_output:
            click.echo(json_formatter.format_json(result, success=auth_ok))
        else:
            if auth_ok:
                click.echo(human_formatter.format_success("Configuration looks good"))
            else:
                click.echo(human_formatter.format_error("Authentication failed"))

            click.echo(f"\nUsername:     {cfg.username}")
            click.echo(f"Base URL:     {cfg.base_url}")
            click.echo(f"Target dir:   {cfg.target_dir or '(not set - required for logs)'}")
            click.echo(f"Log pattern:  {cfg.log_pattern}")
            click.echo(f"Job cache:    {cfg.get_expanded_cache_path()}")
            click.echo(f"Timeout:      {cfg.timeout}s")
            click.echo(f"Max retries:  {cfg.max_retries}")
            click.echo(f"Retry delay:  {cfg.retry_delay}s")
            click.echo("\nBase URL resolution:")
            click.echo(f"  Value:                {base_url_resolution['value']}")
            click.echo(f"  Source:               {base_url_resolution['source']}")
            click.echo(f"  Precedence:           {base_url_resolution['precedence']}")
            click.echo(
                "  INSPIRE_BASE_URL set: "
                f"{'yes' if base_url_resolution['env_present'] else 'no'}"
            )
            click.echo(
                "  Global config:        "
                f"{base_url_resolution['global_config_path'] or '(not found)'}"
            )
            click.echo(
                "  Project config:       "
                f"{base_url_resolution['project_config_path'] or '(not found)'}"
            )

            if default_base_url_hint:
                click.echo(click.style(f"  Note: {default_base_url_hint}", fg="yellow"))

            if auth_error:
                click.echo(f"\nDetails: {auth_error}")

        if not auth_ok:
            sys.exit(EXIT_AUTH_ERROR)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


# ---------------------------------------------------------------------------
# `inspire config env`
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
    help="Write to file instead of stdout",
)
@pass_context
def generate_env(ctx: Context, template: str, output_file: str | None) -> None:
    """Generate .env template file.

    Creates a template with all configuration options as environment variables.

    \b
    Examples:
        inspire config env
        inspire config env --template full
        inspire config env --output .env.example
    """
    _ = ctx  # unused (but consistent signature with other commands)

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
        output_path.write_text(content)
        click.echo(click.style(f"Created {output_path}", fg="green"))
    else:
        click.echo(content)


# ---------------------------------------------------------------------------
# `inspire config show`
# ---------------------------------------------------------------------------


SOURCE_LABELS: dict[str, tuple[str, str]] = {
    SOURCE_DEFAULT: ("default", "white"),
    SOURCE_GLOBAL: ("global", "cyan"),
    SOURCE_PROJECT: ("project", "green"),
    SOURCE_ENV: ("env", "yellow"),
}


def _get_field_value(cfg: Config, option: ConfigOption) -> tuple[str | None, bool]:
    field_name = option.field_name
    if not field_name or not hasattr(cfg, field_name):
        return None, False

    value = getattr(cfg, field_name)

    is_set = value is not None and value != "" and value != []

    if option.secret and value:
        return "********", is_set
    if value is None:
        return "(not set)", False
    if isinstance(value, list):
        return ", ".join(value) if value else "(empty)", is_set
    return str(value), is_set


def _get_source_for_option(sources: dict[str, str], option: ConfigOption) -> str:
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
    click.echo(click.style("Configuration Overview", bold=True))
    click.echo()

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

    prefer_source = getattr(cfg, "prefer_source", "env")
    if prefer_source == "toml":
        click.echo(
            "  Precedence: "
            + click.style("project TOML wins", fg="green")
            + " on conflict"
        )
    else:
        click.echo(
            "  Precedence: "
            + click.style("env vars win", fg="yellow")
            + " on conflict (default)"
        )

    click.echo()

    categories = get_categories()
    if filter_category:
        filter_value = filter_category.lower()
        categories = [c for c in categories if filter_value in c.lower()]
        if not categories:
            click.echo(click.style(f"No category matching '{filter_category}'", fg="red"))
            return

    display_data: list[tuple[str, list[tuple[ConfigOption, str, str, str]]]] = []
    max_value_len = 40

    for category in categories:
        options = get_options_by_category(category)
        if not options:
            continue

        if compact:
            options = [opt for opt in options if _get_field_value(cfg, opt)[1]]
            if not options:
                continue

        category_items: list[tuple[ConfigOption, str, str, str]] = []
        for option in options:
            value_str, _is_set = _get_field_value(cfg, option)
            source = _get_source_for_option(sources, option)
            source_label, source_color = SOURCE_LABELS.get(source, ("?", "white"))
            value_display = value_str or "(not set)"
            max_value_len = max(max_value_len, len(value_display))
            category_items.append((option, value_display, source_label, source_color))

        display_data.append((category, category_items))

    for category, items in display_data:
        click.echo(click.style(category, bold=True, fg="blue"))

        for option, value_display, source_label, source_color in items:
            key_display = option.env_var.ljust(30)
            value_padded = value_display.ljust(max_value_len)
            source_display = click.style(f"[{source_label}]", fg=source_color)

            click.echo(f"  {key_display} {value_padded} {source_display}")

        click.echo()

    click.echo(click.style("Legend:", dim=True))
    legend_parts = []
    for _source, (label, color) in SOURCE_LABELS.items():
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
    result = {
        "config_files": {
            "global": str(global_path) if global_path else None,
            "project": str(project_path) if project_path else None,
        },
        "prefer_source": getattr(cfg, "prefer_source", "env"),
        "values": {},
    }

    categories = get_categories()
    if filter_category:
        filter_value = filter_category.lower()
        categories = [c for c in categories if filter_value in c.lower()]

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


def _show_env(cfg: Config, compact: bool, filter_category: str | None) -> None:
    categories = get_categories()
    if filter_category:
        filter_value = filter_category.lower()
        categories = [c for c in categories if filter_value in c.lower()]

    for category in categories:
        options = get_options_by_category(category)
        if not options:
            continue

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
                if " " in value_str or "," in value_str:
                    click.echo(f'{option.env_var}="{value_str}"')
                else:
                    click.echo(f"{option.env_var}={value_str}")
            else:
                click.echo(f"# {option.env_var}=")
        click.echo()


@click.command("show")
@click.option(
    "--format",
    "-f",
    "output_format",
    type=click.Choice(["table", "json", "env"]),
    default="table",
    help="Output format (table, json, env)",
)
@click.option(
    "--compact",
    "-c",
    is_flag=True,
    help="Hide unset options",
)
@click.option(
    "--filter",
    "-F",
    "filter_category",
    help="Filter by category (e.g., 'API', 'Gitea')",
)
@pass_context
def show_config(
    ctx: Context, output_format: str, compact: bool, filter_category: str | None
) -> None:
    """Display merged configuration with value sources.

    Shows configuration values from all sources (defaults, global config,
    project config, environment variables) with clear indication of where
    each value comes from.

    By default, all options are shown including unset ones. Use --compact
    to hide unset options.

    \b
    Examples:
        inspire config show
        inspire config show --format json
        inspire config show --filter API
        inspire config show --compact
    """
    try:
        cfg, sources = Config.from_files_and_env(
            require_credentials=False, require_target_dir=False
        )
        global_path, project_path = Config.get_config_paths()

        if output_format == "json":
            _show_json(cfg, sources, global_path, project_path, compact, filter_category)
        elif output_format == "env":
            _show_env(cfg, compact, filter_category)
        else:
            _show_table(cfg, sources, global_path, project_path, compact, filter_category)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


config.add_command(show_config)
config.add_command(generate_env)
config.add_command(check_config)


__all__ = ["config"]
