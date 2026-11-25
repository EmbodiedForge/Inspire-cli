"""Config set command -- interactively or explicitly update configuration values."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, cast

import click
import questionary
import tomlkit
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from questionary import confirm, text

from inspire.cli.context import (
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.output import emit_success, emit_warning
from inspire.config import PROJECT_CONFIG_DIR, Config, ConfigError
from inspire.config.schema import (
    get_categories,
    get_manual_edit_redirect,
    get_user_managed_option_by_toml,
    get_user_managed_options,
    is_discovery_owned_toml_key,
)
from inspire.config.schema_models import ConfigOption
from inspire.config.toml import _find_project_config

_REMOTE_ENV_PREFIX = "remote_env."
_DISCOVERY_OWNED_PREFIXES = ("accounts.", "compute_groups", "project_catalog.", "projects.")
_REMOTE_ENV_CATEGORY = "Remote Environment"
_SECRET_MASK = "********"

_MENU_BACK_SENTINEL = -2
_MENU_CANCEL_SENTINEL = -1


def _pick_menu(
    title: str,
    entries: list[str],
    cursor_index: int = 0,
    *,
    allow_back: bool = False,
) -> int | None:
    if not sys.stdin.isatty():
        click.echo(
            click.style(
                "Interactive mode requires a terminal (TTY). "
                "Use 'inspire config set <key> <value>' instead.",
                fg="yellow",
            )
        )
        return _MENU_CANCEL_SENTINEL

    try:
        # Disable CPR probing to avoid redraw glitches and warnings in simpler terminals/PTYS.
        os.environ.setdefault("PROMPT_TOOLKIT_NO_CPR", "1")
        default_index = max(0, min(cursor_index, len(entries) - 1))
        choices = [
            questionary.Choice(title=entry, value=index) for index, entry in enumerate(entries)
        ]
        selected = questionary.select(
            title,
            choices=choices,
            default=choices[default_index],
            instruction="(Left: back, Right/Enter: select)",
            erase_when_done=True,
        )
        key_bindings = cast(KeyBindings, selected.application.key_bindings)
        enter_handler = next(
            (
                binding.handler
                for binding in key_bindings.bindings
                if binding.keys == (Keys.ControlM,)
            ),
            None,
        )

        @key_bindings.add(Keys.Right, eager=True)
        def _right_arrow(event):
            if enter_handler is not None:
                enter_handler(event)

        @key_bindings.add(Keys.Left, eager=True)
        def _left_arrow(event):
            if allow_back:
                event.app.exit(result=_MENU_BACK_SENTINEL)

        result = selected.ask()
    except (EOFError, KeyboardInterrupt, OSError):
        click.echo(
            click.style(
                "Interactive mode requires a terminal (TTY). "
                "Use 'inspire config set <key> <value>' instead.",
                fg="yellow",
            )
        )
        return _MENU_CANCEL_SENTINEL

    if result is None:
        return _MENU_CANCEL_SENTINEL
    return result


def _parse_toml_key(key: str) -> tuple[str | None, str]:
    if "." in key:
        parts = key.split(".", 1)
        return parts[0], parts[1]
    return None, key


def _parse_user_managed_key(toml_key: str) -> tuple[ConfigOption | None, bool]:
    redirect = get_manual_edit_redirect(toml_key)
    if redirect:
        raise ConfigError(
            f"Config key '{toml_key}' is deprecated for manual edits. Use '{redirect}' instead."
        )

    if toml_key.startswith(_REMOTE_ENV_PREFIX):
        env_name = toml_key[len(_REMOTE_ENV_PREFIX) :].strip()
        if not env_name:
            raise ConfigError(
                "Config key 'remote_env' is incomplete. "
                "Use 'remote_env.NAME value' to set a remote environment variable."
            )
        return None, True

    if is_discovery_owned_toml_key(toml_key) or toml_key.startswith(_DISCOVERY_OWNED_PREFIXES):
        raise ConfigError(
            f"Config key '{toml_key}' is discovery-owned. "
            "Use 'inspire init --discover' to refresh discovered account, workspace, "
            "and compute-group data."
        )

    option = get_user_managed_option_by_toml(toml_key)
    if option is None:
        raise ConfigError(
            f"Unknown config key '{toml_key}'. "
            "'inspire config set' only supports declared user-managed keys."
        )
    return option, False


def _load_toml_doc(path: Path) -> tomlkit.TOMLDocument:
    return tomlkit.parse(path.read_text(encoding="utf-8"))


def _save_toml_doc(path: Path, doc: tomlkit.TOMLDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def _set_toml_value(doc: tomlkit.TOMLDocument, section: str | None, key: str, value: Any) -> None:
    if section is None:
        doc[key] = value
    else:
        if section not in doc:
            doc[section] = tomlkit.table()
        doc[section][key] = value


def _get_current_value(doc: tomlkit.TOMLDocument, section: str | None, key: str) -> Any:
    if section and section in doc:
        return doc[section].get(key)
    if not section:
        return doc.get(key)
    return None


def _validate_value(option: ConfigOption, value_str: str) -> tuple[Any, bool, str | None]:
    try:
        parsed = option.parser(value_str) if option.parser else value_str
        if option.validator and not option.validator(parsed):
            return parsed, False, "Value failed validation."
        return parsed, True, None
    except (ValueError, TypeError) as exc:
        try:
            if value_str.startswith("[") or value_str.startswith("{"):
                parsed = json.loads(value_str)
            elif value_str.lower() in ("true", "false"):
                parsed = value_str.lower() == "true"
            elif value_str.isdigit() or (value_str.startswith("-") and value_str[1:].isdigit()):
                parsed = int(value_str)
            elif value_str.replace(".", "", 1).isdigit():
                parsed = float(value_str)
            else:
                parsed = value_str
            return parsed, False, str(exc)
        except json.JSONDecodeError:
            return value_str, False, str(exc)


def _resolve_config_path(use_global: bool, use_project: bool) -> Path:
    if use_global and use_project:
        raise ConfigError("Cannot specify both --global and --project")
    if use_global:
        return Config.resolve_global_config_path()
    if use_project:
        return _find_project_config() or Path.cwd() / PROJECT_CONFIG_DIR / "config.toml"
    return _find_project_config() or Config.resolve_global_config_path()


def _complete_config_key(ctx, param, incomplete):
    from inspire.cli.completion import get_config_key_completions

    return [c for c in get_config_key_completions() if c.value.startswith(incomplete)]


def _is_secret_toml_key(toml_key: str) -> bool:
    option = get_user_managed_option_by_toml(toml_key)
    return option is not None and option.secret


def _mask_value(value: Any, secret: bool) -> Any:
    if secret and value is not None and value != "":
        return _SECRET_MASK
    return value


def _display(value: Any, secret: bool = False) -> str:
    masked = _mask_value(value, secret)
    if masked is None:
        return "(not set)"
    if isinstance(masked, list):
        return json.dumps(masked)
    return str(masked)


def _display_effective(value: Any, default: Any | None = None, secret: bool = False) -> str:
    if value is not None:
        return _display(value, secret)
    if default is not None:
        return f"{_display(default)} (default)"
    return _display(value)


def _prompt_default_str(current: Any, default: Any | None, secret: bool) -> str:
    """Pre-fill a value prompt without exposing a stored secret.

    The ``Current:`` line masks secrets, so pre-filling the raw stored value would put
    it back on screen where the mask cannot reach it.
    """
    if secret:
        return ""
    if current is not None:
        return str(current)
    if default is not None:
        return _display(default)
    return ""


def _apply_change(ctx, doc, config_path, section, key, toml_key, old, new, dry_run):
    secret = _is_secret_toml_key(toml_key)
    if dry_run:
        click.echo(click.style("Dry run - would update:", fg="yellow"))
        click.echo(f"  File: {config_path}")
        click.echo(f"  Key:  {toml_key}")
        if old is not None:
            click.echo(f"  Old:  {_display(old, secret)}")
        click.echo(f"  New:  {_display(new, secret)}")
        return

    _set_toml_value(doc, section, key, new)
    _save_toml_doc(config_path, doc)

    if old is not None:
        emit_success(
            ctx,
            payload={
                "key": toml_key,
                "old_value": _mask_value(old, secret),
                "new_value": _mask_value(new, secret),
            },
            text=f"Updated {toml_key} in {config_path}",
        )
    else:
        emit_success(
            ctx,
            payload={"key": toml_key, "new_value": _mask_value(new, secret)},
            text=f"Set {toml_key} in {config_path}",
        )


# -- Explicit mode (key + value provided) -----------------------------------


def _run_explicit(ctx, key, value, use_global, use_project, dry_run):
    try:
        section, opt_name = _parse_toml_key(key)
        toml_key = f"{section}.{opt_name}" if section else opt_name
        option, is_remote_env = _parse_user_managed_key(toml_key)

        if is_remote_env:
            parsed = value
        elif option:
            parsed, valid, err = _validate_value(option, value)
            if not valid:
                emit_warning(
                    ctx,
                    f"Validation warning: {err}\nValue will be written but may cause issues.",
                )

        config_path = _resolve_config_path(use_global, use_project)
        doc = _load_toml_doc(config_path) if config_path.exists() else tomlkit.document()
        old = _get_current_value(doc, section, opt_name)

        _apply_change(ctx, doc, config_path, section, opt_name, toml_key, old, parsed, dry_run)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


# -- Semi-interactive (key given, value prompted) ---------------------------


def _run_semi_interactive(ctx, key, use_global, use_project, dry_run):
    try:
        section, opt_name = _parse_toml_key(key)
        toml_key = f"{section}.{opt_name}" if section else opt_name
        option, is_remote_env = _parse_user_managed_key(toml_key)

        config_path = _resolve_config_path(use_global, use_project)
        doc = _load_toml_doc(config_path) if config_path.exists() else tomlkit.document()
        current = _get_current_value(doc, section, opt_name)

        click.echo()
        if not is_remote_env and option:
            click.echo(click.style(f"  {option.description}", dim=True))
        secret = bool(option and option.secret)
        current_display = _display_effective(current, option.default if option else None, secret)
        click.echo(f"  Current: {current_display}")
        click.echo(f"  Config:  {config_path}")
        click.echo()

        default_str = _prompt_default_str(current, option.default if option else None, secret)
        new_value = text(
            f"  New value for {toml_key}:",
            default=default_str,
            is_password=secret,
            erase_when_done=True,
        ).ask()
        if new_value is None:
            return

        if is_remote_env:
            parsed = new_value
        elif option:
            parsed, valid, err = _validate_value(option, new_value)
            if not valid and err:
                if not confirm(
                    f"  Validation warning: {err}\n  Proceed anyway?",
                    default=False,
                    erase_when_done=True,
                ).ask():
                    return
        else:
            parsed = new_value

        _apply_change(
            ctx,
            doc,
            config_path,
            section,
            opt_name,
            toml_key,
            current,
            parsed,
            dry_run,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


# -- Full interactive --------------------------------------------------------

_LEVEL_SCOPE = "scope"
_LEVEL_CATEGORY = "category"
_LEVEL_KEY = "key"
_LEVEL_VALUE = "value"
_LEVEL_RENV_NAME = "renv_name"
_LEVEL_RENV_VALUE = "renv_value"


def _run_interactive(ctx, use_global, use_project, dry_run):
    try:
        global_path = Config.resolve_global_config_path()
        project_path = _find_project_config()
        scope_locked = bool(use_global or use_project)

        if use_global or use_project:
            scope = "global" if use_global else "project"
        else:
            scope = None

        user_opts = get_user_managed_options()
        cat_map: dict[str, list[ConfigOption]] = {}
        for o in user_opts:
            cat_map.setdefault(o.category, []).append(o)
        ordered_cats = [c for c in get_categories() if c in cat_map] + [_REMOTE_ENV_CATEGORY]

        category: str | None = None
        chosen: ConfigOption | None = None
        env_name: str | None = None

        level = _LEVEL_SCOPE
        cursor_positions = {
            _LEVEL_SCOPE: 0,
            _LEVEL_CATEGORY: 0,
        }
        key_cursor_positions: dict[str, int] = {}
        renv_cursor = 0

        while True:
            if level == _LEVEL_SCOPE:
                if scope is not None:
                    level = _LEVEL_CATEGORY
                    continue

                entries = []
                if project_path:
                    entries.append(f"Project  ({project_path})")
                entries.append(f"Global   ({global_path})")

                if len(entries) == 1:
                    scope = "project" if project_path else "global"
                    level = _LEVEL_CATEGORY
                    continue

                idx = _pick_menu(
                    "Which config file?",
                    entries,
                    cursor_positions.get(_LEVEL_SCOPE, 0),
                )
                if idx == _MENU_CANCEL_SENTINEL:
                    return
                cursor_positions[_LEVEL_SCOPE] = idx
                scope = "project" if idx == 0 and project_path else "global"
                level = _LEVEL_CATEGORY

            elif level == _LEVEL_CATEGORY:
                idx = _pick_menu(
                    "Select a category:",
                    ordered_cats,
                    cursor_positions.get(_LEVEL_CATEGORY, 0),
                    allow_back=not scope_locked,
                )
                if idx == _MENU_CANCEL_SENTINEL:
                    return
                if idx == _MENU_BACK_SENTINEL:
                    scope = None
                    level = _LEVEL_SCOPE
                    continue
                cursor_positions[_LEVEL_CATEGORY] = idx
                category = ordered_cats[idx]
                level = _LEVEL_KEY

            elif level == _LEVEL_KEY:
                if category == _REMOTE_ENV_CATEGORY:
                    level = _LEVEL_RENV_NAME
                    continue

                options = cat_map.get(category, [])
                if not options:
                    level = _LEVEL_CATEGORY
                    continue

                config_path = _resolve_config_path(scope == "global", scope == "project")
                doc = _load_toml_doc(config_path) if config_path.exists() else tomlkit.document()
                items = []
                for o in options:
                    sec, name = _parse_toml_key(o.toml_key)
                    cur = _get_current_value(doc, sec, name)
                    items.append(f"{o.toml_key}  ({_display_effective(cur, o.default, o.secret)})")

                idx = _pick_menu(
                    "Select a setting:",
                    items,
                    key_cursor_positions.get(category, 0),
                    allow_back=True,
                )
                if idx == _MENU_CANCEL_SENTINEL:
                    return
                if idx == _MENU_BACK_SENTINEL:
                    level = _LEVEL_CATEGORY
                    continue
                key_cursor_positions[category] = idx
                chosen = options[idx]
                level = _LEVEL_VALUE

            elif level == _LEVEL_VALUE:
                while True:
                    config_path = _resolve_config_path(scope == "global", scope == "project")
                    doc = (
                        _load_toml_doc(config_path) if config_path.exists() else tomlkit.document()
                    )
                    sec, name = _parse_toml_key(chosen.toml_key)
                    current = _get_current_value(doc, sec, name)

                    click.echo()
                    click.echo(click.style(f"  {chosen.description}", dim=True))
                    current_display = _display_effective(current, chosen.default, chosen.secret)
                    click.echo(f"  Current: {current_display}")
                    click.echo()

                    default_str = _prompt_default_str(current, chosen.default, chosen.secret)
                    new_val = text(
                        f"  New value for {chosen.toml_key}:",
                        default=default_str,
                        is_password=chosen.secret,
                        erase_when_done=True,
                    ).ask()

                    if new_val is None:
                        chosen = None
                        level = _LEVEL_KEY
                        break

                    parsed, valid, err = _validate_value(chosen, new_val)
                    if not valid and err:
                        proceed = confirm(
                            f"  Validation warning: {err}\n  Proceed anyway?",
                            default=False,
                            erase_when_done=True,
                        ).ask()
                        if proceed is not True:
                            continue

                    _apply_change(
                        ctx,
                        doc,
                        config_path,
                        sec,
                        name,
                        chosen.toml_key,
                        current,
                        parsed,
                        dry_run,
                    )
                    return

                continue

            elif level == _LEVEL_RENV_NAME:
                config_path = _resolve_config_path(scope == "global", scope == "project")
                doc = _load_toml_doc(config_path) if config_path.exists() else tomlkit.document()

                renv_keys = list((doc.get("remote_env") or {}).keys())
                items = []
                for k in renv_keys:
                    cur = _get_current_value(doc, "remote_env", k)
                    items.append(f"{k}  ({_display(cur)})")
                items.append("+ New variable")

                idx = _pick_menu(
                    "Select a remote env variable:",
                    items,
                    renv_cursor,
                    allow_back=True,
                )
                if idx == _MENU_CANCEL_SENTINEL:
                    return
                if idx == _MENU_BACK_SENTINEL:
                    level = _LEVEL_CATEGORY
                    continue
                renv_cursor = idx
                if idx == len(items) - 1:
                    env_name = text(
                        "  Variable name (e.g., PIP_INDEX_URL):",
                        erase_when_done=True,
                    ).ask()
                    if not env_name:
                        level = _LEVEL_RENV_NAME
                        continue
                    env_name = env_name.strip()
                    if not env_name:
                        level = _LEVEL_RENV_NAME
                        continue
                else:
                    env_name = renv_keys[idx]
                level = _LEVEL_RENV_VALUE

            elif level == _LEVEL_RENV_VALUE:
                while True:
                    config_path = _resolve_config_path(scope == "global", scope == "project")
                    doc = (
                        _load_toml_doc(config_path) if config_path.exists() else tomlkit.document()
                    )
                    current = _get_current_value(doc, "remote_env", env_name)
                    click.echo(f"  Current: {_display(current)}")
                    click.echo()

                    env_value = text(
                        f"  Value for {env_name}:",
                        default=str(current) if current is not None else "",
                        erase_when_done=True,
                    ).ask()
                    if env_value is None:
                        env_name = None
                        level = _LEVEL_RENV_NAME
                        break

                    _apply_change(
                        ctx,
                        doc,
                        config_path,
                        "remote_env",
                        env_name,
                        f"remote_env.{env_name}",
                        current,
                        env_value,
                        dry_run,
                    )
                    return

                continue

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


# -- Command ----------------------------------------------------------------


@click.command("set")
@click.argument("key", required=False, shell_complete=_complete_config_key)
@click.argument("value", required=False, default=None)
@click.option(
    "--global",
    "-g",
    "use_global",
    is_flag=True,
    help="Set in global config (~/.config/inspire/config.toml)",
)
@click.option(
    "--project",
    "-p",
    "use_project",
    is_flag=True,
    help="Set in project config (./.inspire/config.toml)",
)
@click.option("--dry-run", is_flag=True, help="Preview changes without writing")
@pass_context
def set_config(ctx, key, value, use_global, use_project, dry_run):
    """Set a configuration value.

    Without arguments, launches an interactive picker grouped by category.
    Navigate with arrow keys. Use Left to go back and Right or Enter to go deeper.
    With a KEY only, prompts for the value interactively.
    With both KEY and VALUE, sets directly (useful for scripting).

    \b
    Examples:
        inspire config set                              # Interactive mode
        inspire config set defaults.target_dir          # Prompt for value
        inspire config set defaults.target_dir /path    # Explicit mode
        inspire config set --global auth.username user
        inspire config set defaults.priority 6
        inspire config set --dry-run defaults.resource 4xH200
    """
    if key is not None and value is not None:
        _run_explicit(ctx, key, value, use_global, use_project, dry_run)
    elif key is not None:
        _run_semi_interactive(ctx, key, use_global, use_project, dry_run)
    else:
        _run_interactive(ctx, use_global, use_project, dry_run)
