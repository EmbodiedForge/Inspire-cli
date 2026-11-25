"""Account Catalog editing commands."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

import click
import tomlkit

from inspire.cli.context import Context, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.confirm import confirmed_or_error
from inspire.config import (
    Config,
    ConfigError,
    clear_account_discovery_state,
    get_account_discovery_state_path,
    load_account_discovery_state,
)
from inspire.config.toml_write import atomic_write_toml, load_toml_document


def _accounts_table(document: tomlkit.TOMLDocument, *, create: bool) -> Any:
    accounts = document.get("accounts")
    if accounts is None and create:
        accounts = tomlkit.table()
        document["accounts"] = accounts
    if accounts is not None and not isinstance(accounts, dict):
        raise ConfigError("Global config [accounts] must be a table")
    return accounts


def _catalog_entries() -> tuple[str, list[dict[str, Any]]]:
    path = Config.resolve_global_config_path()
    document = load_toml_document(path)
    accounts = _accounts_table(document, create=False) or {}
    entries: list[dict[str, Any]] = []
    for alias in sorted(str(key) for key in accounts):
        raw = accounts[alias]
        if not isinstance(raw, dict):
            continue
        if raw.get("password_command"):
            credential = "password_command"
        elif raw.get("password"):
            credential = "configured_password"
        else:
            credential = "environment_only"
        entries.append(
            {
                "alias": alias,
                "username": str(raw.get("username") or "") or None,
                "base_url": str(raw.get("base_url") or "")
                or str((raw.get("api") or {}).get("base_url") or "")
                or None,
                "auth_method": str(raw.get("auth_method") or "http"),
                "credential": credential,
            }
        )
    return str(path), entries


def _account_entry(alias: str) -> tuple[str, Mapping[str, Any]]:
    path = Config.resolve_global_config_path()
    document = load_toml_document(path)
    accounts = _accounts_table(document, create=False) or {}
    raw = accounts.get(alias)
    if not isinstance(raw, Mapping):
        raise click.ClickException(f"Account alias {alias!r} is not registered")
    return str(path), raw


def _credential_kind(raw: Mapping[str, Any]) -> str:
    if raw.get("password_command"):
        return "password_command"
    if raw.get("password"):
        return "configured_password"
    return "environment_only"


def _state_summary(alias: str) -> dict[str, Any]:
    state = load_account_discovery_state(alias)
    path = get_account_discovery_state_path(alias)
    if state is None:
        return {"path": str(path), "present": False, "status": "missing"}
    failed = [
        {"scope": scope, "error": observation.error, "stale": observation.stale}
        for scope, observation in [
            ("workspaces", state.workspaces),
            *((f"projects:{key}", value) for key, value in state.projects.items()),
            *((f"compute_groups:{key}", value) for key, value in state.compute_groups.items()),
            *((f"workspace_specs:{key}", value) for key, value in state.workspace_specs.items()),
        ]
        if observation.status == "failed"
    ]
    return {
        "path": str(path),
        "present": True,
        "status": state.status,
        "updated_at": state.updated_at,
        "workspace_count": len(state.workspaces.items),
        "project_count": len(state.project_catalog()),
        "compute_group_count": len(state.flattened_compute_groups()),
        "failed_scopes": failed,
    }


@click.group("account")
def account() -> None:
    """Edit the global Account Catalog."""


@account.command("list")
@pass_context
def list_accounts(ctx: Context) -> None:
    """List configured aliases without revealing credentials."""
    path, entries = _catalog_entries()
    payload = {"config_path": path, "accounts": entries}
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
        return
    if not entries:
        click.echo("No accounts registered.")
        return
    for entry in entries:
        click.echo(
            f"{entry['alias']}\t{entry['username'] or '-'}\t"
            f"{entry['credential']}\t{entry['base_url'] or '-'}"
        )


@account.command("show")
@click.argument("alias")
@pass_context
def show_account(ctx: Context, alias: str) -> None:
    """Show one account's non-secret intent and discovery state summary."""
    normalized_alias = alias.strip()
    config_path, raw = _account_entry(normalized_alias)
    payload: dict[str, Any] = {
        "alias": normalized_alias,
        "username": str(raw.get("username") or "") or None,
        "base_url": str(raw.get("base_url") or "")
        or str((raw.get("api") or {}).get("base_url") or "")
        or None,
        "auth_method": str(raw.get("auth_method") or "http"),
        "credential": _credential_kind(raw),
        "workspaces": dict(raw.get("workspaces") or {}),
        "projects": dict(raw.get("projects") or {}),
        "state": _state_summary(normalized_alias),
        "config_path": config_path,
    }
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(f"Account: {payload['alias']}")
    click.echo(f"Username: {payload['username'] or '-'}")
    click.echo(f"Base URL: {payload['base_url'] or '-'}")
    click.echo(f"Credential: {payload['credential']}")
    click.echo(f"Discovery: {payload['state']['status']} ({payload['state']['path']})")
    click.echo(f"Workspace selectors: {len(payload['workspaces'])}")
    click.echo(f"Project selectors: {len(payload['projects'])}")


@account.command("export")
@click.argument("alias")
@pass_context
def export_account(ctx: Context, alias: str) -> None:
    """Export one account record without passwords or credential commands."""
    normalized_alias = alias.strip()
    _config_path, raw = _account_entry(normalized_alias)
    safe = deepcopy(dict(raw))
    for key in ("password", "password_command"):
        safe.pop(key, None)
    payload = {"accounts": {normalized_alias: safe}}
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
    else:
        click.echo(tomlkit.dumps(payload).rstrip())


@account.command("doctor")
@click.argument("alias")
@click.option("--live", is_flag=True, help="Resolve selectors against the live platform")
@pass_context
def doctor_account(ctx: Context, alias: str, live: bool) -> None:
    """Diagnose account identity, discovery state, and optional live selectors."""
    normalized_alias = alias.strip()
    config_path, raw = _account_entry(normalized_alias)
    issues: list[str] = []
    username = str(raw.get("username") or "").strip()
    if not username:
        issues.append("username is missing")
    state = load_account_discovery_state(normalized_alias)
    if state is None:
        issues.append("discovery state is missing")
    elif state.status != "complete":
        issues.append(f"discovery state status is {state.status}")

    live_summary: dict[str, Any] | None = None
    if live:
        from inspire.platform.web import browser_api
        from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces
        from inspire.platform.web.session import get_web_session

        session = get_web_session(account=normalized_alias)
        workspaces = try_enumerate_workspaces(
            session,
            workspace_id=str(session.workspace_id or ""),
            base_url=str(session.base_url or ""),
        )
        by_name: dict[str, list[str]] = {}
        for item in workspaces:
            if not isinstance(item, Mapping):
                continue
            by_name.setdefault(str(item.get("name") or ""), []).append(str(item.get("id") or ""))
        workspace_ids: dict[str, str] = {}
        for selector_alias, selector in dict(raw.get("workspaces") or {}).items():
            name = str((selector or {}).get("name") or "") if isinstance(selector, Mapping) else ""
            matches = [value for value in by_name.get(name, []) if value]
            if len(matches) != 1:
                issues.append(
                    f"workspace {selector_alias!r} name {name!r} resolved to {len(matches)} matches"
                )
            else:
                workspace_ids[str(selector_alias)] = matches[0]

        project_checks = 0
        for project_alias, selector in dict(raw.get("projects") or {}).items():
            if not isinstance(selector, Mapping):
                issues.append(f"project {project_alias!r} selector is not a table")
                continue
            workspace_alias = str(selector.get("workspace") or "")
            project_name = str(selector.get("name") or "")
            workspace_id = workspace_ids.get(workspace_alias)
            if not workspace_id:
                issues.append(
                    f"project {project_alias!r} references unresolved workspace {workspace_alias!r}"
                )
                continue
            projects = browser_api.list_projects(workspace_id=workspace_id, session=session)
            matching_projects = [
                item for item in projects if str(getattr(item, "name", "")) == project_name
            ]
            project_checks += 1
            if len(matching_projects) != 1:
                issues.append(
                    f"project {project_alias!r} name {project_name!r} resolved to {len(matching_projects)} matches"
                )
        live_summary = {
            "workspace_count": len(workspaces),
            "resolved_workspace_selectors": len(workspace_ids),
            "project_selectors_checked": project_checks,
        }

    payload = {
        "alias": normalized_alias,
        "ok": not issues,
        "issues": issues,
        "config_path": config_path,
        "state": _state_summary(normalized_alias),
        "live": live_summary,
    }
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
    else:
        click.echo(f"Account {normalized_alias}: {'OK' if not issues else 'FAILED'}")
        for issue in issues:
            click.echo(f"  - {issue}")
        if live_summary:
            click.echo(
                "Live: "
                f"{live_summary['workspace_count']} workspaces, "
                f"{live_summary['resolved_workspace_selectors']} workspace selectors, "
                f"{live_summary['project_selectors_checked']} project selectors"
            )
    if issues:
        raise click.ClickException(f"Account {normalized_alias!r} has {len(issues)} issue(s)")


@account.command("register")
@click.argument("alias")
@click.option("--username", required=True, help="Platform login username")
@click.option("--base-url", help="Platform base URL for this account")
@click.option("--auth-method", type=click.Choice(["http", "playwright"]), default="http")
@click.option("--password-command", help="Command whose stdout supplies the password")
@click.option("--force", is_flag=True, help="Update an existing account entry")
@pass_context
def register_account(
    ctx: Context,
    alias: str,
    username: str,
    base_url: str | None,
    auth_method: str,
    password_command: str | None,
    force: bool,
) -> None:
    """Register one local alias and its explicit platform username."""
    normalized_alias = alias.strip()
    normalized_username = username.strip()
    if not normalized_alias:
        raise click.UsageError("ALIAS cannot be empty")
    if not normalized_username:
        raise click.UsageError("--username cannot be empty")

    path = Config.resolve_global_config_path()
    document = load_toml_document(path)
    accounts = _accounts_table(document, create=True)
    if normalized_alias in accounts and not force:
        raise click.ClickException(
            f"Account alias {normalized_alias!r} already exists; pass --force to update it"
        )
    entry = accounts.get(normalized_alias)
    if not isinstance(entry, dict):
        entry = tomlkit.table()
        accounts[normalized_alias] = entry
    entry["username"] = normalized_username
    entry["auth_method"] = auth_method
    if base_url is not None:
        entry["base_url"] = base_url.rstrip("/")
    if password_command is not None:
        command = password_command.strip()
        if not command:
            raise click.UsageError("--password-command cannot be empty")
        entry["password_command"] = command
    atomic_write_toml(path, document)

    payload = {
        "alias": normalized_alias,
        "username": normalized_username,
        "base_url": str(entry.get("base_url") or "") or None,
        "auth_method": auth_method,
        "credential": _credential_kind(entry),
        "config_path": str(path),
    }
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
    else:
        click.echo(f"Registered account {normalized_alias!r} for username {normalized_username!r}.")


@account.command("remove")
@click.argument("alias")
@click.option("--yes", is_flag=True, help="Remove without confirmation")
@pass_context
def remove_account(ctx: Context, alias: str, yes: bool) -> None:
    """Remove one alias and its account-scoped catalog state."""
    normalized_alias = alias.strip()
    path = Config.resolve_global_config_path()
    document = load_toml_document(path)
    accounts = _accounts_table(document, create=False)
    if not accounts or normalized_alias not in accounts:
        raise click.ClickException(f"Account alias {normalized_alias!r} is not registered")
    if not confirmed_or_error(
        ctx,
        assume_yes=yes,
        subject=f"remove account alias {normalized_alias!r} and its catalog state",
    ):
        raise click.Abort()
    del accounts[normalized_alias]
    atomic_write_toml(path, document)
    clear_account_discovery_state(normalized_alias)
    from inspire.platform.web.session import clear_account_session_cache

    clear_account_session_cache(normalized_alias)
    payload = {"alias": normalized_alias, "removed": True, "config_path": str(path)}
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
    else:
        click.echo(f"Removed account {normalized_alias!r}.")


__all__ = ["account"]
