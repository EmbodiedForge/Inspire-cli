"""Init command for Inspire CLI.

Creates configuration files for global or project-specific settings.
Detects environment variables and auto-splits by scope (global/project).
"""

import os
import click
from pathlib import Path

from inspire.cli.utils.config import Config, PROJECT_CONFIG_DIR, CONFIG_FILENAME
from inspire.cli.utils.config_schema import (
    CONFIG_OPTIONS,
    ConfigOption,
    CATEGORY_ORDER,
    parse_value,
)


# TOML configuration template (used when no env vars detected or --template flag)
CONFIG_TEMPLATE = """# Inspire CLI Configuration
# Location: {location_comment}
#
# Values here are overridden by environment variables.
# Sensitive values (passwords, tokens) should use env vars.

[auth]
username = "your_username"
# password - use INSPIRE_PASSWORD env var

[api]
base_url = "https://api.example.com"
timeout = 30
max_retries = 3
retry_delay = 1.0

[paths]
target_dir = "/shared/EBM_dev"
log_pattern = "training_master_*.log"
job_cache = "~/.inspire/jobs.json"
log_cache_dir = "~/.inspire/logs"

[git]
# Platform selection: "gitea" or "github"
platform = "gitea"

[gitea]
server = "https://codeberg.org"
repo = "owner/repo"
# token - use INSP_GITEA_TOKEN env var
log_workflow = "retrieve_job_log.yml"
sync_workflow = "sync_code.yml"
bridge_workflow = "run_bridge_action.yml"
remote_timeout = 90

[github]
server = "https://github.com"
repo = "owner/repo"
# token - use INSP_GITHUB_TOKEN env var
log_workflow = "retrieve_job_log.yml"
sync_workflow = "sync_code.yml"
bridge_workflow = "run_bridge_action.yml"

[sync]
default_remote = "origin"

[bridge]
action_timeout = 300
denylist = ["*.tmp", ".git/*"]

[job]
# project_id = "project-..."
# workspace_id = "ws-..."
# image = "pytorch:latest"
# priority = 6

[notebook]
resource = "1xH200"
# image = "pytorch:latest"
"""


def _detect_env_vars() -> list[tuple[ConfigOption, str]]:
    """Detect which configuration env vars are currently set.

    Returns:
        List of (ConfigOption, value) tuples for set env vars
    """
    detected = []
    for option in CONFIG_OPTIONS:
        value = os.getenv(option.env_var)
        if value is not None and value != "":
            detected.append((option, value))
    return detected


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
        click.echo(click.style("Global config (~/.config/inspire/config.toml):", fg="cyan", bold=True))

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


def _generate_toml_content(
    detected: list[tuple[ConfigOption, str]], scope_filter: str | None = None
) -> str:
    """Generate TOML configuration file content from detected env vars.

    Secrets are always excluded for security.

    Args:
        detected: List of (ConfigOption, value) tuples
        scope_filter: If provided, only include options with matching scope ("global" or "project")

    Returns:
        TOML file content as string
    """
    lines = [
        "# Inspire CLI Configuration",
        "# Generated by 'inspire init'",
        "",
    ]

    # Filter by scope if specified
    if scope_filter:
        detected = [(opt, val) for opt, val in detected if opt.scope == scope_filter]

    # Group options by TOML section (first part of toml_key)
    by_section: dict[str, list[tuple[ConfigOption, str]]] = {}
    for option, value in detected:
        section = option.toml_key.split(".", 1)[0]
        if section not in by_section:
            by_section[section] = []
        by_section[section].append((option, value))

    # Define section order based on TOML key prefixes
    section_order = ["auth", "api", "paths", "gitea", "sync", "bridge", "job", "notebook", "ssh", "mirrors", "other"]

    for section in section_order:
        if section not in by_section:
            continue

        lines.append(f"[{section}]")

        for option, value in by_section[section]:
            key = option.toml_key.split(".", 1)[1]  # Get part after section

            # Always exclude secrets
            if option.secret:
                lines.append(f"# {key} - use env var {option.env_var} for security")
                continue

            # Format value based on type
            parsed = parse_value(option, value)
            if isinstance(parsed, bool):
                toml_value = "true" if parsed else "false"
            elif isinstance(parsed, int):
                toml_value = str(parsed)
            elif isinstance(parsed, float):
                toml_value = str(parsed)
            elif isinstance(parsed, list):
                # Format as TOML array
                toml_value = "[" + ", ".join(f'"{item}"' for item in parsed) + "]"
            else:
                # String - escape and quote
                toml_value = '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

            lines.append(f"{key} = {toml_value}")

        lines.append("")

    return "\n".join(lines)


def _init_smart_mode(
    detected: list[tuple[ConfigOption, str]],
    global_flag: bool,
    project_flag: bool,
    force: bool,
) -> None:
    """Initialize config using detected env vars (smart mode).

    Auto-splits by scope unless --global or --project is specified.
    Secrets are always excluded.
    """
    # Show preview grouped by scope
    _format_preview_by_scope(detected)

    # Count values and scopes
    secrets = [opt for opt, _ in detected if opt.secret]
    non_secrets = [(opt, val) for opt, val in detected if not opt.secret]
    global_opts = [(opt, val) for opt, val in detected if opt.scope == "global"]
    project_opts = [(opt, val) for opt, val in detected if opt.scope == "project"]

    click.echo(f"Found {len(detected)} environment variable(s):")
    click.echo(f"  - {len(non_secrets)} regular value(s)")
    if secrets:
        click.echo(f"  - {len(secrets)} secret(s) (excluded)")
    if not global_flag and not project_flag:
        click.echo(f"  - {len(global_opts)} global-scope option(s)")
        click.echo(f"  - {len(project_opts)} project-scope option(s)")
    click.echo()

    # Define paths
    global_path = Config.GLOBAL_CONFIG_PATH
    project_path = Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME

    # Handle different modes
    if global_flag:
        # Force all to global
        _write_single_file(detected, global_path, force, "global")
    elif project_flag:
        # Force all to project
        _write_single_file(detected, project_path, force, "project")
    else:
        # Auto-split by scope
        _write_auto_split(detected, global_opts, project_opts, global_path, project_path, force, secrets)


def _write_single_file(
    detected: list[tuple[ConfigOption, str]],
    output_path: Path,
    force: bool,
    dest_name: str,
) -> None:
    """Write all detected options to a single config file."""
    # Check for existing file
    if output_path.exists() and not force:
        click.echo(
            click.style(f"Config file already exists: {output_path}", fg="yellow")
        )
        if not click.confirm("\nOverwrite existing config?"):
            click.echo("Aborted.")
            return

    # Generate TOML content (no scope filter - include all)
    toml_content = _generate_toml_content(detected)

    # Create parent directory if needed
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write the file
    output_path.write_text(toml_content)
    click.echo(click.style(f"Created {output_path}", fg="green"))
    click.echo()

    # Next steps
    _show_next_steps(detected)


def _write_auto_split(
    detected: list[tuple[ConfigOption, str]],
    global_opts: list[tuple[ConfigOption, str]],
    project_opts: list[tuple[ConfigOption, str]],
    global_path: Path,
    project_path: Path,
    force: bool,
    secrets: list[ConfigOption],
) -> None:
    """Write config files split by scope (auto-split mode)."""
    files_to_write = []

    # Check global config
    if global_opts:
        if global_path.exists() and not force:
            click.echo(f"Global config already exists: {global_path}")
            if click.confirm("Overwrite?", default=False):
                files_to_write.append(("global", global_path))
            else:
                click.echo("Skipping global config.")
            click.echo()
        else:
            files_to_write.append(("global", global_path))

    # Check project config
    if project_opts:
        if project_path.exists() and not force:
            click.echo(f"Project config already exists: {project_path}")
            if click.confirm("Overwrite?", default=False):
                files_to_write.append(("project", project_path))
            else:
                click.echo("Skipping project config.")
            click.echo()
        else:
            files_to_write.append(("project", project_path))

    if not files_to_write:
        click.echo("No files written.")
        return

    # Write files
    for scope, path in files_to_write:
        content = _generate_toml_content(detected, scope_filter=scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        color = "cyan" if scope == "global" else "green"
        click.echo(click.style(f"Created {path}", fg=color))

    click.echo()
    _show_next_steps(detected)


def _show_next_steps(detected: list[tuple[ConfigOption, str]]) -> None:
    """Show next steps after config creation."""
    secrets = [opt for opt, _ in detected if opt.secret]

    click.echo(click.style("Next steps:", bold=True))
    step = 1
    if secrets:
        secret_vars = ", ".join(opt.env_var for opt in secrets)
        click.echo(f"  {step}. Keep {secret_vars} as env var(s) (not written for security)")
        step += 1
    click.echo(f"  {step}. Verify with: inspire config show")


def _init_template_mode(global_flag: bool, project_flag: bool, force: bool) -> None:
    """Initialize config using template with placeholders (template mode).

    Prompts for destination if neither --global nor --project specified.
    """
    # Determine destination
    if global_flag:
        config_dir = Config.GLOBAL_CONFIG_PATH.parent
        config_path = Config.GLOBAL_CONFIG_PATH
        location_comment = "~/.config/inspire/config.toml (global)"
    elif project_flag:
        config_path = Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        config_dir = config_path.parent
        location_comment = "./.inspire/config.toml (project-specific)"
    else:
        # Prompt user
        click.echo("Where would you like to create the config?")
        click.echo("  [g] Global config (~/.config/inspire/config.toml)")
        click.echo("  [p] Project config (./.inspire/config.toml)")
        choice = click.prompt("Choice", default="p", type=click.Choice(["g", "p"], case_sensitive=False))

        if choice.lower() == "g":
            config_dir = Config.GLOBAL_CONFIG_PATH.parent
            config_path = Config.GLOBAL_CONFIG_PATH
            location_comment = "~/.config/inspire/config.toml (global)"
        else:
            config_path = Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME
            config_dir = config_path.parent
            location_comment = "./.inspire/config.toml (project-specific)"

    # Check if config already exists
    if config_path.exists() and not force:
        click.echo(
            click.style(f"Config file already exists: {config_path}", fg="yellow")
        )
        if not click.confirm("\nOverwrite existing config?"):
            click.echo("Aborted.")
            return

    # Create directory if needed
    config_dir.mkdir(parents=True, exist_ok=True)

    # Write config template
    content = CONFIG_TEMPLATE.format(location_comment=location_comment)
    config_path.write_text(content)

    # Success message
    click.echo(click.style(f"Created {config_path}", fg="green"))

    click.echo("\nNext steps:")
    click.echo(f"  1. Edit {config_path} with your settings")
    click.echo("  2. Set INSPIRE_USERNAME and INSPIRE_PASSWORD environment variables")
    click.echo("  3. Run 'inspire config show' to verify your configuration")


@click.command()
@click.option(
    "--global",
    "-g",
    "global_flag",
    is_flag=True,
    help="Force all options to global config (~/.config/inspire/)",
)
@click.option(
    "--project",
    "-p",
    "project_flag",
    is_flag=True,
    help="Force all options to project config (./.inspire/)",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Overwrite existing files without prompting",
)
@click.option(
    "--template",
    "-t",
    "template_flag",
    is_flag=True,
    help="Create template with placeholders (skip env var detection)",
)
def init(global_flag: bool, project_flag: bool, force: bool, template_flag: bool) -> None:
    """Initialize Inspire CLI configuration.

    Detects environment variables and creates config.toml files.
    By default, options are auto-split by scope: global options go to
    ~/.config/inspire/config.toml, project options go to ./.inspire/config.toml.

    Use --global or --project to force all options to a single file.
    Secrets (passwords, tokens) are never written to config files for security.

    If no environment variables are detected (or with --template), creates
    a template config with placeholder values.

    \b
    Examples:
        # Auto-detect env vars and split by scope
        inspire init

        \b
        # Force all options to global config
        inspire init --global

        \b
        # Force all options to project config
        inspire init --project

        \b
        # Create template with placeholders
        inspire init --template
    """
    # Validate flags
    if global_flag and project_flag:
        click.echo(click.style("Error: Cannot specify both --global and --project", fg="red"))
        raise SystemExit(1)

    # Template mode - skip env var detection
    if template_flag:
        click.echo("Creating template config with placeholders.\n")
        _init_template_mode(global_flag, project_flag, force)
        return

    # Detect environment variables
    detected = _detect_env_vars()

    if detected:
        # Smart mode - use detected env vars
        _init_smart_mode(detected, global_flag, project_flag, force)
    else:
        # No env vars - fall back to template mode
        click.echo("No environment variables detected. Creating template config.\n")
        _init_template_mode(global_flag, project_flag, force)
