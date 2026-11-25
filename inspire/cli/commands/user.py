"""Current-user identity and access diagnostics."""

from __future__ import annotations

from typing import Any, Callable

import click

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.launch_preview import redact_structure
from inspire.cli.utils.redaction import redact_named_fields
from inspire.config import ConfigError
from inspire.platform.web import browser_api


def _emit(ctx: Context, payload: dict[str, Any], render: Callable[[], None]) -> None:
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
    else:
        render()


def _run(ctx: Context, operation: Callable[[], None]) -> None:
    try:
        operation()
    except ConfigError as exc:
        exit_with_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
    except Exception as exc:  # noqa: BLE001
        exit_with_error(ctx, "APIError", str(exc), EXIT_API_ERROR)


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("items", "list", "api_keys", "ssh_keys"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


@click.group("user")
def user() -> None:
    """Inspect the selected platform identity and access."""


@user.command("whoami")
@pass_context
def whoami(ctx: Context) -> None:
    """Show the current authenticated platform user."""

    def operation() -> None:
        detail = redact_structure(browser_api.get_current_user())
        payload = {"user": detail}

        def render() -> None:
            click.echo(f"ID:       {detail.get('id') or detail.get('user_id') or '-'}")
            click.echo(f"Username: {detail.get('username') or detail.get('name') or '-'}")
            click.echo(f"Email:    {detail.get('email') or '-'}")

        _emit(ctx, payload, render)

    _run(ctx, operation)


@user.command("permissions")
@click.option("--workspace-id", help="Show permissions scoped to one workspace")
@pass_context
def permissions(ctx: Context, workspace_id: str | None) -> None:
    """Show global or workspace-specific platform permissions."""

    def operation() -> None:
        data = browser_api.get_my_permissions(workspace_id=workspace_id)
        payload = {"workspace_id": workspace_id, "permissions": data}

        def render() -> None:
            click.echo(json_formatter.format_json(data))

        _emit(ctx, payload, render)

    _run(ctx, operation)


@user.command("api-keys")
@pass_context
def api_keys(ctx: Context) -> None:
    """List API key metadata with all secret-like fields redacted."""

    def operation() -> None:
        # `redact_structure` guards unknown schemas by substring and so misses the
        # API-key surface's `value` field; redact that one by name on top of it.
        data = redact_named_fields(redact_structure(browser_api.list_user_api_keys()))
        payload = {"api_keys": data}
        items = _items(data)

        def render() -> None:
            if not items:
                click.echo("No API keys found.")
                return
            for item in items:
                click.echo(
                    f"{item.get('id') or item.get('api_key_id') or '-'}\t"
                    f"{item.get('name') or '-'}\t{item.get('status') or '-'}"
                )

        _emit(ctx, payload, render)

    _run(ctx, operation)


@user.command("ssh-keys")
@click.option("--limit", type=click.IntRange(1, 500), default=100, show_default=True)
@pass_context
def ssh_keys(ctx: Context, limit: int) -> None:
    """List SSH public-key names, fingerprints, and timestamps."""

    def operation() -> None:
        data = redact_structure(browser_api.list_ssh_public_keys(page_size=limit))
        payload = {"ssh_keys": data}
        items = _items(data)

        def render() -> None:
            if not items:
                click.echo("No SSH public keys found.")
                return
            for item in items:
                click.echo(
                    f"{item.get('id') or '-'}\t{item.get('name') or '-'}\t"
                    f"{item.get('fingerprint') or '-'}\t"
                    f"{item.get('created_at') or item.get('create_time') or '-'}"
                )

        _emit(ctx, payload, render)

    _run(ctx, operation)


__all__ = ["user"]
