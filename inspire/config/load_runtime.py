"""Environment and validation helpers for config loading."""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Any

from inspire.config.models import SOURCE_ENV, SOURCE_GLOBAL, SOURCE_PROJECT, ConfigError
from inspire.config.schema import CONFIG_OPTIONS

_PASSWORD_COMMAND_TIMEOUT_SECONDS = 5


def _run_password_command(command: str) -> str:
    """Run a configured password provider without a shell or diagnostic leakage."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ConfigError("Invalid password_command syntax") from exc
    if not argv:
        raise ConfigError("password_command cannot be empty")

    try:
        completed = subprocess.run(
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=_PASSWORD_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConfigError("password_command timed out") from exc
    except OSError as exc:
        raise ConfigError("password_command could not be executed") from exc

    if completed.returncode != 0:
        raise ConfigError(f"password_command failed with exit code {completed.returncode}")
    password = completed.stdout.rstrip("\r\n")
    if not password:
        raise ConfigError("password_command returned empty stdout")
    return password


def _apply_env_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    prefer_source: str,
    protected_fields: set[str] | None = None,
) -> str | None:
    env_password = os.getenv("INSPIRE_PASSWORD")
    protected_fields = protected_fields or set()

    for option in CONFIG_OPTIONS:
        if option.env_var == "INSPIRE_PASSWORD":
            continue

        value = os.getenv(option.env_var)
        if value is None and option.env_var == "INSP_LOG_CACHE_DIR":
            value = os.getenv("INSPIRE_LOG_CACHE_DIR")
        if value is None:
            continue

        field_name = option.field_name
        if field_name not in config_dict or field_name in protected_fields:
            continue

        if option.parser:
            try:
                parsed_value = option.parser(value)
            except (ValueError, TypeError) as e:
                raise ConfigError(f"Invalid {option.env_var} value: {value}") from e
            new_value = parsed_value
        else:
            new_value = value

        if prefer_source == "toml" and sources.get(field_name) == SOURCE_PROJECT:
            continue

        config_dict[field_name] = new_value
        sources[field_name] = SOURCE_ENV

    for env_var, field_name in (
        ("INSPIRE_DEFAULT_WORKSPACE_ID", "default_workspace_id"),
        ("INSPIRE_WORKSPACE_ID", "job_workspace_id"),
        ("INSPIRE_NOTEBOOK_WORKSPACE_ID", "notebook_workspace_id"),
    ):
        value = os.getenv(env_var)
        if value is None:
            continue
        if prefer_source == "toml" and sources.get(field_name) == SOURCE_PROJECT:
            continue
        config_dict[field_name] = value
        sources[field_name] = SOURCE_ENV

    return env_password


def _apply_password_and_token_fallbacks(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    project_accounts: dict[str, str],
    env_password: str | None,
    prefer_source: str,
) -> None:
    selected_account_alias = str(config_dict.get("account_alias") or "").strip()
    account_scoped_credential = bool(
        selected_account_alias
        and (str(config_dict.get("password_command") or "").strip() or config_dict.get("password"))
    )
    project_toml_selects_legacy_identity = (
        prefer_source == "toml"
        and not selected_account_alias
        and sources.get("username") == SOURCE_PROJECT
    )
    if env_password and not project_toml_selects_legacy_identity and not account_scoped_credential:
        config_dict["password"] = env_password
        sources["password"] = SOURCE_ENV
    else:
        password_command = str(config_dict.get("password_command") or "").strip()
        if password_command:
            config_dict["password"] = _run_password_command(password_command)
            sources["password"] = sources.get("password_command", SOURCE_GLOBAL)
        else:
            resolved_username = str(config_dict.get("username") or "").strip()
            account_key = str(config_dict.get("account_alias") or "").strip() or resolved_username
            account_password = config_dict.get("accounts", {}).get(account_key)
            if account_password:
                config_dict["password"] = account_password
                sources["password"] = (
                    SOURCE_PROJECT if account_key in project_accounts else SOURCE_GLOBAL
                )

    if not config_dict.get("github_token"):
        github_token_fallback = os.getenv("GITHUB_TOKEN")
        if github_token_fallback:
            config_dict["github_token"] = github_token_fallback
            sources["github_token"] = SOURCE_ENV


def _validate_required_config(
    *,
    config_dict: dict[str, Any],
    require_credentials: bool,
    require_target_dir: bool,
) -> None:
    if require_credentials:
        if not config_dict["username"]:
            raise ConfigError(
                "Missing username configuration.\n"
                "Select an account with --account/INSPIRE_ACCOUNT/[context].account, "
                "or configure legacy INSPIRE_USERNAME/[auth].username."
            )
        if not config_dict["password"]:
            raise ConfigError(
                "Missing password configuration.\n"
                "Set INSPIRE_PASSWORD env var or add an account password in config.toml:\n"
                '  [accounts."your-alias"]\n'
                "  username = 'your_platform_username'\n"
                "  password = 'your_password'"
            )

    if require_target_dir and not config_dict["target_dir"]:
        raise ConfigError(
            "Missing target directory configuration.\n"
            "Set INSPIRE_TARGET_DIR env var or add to config.toml:\n"
            "  [defaults]\n"
            "  target_dir = '/path/to/shared/directory'"
        )


__all__ = [
    "_apply_env_layer",
    "_apply_password_and_token_fallbacks",
    "_run_password_command",
    "_validate_required_config",
]
