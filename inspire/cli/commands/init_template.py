"""Init helpers: template mode."""

from __future__ import annotations

from pathlib import Path

import click

from inspire.config import CONFIG_FILENAME, PROJECT_CONFIG_DIR, Config

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
action_timeout = 600
denylist = ["*.tmp", ".git/*"]

[workspaces]
# cpu = "ws-..."       # Default workspace (CPU jobs / notebooks)
# gpu = "ws-..."       # GPU workspace (H100/H200 jobs)
# internet = "ws-..."  # Internet-enabled GPU workspace (e.g. RTX 4090)
# special = "ws-..."   # Custom alias (use with --workspace special)

[job]
# project_id = "project-..."
# workspace_id = "ws-..."
# image = "pytorch:latest"
# priority = 6
# shm_size = 32  # Default shared memory (GiB) for notebooks; jobs use it when set

[notebook]
resource = "1xH200"
# image = "pytorch:latest"

[remote_env]
# Environment variables exported before remote commands run.
# Tip: use "$VARNAME" or "${{VARNAME}}" to pull from your *local* env at runtime.
# WANDB_API_KEY = "$WANDB_API_KEY"
# HF_TOKEN = "$HF_TOKEN"
"""


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
        choice = click.prompt(
            "Choice", default="p", type=click.Choice(["g", "p"], case_sensitive=False)
        )

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
        click.echo(click.style(f"Config file already exists: {config_path}", fg="yellow"))
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
