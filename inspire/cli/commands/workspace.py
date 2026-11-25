"""Live workspace inventory and user-declared compute-pool attributes."""

from __future__ import annotations

from typing import Any

import click
import tomlkit

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error
from inspire.config import Config, ConfigError
from inspire.config.toml_write import atomic_write_text
from inspire.platform.web.browser_api.availability import list_compute_groups, list_compute_pools
from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces
from inspire.platform.web.session import get_web_session


def _internet_text(value: bool | None) -> str:
    if value is None:
        return "undeclared"
    return "on" if value else "off"


def _load_config() -> Config:
    config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
    return config


def _pool_rows(workspaces: list[dict[str, Any]], session: Any) -> dict[str, list[dict[str, Any]]]:
    rows_by_workspace: dict[str, list[dict[str, Any]]] = {}
    for workspace in workspaces:
        workspace_id = str(workspace.get("id") or "")
        pools = {
            str(pool.get("compute_group_id") or "").strip(): pool
            for pool in list_compute_pools(workspace_id=workspace_id, session=session)
            if str(pool.get("compute_group_id") or "").strip()
        }
        rows: list[dict[str, Any]] = []
        for group in list_compute_groups(workspace_id=workspace_id, session=session):
            pool_id = str(group.get("compute_group_id") or "").strip()
            pool = pools.get(pool_id, {})
            logical_group_id = str(group.get("logic_compute_group_id") or "").strip()
            rows.append(
                {
                    "compute_group_id": pool_id,
                    "pool_name": str(pool.get("name") or ""),
                    "logic_compute_group_id": logical_group_id,
                    "name": str(group.get("name") or group.get("compute_group_name") or ""),
                    "cluster_id": str(pool.get("cluster_id") or ""),
                    "cluster_name": str(pool.get("cluster_name") or ""),
                }
            )
        rows_by_workspace[workspace_id] = rows
    return rows_by_workspace


def _resolve_pool_id(reference: str, session: Any, *, base_url: str = "") -> str:
    value = reference.strip()
    workspaces = try_enumerate_workspaces(session, base_url=base_url)
    matches: set[str] = set()
    for rows in _pool_rows(workspaces, session).values():
        for row in rows:
            identifiers = {row["compute_group_id"], row["logic_compute_group_id"]}
            names = {row["name"], row["pool_name"]}
            if value in identifiers or value in names:
                if row["compute_group_id"]:
                    matches.add(row["compute_group_id"])
    if not matches:
        raise ConfigError(f"No compute pool or logical group named {value!r} is visible")
    if len(matches) > 1:
        raise ConfigError(
            f"Compute pool reference {value!r} is ambiguous; use one of: {', '.join(sorted(matches))}"
        )
    return next(iter(matches))


def _pool_items(workspaces: list[dict[str, Any]], config: Config, session: Any) -> None:
    for workspace in workspaces:
        workspace_id = str(workspace.get("id") or "")
        rows = _pool_rows([workspace], session).get(workspace_id, [])
        for row in rows:
            row["internet"] = (config.pool_internet or {}).get(row["compute_group_id"])
        workspace["compute_groups"] = rows


def _add_workspace_aliases(workspaces: list[dict[str, Any]], config: Config) -> None:
    selectors = config.workspace_selectors or {}
    for workspace in workspaces:
        workspace_name = str(workspace.get("name") or "").strip()
        workspace["aliases"] = sorted(
            alias
            for alias, selector in selectors.items()
            if str((selector or {}).get("name") or "").strip() == workspace_name
        )


@click.group()
def workspace() -> None:
    """Inspect workspaces and declare compute-pool attributes."""


@workspace.command("list")
@click.option(
    "--pools",
    is_flag=True,
    help="Per logical group, show its parent compute pool, raw cluster name, and declaration",
)
@pass_context
def list_workspaces(ctx: Context, pools: bool) -> None:
    """List workspaces and their configured aliases; optionally include compute-pool evidence."""
    try:
        config = _load_config()
        session = get_web_session(require_workspace=False, account=config.account_alias)
        items = try_enumerate_workspaces(session, base_url=config.base_url)
        _add_workspace_aliases(items, config)
        if pools:
            _pool_items(items, config, session)
        if ctx.json_output:
            click.echo(json_formatter.format_json({"items": items, "total": len(items)}))
            return
        click.echo(f"Workspaces: {len(items)}")
        for item in items:
            aliases = ",".join(item.get("aliases") or []) or "-"
            click.echo(f"{item.get('name') or '(unnamed)'}\t{item.get('id')}\taliases={aliases}")
            if pools:
                for group in item.get("compute_groups") or []:
                    internet = group.get("internet")
                    click.echo(
                        f"  {group.get('name') or '(unnamed)'}"
                        f"\t{group.get('logic_compute_group_id')}"
                        f"\tpool={group.get('compute_group_id') or '-'}"
                        f"\tpool_name={group.get('pool_name') or '-'}"
                        f"\tcluster_name={group.get('cluster_name') or '-'}"
                        f"\tinternet={_internet_text(internet)}"
                    )
    except ConfigError as exc:
        exit_with_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        exit_with_error(ctx, "APIError", str(exc), EXIT_API_ERROR)


@workspace.group("pool")
def pool() -> None:
    """Manage attributes shared by every logical group in one compute pool."""


@pool.command("set")
@click.argument("pool_reference", metavar="POOL_OR_LOGICAL_GROUP_ID_OR_NAME")
@click.option(
    "--internet",
    type=click.Choice(["on", "off"]),
    required=True,
    help="Declare whether this compute pool has internet reachability",
)
@click.option("--dry-run", is_flag=True, help="Show the declaration without writing config")
@pass_context
def set_pool(ctx: Context, pool_reference: str, internet: str, dry_run: bool) -> None:
    """Declare internet reachability on a compute pool (cg-), resolved from cg-/lcg-/name."""
    try:
        config = _load_config()
        account = str(config.account_alias or config.username or "").strip()
        if not account:
            raise ConfigError("No account selected; pass --account or configure [context].account")
        session = get_web_session(require_workspace=False, account=config.account_alias)
        pool_id = _resolve_pool_id(pool_reference, session, base_url=config.base_url)
        declared = internet == "on"
        config_path = Config.resolve_global_config_path()
        document = (
            tomlkit.parse(config_path.read_text(encoding="utf-8"))
            if config_path.exists()
            else tomlkit.document()
        )
        accounts = document.setdefault("accounts", tomlkit.table())
        account_table = accounts.setdefault(account, tomlkit.table())
        pools = account_table.setdefault("pools", tomlkit.table())
        pool_table = pools.setdefault(pool_id, tomlkit.table())
        payload: dict[str, Any] = {
            "account": account,
            "pool_id": pool_id,
            "internet": declared,
            "internet_text": internet,
            "config_path": str(config_path),
            "dry_run": dry_run,
        }
        if dry_run:
            if ctx.json_output:
                click.echo(json_formatter.format_json(payload))
            else:
                click.echo(f"Would declare pool {pool_id} internet={internet} in {config_path}")
            return

        pool_table["internet"] = declared
        atomic_write_text(config_path, tomlkit.dumps(document))
        if ctx.json_output:
            click.echo(json_formatter.format_json(payload))
        else:
            click.echo(f"Declared pool {pool_id} internet={internet} in {config_path}")
    except ConfigError as exc:
        exit_with_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        exit_with_error(ctx, "APIError", str(exc), EXIT_API_ERROR)


__all__ = ["workspace"]
