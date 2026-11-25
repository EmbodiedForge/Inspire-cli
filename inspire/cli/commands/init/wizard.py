"""Interactive setup wizard for Inspire CLI configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import click
import questionary

from inspire.config.toml import _load_toml


class _WizardCancelled(Exception):
    """Raised when the interactive wizard is cancelled by the user."""


# Empty answers to a required prompt are re-asked, then the step fails.
_REQUIRED_PROMPT_ATTEMPTS = 3


def _wizard_info(message: str) -> None:
    click.echo(message, err=True)


def _wizard_warning(message: str) -> None:
    click.echo(f"Warning: {message}", err=True)


def _wizard_error(message: str) -> None:
    click.echo(f"Error: {message}", err=True)


def _wizard_success(message: str) -> None:
    click.echo(f"OK {message}")


def _as_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_project_label(project: dict[str, Any]) -> str:
    name = str(project.get("name", "Unknown")).strip() or "Unknown"

    fragments: list[str] = []

    gpu_limit = bool(project.get("gpu_limit", False))
    member_gpu_limit = bool(project.get("member_gpu_limit", False))
    member_gpu_hours = _as_float(project.get("member_remain_gpu_hours"))
    member_budget = _as_float(project.get("member_remain_budget"))
    remain_budget = _as_float(project.get("remain_budget"))

    if not gpu_limit:
        fragments.append("no GPU-hour limit")
    elif member_gpu_limit:
        fragments.append(f"member GPU hrs: {member_gpu_hours:,.1f}")
    else:
        fragments.append("GPU-hour limit enforced")

    if member_budget:
        fragments.append(f"my budget: {member_budget:,.0f}")
    elif remain_budget:
        fragments.append(f"project budget: {remain_budget:,.0f}")

    return f"{name} ({', '.join(fragments)})" if fragments else name


def _get_existing_config_summary(global_path: Path, project_path: Path) -> dict[str, Any]:
    """Get summary of existing config files."""
    summary: dict[str, Any] = {
        "global_exists": global_path.exists(),
        "project_exists": project_path.exists(),
        "global_config": None,
        "project_config": None,
    }

    if global_path.exists():
        try:
            summary["global_config"] = _load_toml(global_path)
        except Exception:
            pass

    if project_path.exists():
        try:
            summary["project_config"] = _load_toml(project_path)
        except Exception:
            pass

    return summary


def _ask_or_cancel(prompt):  # noqa: ANN001
    answer = prompt.ask()
    if answer is None:
        raise _WizardCancelled()
    return answer


def _check_existing_configs(
    global_path: Path,
    project_path: Path,
    force: bool,
) -> bool:
    """Check for existing configs and prompt for overwrite if needed.

    Returns True if should proceed, False if user cancelled.
    """
    existing = []
    if global_path.exists():
        existing.append(f"Global: {global_path}")
    if project_path.exists():
        existing.append(f"Project: {project_path}")

    if not existing:
        return True

    if force:
        return True

    click.echo(click.style("\nExisting configuration files found:", fg="yellow"))
    for path in existing:
        click.echo(f"  - {path}")

    should_overwrite = questionary.confirm(
        "Overwrite existing configuration?",
        default=False,
    )
    should_overwrite = _ask_or_cancel(should_overwrite)

    return bool(should_overwrite)


def _prompt_credentials() -> tuple[str, str, str] | None:
    """Prompt user for credentials.

    Returns (username, password, base_url) or None if cancelled.
    """
    click.echo(click.style("\nStep 1: Credentials", fg="blue", bold=True))
    click.echo("Enter your Inspire platform credentials.\n")

    # Check for existing env vars
    env_username = os.environ.get("INSPIRE_USERNAME", "")
    env_base_url = os.environ.get("INSPIRE_BASE_URL", "")

    if env_username:
        _wizard_info(f"Using username from INSPIRE_USERNAME: {env_username}")
    if env_base_url:
        _wizard_info(f"Using base URL from INSPIRE_BASE_URL: {env_base_url}")

    # Prompt for username
    username = questionary.text(
        "Username:",
        default=env_username,
    )
    username = _ask_or_cancel(username)

    if not username:
        _wizard_error("Username is required")
        return None

    # Prompt for password
    password = questionary.password(
        "Password:",
    )
    password = _ask_or_cancel(password)

    if not password:
        _wizard_error("Password is required")
        return None

    # Prompt for base URL. The example is display text only — defaulting to it
    # would persist a placeholder host that nothing can resolve against.
    base_url_default = env_base_url
    base_url = ""
    for _ in range(_REQUIRED_PROMPT_ATTEMPTS):
        base_url = str(
            _ask_or_cancel(
                questionary.text(
                    "Base URL (e.g., https://inspire.example.com):",
                    default=base_url_default,
                )
            )
            or ""
        ).strip()
        if base_url:
            break
        _wizard_error("Base URL is required")
        base_url_default = ""

    if not base_url:
        return None

    return username, password, base_url


def _prompt_account_alias(default: str | None = None) -> str | None:
    """Prompt for the local account alias, which is distinct from username."""
    prompt = questionary.text(
        "Account alias:",
        default=str(default or "").strip(),
    )
    alias = str(_ask_or_cancel(prompt) or "").strip()
    if not alias:
        _wizard_error("Account alias is required")
        return None
    return alias


def _prompt_discovery() -> bool:
    """Prompt whether to run discovery.

    Returns True if should run discovery.
    """
    click.echo(click.style("\nStep 2: Discovery", fg="blue", bold=True))

    prompt = questionary.confirm(
        "Would you like to discover available projects and workspaces?\n"
        "This will log in to the web UI and scan for accessible resources.",
        default=True,
    )
    return bool(_ask_or_cancel(prompt))


def _prompt_project_ranking(projects: list[dict[str, Any]]) -> list[str]:
    """Prompt user to rank projects by priority.

    Returns list of project IDs in priority order.
    """
    if not projects:
        return []

    click.echo(click.style("\nStep 3: Project Selection", fg="blue", bold=True))
    click.echo("Select projects in priority order (highest priority first).\n")

    # Format project choices
    choices = []
    for proj in projects:
        proj_id = proj.get("id", "")
        label = _format_project_label(proj)
        choices.append(questionary.Choice(title=label, value=proj_id))

    # Let user select and order projects
    selected_prompt = questionary.checkbox(
        "Select projects (use space to select, then we'll order them):",
        choices=choices,
    )
    selected = cast(list[str], _ask_or_cancel(selected_prompt))

    if selected == []:
        return []

    # Now let them order the selected projects
    if len(selected) > 1:
        click.echo("\nNow let's order them by priority (highest first):\n")

        ordered: list[str] = []
        remaining = selected.copy()

        for i in range(len(selected)):
            # Create choices from remaining projects
            remaining_choices = [c for c in choices if c.value in remaining]

            if len(remaining_choices) == 1:
                ordered.append(cast(str, remaining_choices[0].value))
                break

            choice_prompt = questionary.select(
                f"Priority {i + 1}:",
                choices=remaining_choices,
            )
            choice = cast(str, _ask_or_cancel(choice_prompt))

            ordered.append(choice)
            remaining.remove(choice)

        return ordered

    return selected


def _prompt_target_dir(default: str | None = None) -> str | None:
    """Prompt for target directory."""
    click.echo(click.style("\nStep 4: Target Directory", fg="blue", bold=True))
    click.echo(
        "Enter the target directory on the shared filesystem.\n"
        "This is where your code will be synced and jobs will run.\n"
    )

    target_dir = questionary.text(
        "Target directory:",
        default=default or "",
    )
    target_dir = _ask_or_cancel(target_dir)

    return target_dir if target_dir else None


def _create_config_files(
    global_path: Path,
    project_path: Path,
    account_alias: str,
    credentials: tuple[str, str, str],
    project_order: list[str],
    target_dir: str | None,
    discover_results: dict[str, Any] | None,
) -> None:
    """Create the configuration files."""
    import tomlkit

    username, password, base_url = credentials

    # Create global config
    global_doc = tomlkit.document()
    global_doc.add(tomlkit.comment("Inspire CLI Global Configuration"))
    global_doc.add(tomlkit.nl())

    context_table = tomlkit.table()
    context_table.add("account", account_alias)
    global_doc.add("context", context_table)
    global_doc.add(tomlkit.nl())

    # Accounts section with password
    accounts_table = tomlkit.table()
    user_account = tomlkit.table()
    user_account.add("username", username)
    user_account.add("password", password)
    user_account.add("base_url", base_url.rstrip("/"))
    user_account.add("auth_method", "http")

    # Add discovered data if available
    if discover_results:
        # Compute groups
        if "compute_groups" in discover_results:
            compute_groups_aot = tomlkit.aot()
            for cg in discover_results["compute_groups"]:
                cg_table = tomlkit.table()
                cg_table.add("id", cg.get("id", ""))
                cg_table.add("name", cg.get("name", ""))
                if "gpu_type" in cg:
                    cg_table.add("gpu_type", cg["gpu_type"])
                if "location" in cg:
                    cg_table.add("location", cg["location"])
                workspace_ids = [
                    str(ws_id)
                    for ws_id in (cg.get("workspace_ids") or [])
                    if str(ws_id or "").strip()
                ]
                if workspace_ids:
                    cg_table.add("workspace_ids", workspace_ids)
                compute_groups_aot.append(cg_table)
            if compute_groups_aot:
                global_doc.add("compute_groups", compute_groups_aot)

    accounts_table.add(account_alias, user_account)
    global_doc.add("accounts", accounts_table)
    global_doc.add(tomlkit.nl())

    # Save global config
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(tomlkit.dumps(global_doc), encoding="utf-8")

    # Create project config
    project_doc = tomlkit.document()
    project_doc.add(tomlkit.comment("Inspire CLI Project Configuration"))
    project_doc.add(tomlkit.nl())

    # CLI section
    cli_table = tomlkit.table()
    cli_table.add("prefer_source", "toml")
    project_doc.add("cli", cli_table)
    project_doc.add(tomlkit.nl())

    context_table = tomlkit.table()
    context_table.add("account", account_alias)
    project_doc.add("context", context_table)
    project_doc.add(tomlkit.nl())

    # Defaults section
    defaults_table = tomlkit.table()
    if project_order:
        defaults_table.add("project_order", project_order)
    if target_dir:
        defaults_table.add("target_dir", target_dir)
    if defaults_table:
        project_doc.add("defaults", defaults_table)
        project_doc.add(tomlkit.nl())

    # Save project config
    project_path.parent.mkdir(parents=True, exist_ok=True)
    project_path.write_text(tomlkit.dumps(project_doc), encoding="utf-8")


def run_wizard(
    global_path: Path,
    project_path: Path,
    force: bool = False,
    yes: bool = False,
    account_alias: str | None = None,
) -> bool:
    """Run the interactive setup wizard.

    Returns True if successful, False otherwise.
    """
    click.echo(click.style("\n" + "=" * 60, fg="cyan"))
    click.echo(click.style("  Inspire CLI Setup Wizard", fg="cyan", bold=True))
    click.echo(click.style("=" * 60, fg="cyan"))
    click.echo()
    click.echo(
        "This wizard will guide you through setting up Inspire CLI configuration.\n"
        "We'll create both global (~/.config/inspire/config.toml) and\n"
        "project (./.inspire/config.toml) configuration files.\n"
    )

    try:
        # Check existing configs
        if not _check_existing_configs(global_path, project_path, force):
            _wizard_info("Setup cancelled.")
            return False

        account_alias = str(account_alias or "").strip() or _prompt_account_alias()
        if not account_alias:
            return False

        # Step 1: Credentials
        credentials = _prompt_credentials()
        if not credentials:
            return False

        # Step 2: Discovery (optional)
        discover_results = None
        if _prompt_discovery():
            click.echo()
            from .wizard_discovery import run_discovery_for_wizard

            discover_results = run_discovery_for_wizard(
                username=credentials[0],
                password=credentials[1],
                base_url=credentials[2],
            )

            if discover_results and not discover_results.get("success"):
                _wizard_warning("Discovery encountered issues but continuing...")

        # Step 3: Project ranking (if we have projects)
        project_order = []
        if discover_results and "projects" in discover_results:
            project_order = _prompt_project_ranking(discover_results["projects"])

        # Step 4: Target directory
        target_dir = _prompt_target_dir()

        # Review
        click.echo(click.style("\n" + "-" * 60, fg="cyan"))
        click.echo(click.style("Configuration Summary:", fg="cyan", bold=True))
        click.echo("-" * 60)
        click.echo(f"Account alias: {account_alias}")
        click.echo(f"Username:     {credentials[0]}")
        click.echo(f"Base URL:     {credentials[2]}")
        click.echo(f"Project Order: {', '.join(project_order) if project_order else '(not set)'}")
        click.echo(f"Target Dir:   {target_dir or '(not set)'}")
        click.echo()

        if not yes:
            confirm = questionary.confirm(
                "Create configuration files with these settings?",
                default=True,
            )
            confirm = _ask_or_cancel(confirm)

            if not confirm:
                _wizard_info("Setup cancelled.")
                return False

        _create_config_files(
            global_path,
            project_path,
            account_alias,
            credentials,
            project_order,
            target_dir,
            discover_results,
        )

        _wizard_success("Configuration files created successfully!")
        click.echo()
        click.echo(f"Global config:  {global_path}")
        click.echo(f"Project config: {project_path}")
        click.echo()
        click.echo("Next steps:")
        click.echo("  1. Run 'inspire config check' to verify the configuration")
        click.echo("  2. Run 'inspire project list' to see available projects")
        click.echo("  3. Run 'inspire notebook list' to see your notebooks")

        return True
    except _WizardCancelled:
        _wizard_info("Setup cancelled.")
        return False

    except Exception as e:
        _wizard_error(f"Failed to create configuration: {e}")
        return False
