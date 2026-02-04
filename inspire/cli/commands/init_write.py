"""Init helpers: writing config files."""

from __future__ import annotations

from pathlib import Path

import click

from inspire.cli.commands.init_env import _generate_toml_content
from inspire.config import CONFIG_FILENAME, PROJECT_CONFIG_DIR, ConfigOption


def _write_single_file(
    detected: list[tuple[ConfigOption, str]],
    output_path: Path,
    force: bool,
    dest_name: str,
) -> None:
    """Write all detected options to a single config file."""
    _ = dest_name  # unused (kept to preserve signature)

    # Check for existing file
    if output_path.exists() and not force:
        click.echo(click.style(f"Config file already exists: {output_path}", fg="yellow"))
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
    _ = secrets  # unused (kept to preserve signature)

    files_to_write: list[tuple[str, Path]] = []

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


def _project_config_path() -> Path:
    return Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME
