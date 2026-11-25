"""Shared CLI surface for kind-specific Workload Profile CRUD."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import click
import tomlkit

from inspire.cli.context import Context, pass_context
from inspire.cli.utils.confirm import confirmed_or_error
from inspire.cli.formatters import json_formatter
from inspire.config import PROJECT_CONFIG_DIR, Config, ConfigError
from inspire.config.toml import _find_project_config
from inspire.config.toml_write import atomic_write_toml, load_toml_document
from inspire.config.workload_profiles import merge_profiles, validate_profile


def _profile_config_path(scope: str) -> Path:
    if scope == "global":
        return Config.resolve_global_config_path()
    return _find_project_config() or Path.cwd() / PROJECT_CONFIG_DIR / "config.toml"


def _profiles_from_path(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    document = load_toml_document(path)
    profiles = document.get("profiles", {})
    if not isinstance(profiles, Mapping):
        raise ConfigError(f"{path}: [profiles] must be a table")
    return profiles


def _effective_profiles() -> tuple[dict[str, Any], dict[str, Any]]:
    global_profiles = _profiles_from_path(Config.resolve_global_config_path())
    project_path = _find_project_config()
    project_profiles = _profiles_from_path(project_path) if project_path else {}
    return merge_profiles(global_profiles, project_profiles)


def _parse_profile_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read profile file {path}") from exc
    try:
        if path.suffix.lower() == ".json":
            raw = json.loads(text)
        else:
            document = tomlkit.parse(text)
            raw = document.get("profile", document)
    except (json.JSONDecodeError, tomlkit.exceptions.ParseError) as exc:
        raise ConfigError(f"Invalid profile file {path}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError("Profile file must contain an object")
    return dict(raw)


def _parse_values(values: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in values:
        if "=" not in item:
            raise ConfigError(f"Invalid --value {item!r}; expected KEY=VALUE")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError("Profile field name cannot be empty")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        result[key] = value
    return result


def _profile_table(document: tomlkit.TOMLDocument, kind: str, *, create: bool) -> Any:
    profiles = document.get("profiles")
    if profiles is None and create:
        profiles = tomlkit.table()
        document["profiles"] = profiles
    if profiles is None:
        return None
    if not isinstance(profiles, dict):
        raise ConfigError("[profiles] must be a table")
    kind_table = profiles.get(kind)
    if kind_table is None and create:
        kind_table = tomlkit.table()
        profiles[kind] = kind_table
    if kind_table is not None and not isinstance(kind_table, dict):
        raise ConfigError(f"[profiles.{kind}] must be a table")
    return kind_table


def build_profile_group(kind: str) -> click.Group:
    """Build the `profile` subgroup for one platform Workload kind."""

    @click.group("profile")
    def profile_group() -> None:
        """Manage portable, named workload conditions."""

    @profile_group.command("list")
    @pass_context
    def list_profiles(ctx: Context) -> None:
        """List effective profiles and their winning config scope."""
        try:
            profiles, sources = _effective_profiles()
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        kind_profiles = profiles.get(kind, {})
        kind_sources = sources.get(kind, {})
        items = [
            {"name": name, "source": kind_sources.get(name), "profile": kind_profiles[name]}
            for name in sorted(kind_profiles)
        ]
        payload = {"workload": kind, "profiles": items}
        if ctx.json_output:
            click.echo(json_formatter.format_json(payload))
            return
        if not items:
            click.echo(f"No {kind} profiles configured.")
            return
        for item in items:
            click.echo(f"{item['name']}\t{item['source']}")

    @profile_group.command("show")
    @click.argument("name")
    @pass_context
    def show_profile(ctx: Context, name: str) -> None:
        """Show one effective profile with its winning scope."""
        try:
            profiles, sources = _effective_profiles()
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        kind_profiles = profiles.get(kind, {})
        if name not in kind_profiles:
            raise click.ClickException(f"Unknown {kind} profile {name!r}")
        payload = {
            "workload": kind,
            "name": name,
            "source": sources.get(kind, {}).get(name),
            "profile": kind_profiles[name],
        }
        if ctx.json_output:
            click.echo(json_formatter.format_json(payload))
        else:
            click.echo(tomlkit.dumps(tomlkit.document().append("profile", payload["profile"])))

    @profile_group.command("set")
    @click.argument("name")
    @click.option("--scope", type=click.Choice(["global", "project"]), required=True)
    @click.option("--file", "profile_file", type=click.Path(path_type=Path, dir_okay=False))
    @click.option("--value", "values", multiple=True, metavar="KEY=VALUE")
    @pass_context
    def set_profile(
        ctx: Context,
        name: str,
        scope: str,
        profile_file: Path | None,
        values: tuple[str, ...],
    ) -> None:
        """Create or replace one profile in an explicit config scope."""
        if profile_file is None and not values:
            raise click.UsageError("Pass --file or at least one --value KEY=VALUE")
        try:
            raw: dict[str, Any] = {}
            if profile_file is not None:
                raw.update(_parse_profile_file(profile_file))
            raw.update(_parse_values(values))
            validated = validate_profile(kind, raw)

            path = _profile_config_path(scope)
            document = load_toml_document(path)
            table = _profile_table(document, kind, create=True)
            table[name] = validated
            atomic_write_toml(path, document)
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        payload = {
            "workload": kind,
            "name": name,
            "scope": scope,
            "config_path": str(path),
            "profile": validated,
        }
        if ctx.json_output:
            click.echo(json_formatter.format_json(payload))
        else:
            click.echo(f"Set {kind} profile {name!r} in {scope} config.")

    @profile_group.command("delete")
    @click.argument("name")
    @click.option("--scope", type=click.Choice(["global", "project"]), required=True)
    @click.option(
        "--yes",
        "--force",
        "yes",
        is_flag=True,
        help="Delete without an interactive confirmation (alias: --force)",
    )
    @pass_context
    def delete_profile(ctx: Context, name: str, scope: str, yes: bool) -> None:
        """Delete one profile from an explicit config scope."""
        subject = f"delete the {kind} profile {name!r} from {scope} config"
        if not confirmed_or_error(ctx, assume_yes=yes, subject=subject):
            raise click.Abort()

        path = _profile_config_path(scope)
        document = load_toml_document(path)
        try:
            table = _profile_table(document, kind, create=False)
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        if table is None or name not in table:
            raise click.ClickException(f"No {kind} profile {name!r} in {scope} config")
        del table[name]
        atomic_write_toml(path, document)
        payload = {
            "workload": kind,
            "name": name,
            "scope": scope,
            "deleted": True,
            "config_path": str(path),
        }
        if ctx.json_output:
            click.echo(json_formatter.format_json(payload))
        else:
            click.echo(f"Deleted {kind} profile {name!r} from {scope} config.")

    return profile_group


__all__ = ["build_profile_group"]
