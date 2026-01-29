"""Configuration commands for Inspire CLI.

Commands:
    inspire config show    - Display merged configuration with sources
    inspire config check   - Validate environment and authentication
    inspire config env     - Generate .env template
"""

import os
import json
import sys
from pathlib import Path

import click

from inspire.cli.context import (
    Context,
    pass_context,
    EXIT_CONFIG_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_GENERAL_ERROR,
)
from inspire.cli.utils.config import (
    Config,
    ConfigError,
    SOURCE_DEFAULT,
    SOURCE_GLOBAL,
    SOURCE_PROJECT,
    SOURCE_ENV,
    PROJECT_CONFIG_DIR,
    CONFIG_FILENAME,
)
from inspire.cli.utils.config_schema import (
    CONFIG_OPTIONS,
    ConfigOption,
    get_categories,
    get_options_by_category,
    CATEGORY_ORDER,
)
from inspire.cli.utils.auth import AuthManager, AuthenticationError
from inspire.cli.formatters import json_formatter, human_formatter


# Source display labels with color
SOURCE_LABELS = {
    SOURCE_DEFAULT: ("default", "white"),
    SOURCE_GLOBAL: ("global", "cyan"),
    SOURCE_PROJECT: ("project", "green"),
    SOURCE_ENV: ("env", "yellow"),
}


@click.group()
def config() -> None:
    """Inspect and validate Inspire CLI configuration."""
    pass


@config.command("show")
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
        # Load config with source tracking (don't require credentials for show)
        cfg, sources = Config.from_files_and_env(
            require_credentials=False, require_target_dir=False
        )

        # Get config file paths
        global_path, project_path = Config.get_config_paths()

        if output_format == "json":
            _show_json(cfg, sources, global_path, project_path, compact, filter_category)
        elif output_format == "env":
            _show_env(cfg, sources, compact, filter_category)
        else:
            _show_table(cfg, sources, global_path, project_path, compact, filter_category)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


def _get_field_value(cfg: Config, option: ConfigOption) -> tuple[str | None, bool]:
    """Get config value for a given option.

    Returns:
        Tuple of (value_string, is_set) where is_set indicates if value differs from default
    """
    # Map TOML key to Config field name
    field_mapping = {
        "auth.username": "username",
        "auth.password": "password",
        "api.base_url": "base_url",
        "api.timeout": "timeout",
        "api.max_retries": "max_retries",
        "api.retry_delay": "retry_delay",
        "api.skip_ssl_verify": "skip_ssl_verify",
        "api.force_proxy": "force_proxy",
        "api.openapi_prefix": "openapi_prefix",
        "api.browser_api_prefix": "browser_api_prefix",
        "api.auth_endpoint": "auth_endpoint",
        "api.docker_registry": "docker_registry",
        "paths.target_dir": "target_dir",
        "paths.log_pattern": "log_pattern",
        "paths.job_cache": "job_cache_path",
        "paths.log_cache_dir": "log_cache_dir",
        "gitea.server": "gitea_server",
        "gitea.repo": "gitea_repo",
        "gitea.token": "gitea_token",
        "gitea.log_workflow": "gitea_log_workflow",
        "gitea.sync_workflow": "gitea_sync_workflow",
        "gitea.bridge_workflow": "gitea_bridge_workflow",
        "gitea.remote_timeout": "remote_timeout",
        "sync.default_remote": "default_remote",
        "bridge.action_timeout": "bridge_action_timeout",
        "bridge.denylist": "bridge_action_denylist",
        "job.priority": "job_priority",
        "job.image": "job_image",
        "job.project_id": "job_project_id",
        "job.workspace_id": "job_workspace_id",
        "job.shm_size": "shm_size",
        "notebook.resource": "notebook_resource",
        "notebook.image": "notebook_image",
        "ssh.rtunnel_bin": "rtunnel_bin",
        "ssh.sshd_deb_dir": "sshd_deb_dir",
        "ssh.dropbear_deb_dir": "dropbear_deb_dir",
        "ssh.rtunnel_download_url": "rtunnel_download_url",
        "mirrors.apt_mirror_url": "apt_mirror_url",
        "mirrors.pip_index_url": "pip_index_url",
        "mirrors.pip_trusted_host": "pip_trusted_host",
    }

    field_name = field_mapping.get(option.toml_key)
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
    field_mapping = {
        "auth.username": "username",
        "auth.password": "password",
        "api.base_url": "base_url",
        "api.timeout": "timeout",
        "api.max_retries": "max_retries",
        "api.retry_delay": "retry_delay",
        "api.skip_ssl_verify": "skip_ssl_verify",
        "api.force_proxy": "force_proxy",
        "api.openapi_prefix": "openapi_prefix",
        "api.browser_api_prefix": "browser_api_prefix",
        "api.auth_endpoint": "auth_endpoint",
        "api.docker_registry": "docker_registry",
        "paths.target_dir": "target_dir",
        "paths.log_pattern": "log_pattern",
        "paths.job_cache": "job_cache_path",
        "paths.log_cache_dir": "log_cache_dir",
        "gitea.server": "gitea_server",
        "gitea.repo": "gitea_repo",
        "gitea.token": "gitea_token",
        "gitea.log_workflow": "gitea_log_workflow",
        "gitea.sync_workflow": "gitea_sync_workflow",
        "gitea.bridge_workflow": "gitea_bridge_workflow",
        "gitea.remote_timeout": "remote_timeout",
        "sync.default_remote": "default_remote",
        "bridge.action_timeout": "bridge_action_timeout",
        "bridge.denylist": "bridge_action_denylist",
        "job.priority": "job_priority",
        "job.image": "job_image",
        "job.project_id": "job_project_id",
        "job.workspace_id": "job_workspace_id",
        "job.shm_size": "shm_size",
        "notebook.resource": "notebook_resource",
        "notebook.image": "notebook_image",
        "ssh.rtunnel_bin": "rtunnel_bin",
        "ssh.sshd_deb_dir": "sshd_deb_dir",
        "ssh.dropbear_deb_dir": "dropbear_deb_dir",
        "ssh.rtunnel_download_url": "rtunnel_download_url",
        "mirrors.apt_mirror_url": "apt_mirror_url",
        "mirrors.pip_index_url": "pip_index_url",
        "mirrors.pip_trusted_host": "pip_trusted_host",
    }

    field_name = field_mapping.get(option.toml_key)
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
        click.echo(f"  Global:  ~/.config/inspire/config.toml " + click.style("(not found)", fg="white"))
    if project_path:
        click.echo(f"  Project: {project_path} " + click.style("(found)", fg="green"))
    else:
        click.echo(f"  Project: ./inspire/config.toml " + click.style("(not found)", fg="white"))
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
    display_data: list[tuple[str, list[tuple[ConfigOption, str, str, str, str]]]] = []
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
            value_str, is_set = _get_field_value(cfg, option)
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
                "value": value_str if not option.secret else ("********" if value_str != "(not set)" else None),
                "source": source,
                "toml_key": option.toml_key,
                "description": option.description,
            }

    click.echo(json.dumps(result, indent=2))


def _show_env(
    cfg: Config,
    sources: dict[str, str],
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
            value_str, is_set = _get_field_value(cfg, option)
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


@config.command("env")
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
    lines = []
    lines.append("# Inspire CLI Environment Variables")
    lines.append("# Generated template - customize values as needed")
    lines.append("")

    # Define essential categories for minimal template
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
            # Add description as comment
            lines.append(f"# {option.description}")

            # Format the value
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


@config.command("check")
@pass_context
def check_config(ctx: Context) -> None:
    """Check environment configuration and API authentication.

    Verifies configuration (from files and environment) and attempts to
    authenticate with the Inspire API.
    """
    try:
        cfg, sources = Config.from_files_and_env(require_credentials=True)
        auth_ok = True
        auth_error = None

        # Attempt authentication
        try:
            AuthManager.get_api(cfg)
        except AuthenticationError as e:
            auth_ok = False
            auth_error = str(e)

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
            click.echo(
                f"Target dir:   {cfg.target_dir or '(not set - required for logs)'}"
            )
            click.echo(f"Log pattern:  {cfg.log_pattern}")
            click.echo(f"Job cache:    {cfg.get_expanded_cache_path()}")
            click.echo(f"Timeout:      {cfg.timeout}s")
            click.echo(f"Max retries:  {cfg.max_retries}")
            click.echo(f"Retry delay:  {cfg.retry_delay}s")

            if auth_error:
                click.echo(f"\nDetails: {auth_error}")

        # Exit non-zero if auth failed when not in JSON mode
        if not auth_ok:
            sys.exit(EXIT_AUTH_ERROR)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


def _handle_error(ctx: Context, error_type: str, message: str, exit_code: int) -> None:
    """Handle and format errors consistently."""
    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error(error_type, message, exit_code),
            err=True,
        )
    else:
        click.echo(human_formatter.format_error(message), err=True)
    sys.exit(exit_code)

