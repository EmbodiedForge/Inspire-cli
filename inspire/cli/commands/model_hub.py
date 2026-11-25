"""Model Hub repository commands."""

from __future__ import annotations

import json
from typing import Any

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.logging_setup import redact_text
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import require_web_session, resolve_json_output
from inspire.platform.web.browser_api import model_hub as model_hub_api

_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "set_cookie",
)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _payload_items(payload: dict[str, Any]) -> list[Any]:
    for key in ("list", "items", "models", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _item_model(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    model = item.get("model")
    return model if isinstance(model, dict) else item


def _model_matches(item: Any, *, project_id: str | None, query: str | None) -> bool:
    model = _item_model(item)
    if project_id and str(model.get("project_id") or "") != project_id:
        return False
    needle = (query or "").strip().lower()
    if not needle:
        return True
    fields = (
        model.get("model_id"),
        model.get("name"),
        model.get("model_path"),
        model.get("description"),
        model.get("tags"),
    )
    return any(needle in str(value).lower() for value in fields if value not in (None, ""))


def _model_label(item: Any) -> str:
    model = _item_model(item)
    model_id = redact_text(str(model.get("model_id") or model.get("id") or "-"))
    name = redact_text(str(model.get("name") or "-"))
    version = redact_text(str(model.get("version") or model.get("model_version") or "-"))
    status = redact_text(str(model.get("status") or "-"))
    project_id = redact_text(str(model.get("project_id") or "-"))
    return f"{model_id}  {name}  version={version}  status={status}  project={project_id}"


def _emit_payload(ctx: Context, payload: dict[str, Any], *, text_lines: list[str]) -> None:
    safe_payload = _redact_value(payload)
    if resolve_json_output(ctx, False):
        click.echo(json_formatter.format_json(safe_payload))
        return
    if text_lines:
        for line in text_lines:
            click.echo(redact_text(line))
    else:
        click.echo("No Model Hub models found.")


@click.group("model-hub")
def model_hub() -> None:
    """Inspect Model Hub repository models."""


@model_hub.command("list")
@click.argument("query", required=False, default="")
@click.option("--workspace-id", default=None, help="Workspace ID. Defaults to current session.")
@click.option("--project-id", default=None, help="Client-side project filter.")
@click.option("--page", type=int, default=1, show_default=True)
@click.option("--page-size", type=int, default=20, show_default=True)
@pass_context
def list_model_hub_cmd(
    ctx: Context,
    query: str,
    workspace_id: str | None,
    project_id: str | None,
    page: int,
    page_size: int,
) -> None:
    """List Model Hub repository models."""
    session = require_web_session(ctx, hint="Listing Model Hub models requires web authentication.")
    resolved_workspace_id = workspace_id or getattr(session, "workspace_id", None)
    if not resolved_workspace_id:
        _handle_error(ctx, "ValidationError", "--workspace-id is required", EXIT_VALIDATION_ERROR)
        return
    if page < 1 or page_size == 0 or page_size < -1:
        _handle_error(
            ctx,
            "ValidationError",
            "--page must be >= 1 and --page-size must be -1 or >= 1",
            EXIT_VALIDATION_ERROR,
        )
        return

    try:
        raw_payload = model_hub_api.list_model_hub_models(
            workspace_id=resolved_workspace_id,
            page=page,
            page_size=page_size,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return

    items = [
        item
        for item in _payload_items(raw_payload)
        if _model_matches(item, project_id=project_id, query=query)
    ]
    payload = {
        "workspace_id": resolved_workspace_id,
        "project_id": project_id,
        "query": query,
        "page": page,
        "page_size": page_size,
        "total": len(items),
        "backend_total": raw_payload.get("total"),
        "models": items,
    }
    _emit_payload(ctx, payload, text_lines=[_model_label(item) for item in items])


@model_hub.command("detail")
@click.argument("model_id")
@click.option("--version", default=None, help="Fetch a specific model version detail.")
@pass_context
def detail_model_hub_cmd(ctx: Context, model_id: str, version: str | None) -> None:
    """Show Model Hub model detail."""
    session = require_web_session(ctx, hint="Reading Model Hub detail requires web authentication.")
    try:
        payload = model_hub_api.get_model_hub_detail(
            model_id,
            version=version,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return

    safe_payload = _redact_value(payload)
    if resolve_json_output(ctx, False):
        click.echo(json_formatter.format_json(safe_payload))
        return
    click.echo(json.dumps(safe_payload, ensure_ascii=False, indent=2))


@model_hub.command("versions")
@click.argument("model_id")
@pass_context
def versions_model_hub_cmd(ctx: Context, model_id: str) -> None:
    """List Model Hub versions for a repository model."""
    session = require_web_session(
        ctx, hint="Listing Model Hub versions requires web authentication."
    )
    try:
        payload = model_hub_api.list_model_hub_versions(model_id, session=session)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return

    safe_payload = _redact_value(payload)
    if resolve_json_output(ctx, False):
        click.echo(json_formatter.format_json(safe_payload))
        return
    click.echo(json.dumps(safe_payload, ensure_ascii=False, indent=2))


@model_hub.command("related-servings")
@click.argument("model_id")
@click.option("--version", default=None, help="Filter to a specific model version.")
@click.option("--page", type=int, default=1, show_default=True)
@click.option("--page-size", type=int, default=20, show_default=True)
@pass_context
def related_servings_model_hub_cmd(
    ctx: Context,
    model_id: str,
    version: str | None,
    page: int,
    page_size: int,
) -> None:
    """List inference servings linked to a Model Hub model."""
    if page < 1 or page_size == 0 or page_size < -1:
        _handle_error(
            ctx,
            "ValidationError",
            "--page must be >= 1 and --page-size must be -1 or >= 1",
            EXIT_VALIDATION_ERROR,
        )
        return
    session = require_web_session(
        ctx, hint="Reading Model Hub related servings requires web authentication."
    )
    try:
        payload = model_hub_api.list_model_hub_related_servings(
            model_id,
            version=version,
            page=page,
            page_size=page_size,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return

    total = payload.get("total")
    text_lines = [f"Related servings: {total if total is not None else 'unknown'}"]
    _emit_payload(
        ctx,
        {
            "model_id": model_id,
            "version": version,
            "page": page,
            "page_size": page_size,
            "related_servings": payload,
        },
        text_lines=text_lines,
    )


@model_hub.command("delete-check")
@click.argument("model_id")
@click.option("--version", default=None, help="Check a specific model version.")
@pass_context
def delete_check_model_hub_cmd(ctx: Context, model_id: str, version: str | None) -> None:
    """Check non-mutating deletion blockers for a Model Hub model."""
    session = require_web_session(
        ctx, hint="Checking Model Hub delete blockers requires web authentication."
    )
    try:
        pending = model_hub_api.check_model_hub_pending_serving(
            model_id,
            version=version,
            session=session,
        )
        related = model_hub_api.list_model_hub_related_servings(
            model_id,
            version=version,
            page=1,
            page_size=20,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return

    pending_value = pending.get("has_pending_serving")
    pending_known = isinstance(pending_value, bool)
    has_pending = bool(pending_value)
    related_total = related.get("total")
    related_known = isinstance(related_total, int)
    payload = {
        "model_id": model_id,
        "version": version,
        "has_pending_serving": has_pending,
        "pending_known": pending_known,
        "related_servings_total": related_total,
        "related_servings_total_known": related_known,
        "pending": pending,
        "related_servings": related,
        "safe_to_delete": pending_known
        and not has_pending
        and related_known
        and related_total == 0,
    }
    text_lines = [
        f"Pending serving: {'yes' if has_pending else 'no'}",
        f"Related servings: {related_total if related_total is not None else 'unknown'}",
    ]
    _emit_payload(ctx, payload, text_lines=text_lines)


__all__ = ["model_hub"]
