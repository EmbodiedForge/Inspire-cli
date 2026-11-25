"""Top-level resource resolver and shortcut commands."""

from __future__ import annotations

import click

from inspire.cli.context import Context, EXIT_VALIDATION_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.confirm import confirmed_or_error
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.resource_resolver import (
    RESOURCE_TYPES,
    ResourceCache,
    normalize_resource_type,
    resolve_resource,
    resource_to_json,
)

_PASSTHROUGH_CONTEXT = {
    "ignore_unknown_options": True,
    "allow_extra_args": True,
}

_RESOURCE_TYPE_WORDS = {
    "job": "job",
    "jobs": "job",
    "notebook": "notebook",
    "notebooks": "notebook",
    "serving": "model",
    "servings": "model",
    "model": "model",
    "models": "model",
}


def _reject_resource_type_reference(operation: str, reference: str) -> None:
    """Refuse the `inspire stop notebook <id>` shape instead of resolving "notebook" as a name.

    The shortcut exists to resolve a resource *reference*; feeding it a type word skips that
    contract and lets the resolver pick an unrelated object with a similar name.  Measured:
    `inspire stop notebook <id>` stopped another session's notebook.  A usage error with the
    exact command is the only safe outcome.
    """
    canonical = _RESOURCE_TYPE_WORDS.get(str(reference or "").strip().lower())
    if canonical is None:
        return
    raise click.UsageError(
        f"`inspire {operation} {reference} ...` is ambiguous: {reference} is a resource type, "
        f"not a reference. Use `inspire {canonical} {operation} <id>`; confirm the id with "
        f"`inspire resolve <id> --type {canonical}`."
    )


def _group_for_resource(resource_type: str) -> click.Group:
    if resource_type == "job":
        from inspire.cli.commands.job import job

        return job
    if resource_type == "notebook":
        from inspire.cli.commands.notebook import notebook

        return notebook
    if resource_type == "serving":
        from inspire.cli.commands.model import model

        return model
    raise click.UsageError(f"Unsupported resource type: {resource_type}")


def _invoke_group(ctx: Context, resource_type: str, args: list[str]) -> None:
    group = _group_for_resource(resource_type)
    group.main(
        args=args,
        prog_name=f"inspire {resource_type}",
        standalone_mode=False,
        obj=ctx,
    )


def _shortcut(
    ctx: Context,
    operation: str,
    reference: str,
    passthrough_args: tuple[str, ...],
) -> None:
    _reject_resource_type_reference(operation, reference)
    verify = operation in {"stop", "logs", "exec"}
    allowed_types: tuple[str, ...] = RESOURCE_TYPES
    if operation == "logs":
        allowed_types = ("job", "serving")
    resolved = resolve_resource(ctx, reference, allowed_types=allowed_types, verify=verify)

    args = list(passthrough_args)
    if operation == "exec" and resolved.resource_type == "notebook":
        _invoke_group(ctx, "notebook", ["exec", "--notebook", resolved.resource_id, *args])
        return
    _invoke_group(
        ctx,
        resolved.resource_type,
        [operation, resolved.resource_id, *args],
    )


@click.command("resolve")
@click.argument("reference")
@click.option(
    "--type",
    "resource_type",
    type=click.Choice(["job", "notebook", "serving", "model"], case_sensitive=False),
    help="Restrict lookup to one resource type.",
)
@click.option("--verify", is_flag=True, help="Verify the resolved resource with the platform.")
@pass_context
def resolve_cmd(ctx: Context, reference: str, resource_type: str | None, verify: bool) -> None:
    """Resolve a resource name, ID, or cached alias."""

    allowed_types = None
    canonical_type = normalize_resource_type(resource_type)
    if resource_type and canonical_type is None:
        exit_with_error(
            ctx,
            "ValidationError",
            f"Unsupported resource type: {resource_type}",
            EXIT_VALIDATION_ERROR,
        )
    if canonical_type:
        allowed_types = [canonical_type]

    resolved = resolve_resource(ctx, reference, allowed_types=allowed_types, verify=verify)
    payload = resource_to_json(resolved)
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
        return

    parts = [payload["resource_type"], payload["resource_id"]]
    if payload.get("name"):
        parts.append(payload["name"])
    if payload.get("status"):
        parts.append(payload["status"])
    click.echo("\t".join(parts))


@click.group("cache")
def cache_cmd() -> None:
    """Inspect or clear the local resource resolver cache."""


@cache_cmd.command("show")
@click.option(
    "--type",
    "resource_type",
    type=click.Choice(["job", "notebook", "serving", "model"], case_sensitive=False),
    help="Show only one resource type.",
)
@pass_context
def cache_show_cmd(ctx: Context, resource_type: str | None) -> None:
    """List what the local resource cache holds; this reads a local file, not the platform.

    The cache maps shorthand references to resolved job/notebook/serving/model entries. Human output is
    `type<TAB>id[<TAB>name[<TAB>status]]`; `--json` also reports the cache file path. An empty cache is
    normal on a fresh machine - resolution re-queries the platform and repopulates it.
    """
    canonical_type = normalize_resource_type(resource_type)
    cache = ResourceCache()
    resources = cache.list(canonical_type)
    payload = [resource_to_json(candidate) for candidate in resources]
    if ctx.json_output:
        click.echo(
            json_formatter.format_json({"path": str(cache.cache_path), "resources": payload})
        )
        return
    if not resources:
        click.echo("Resource cache is empty.")
        return
    for resource in resources:
        fields = [resource.resource_type, resource.resource_id]
        if resource.name:
            fields.append(resource.name)
        if resource.status:
            fields.append(resource.status)
        click.echo("\t".join(fields))


@cache_cmd.command("stats")
@pass_context
def cache_stats_cmd(ctx: Context) -> None:
    """Report the resource cache's location, total size and per-type counts.

    Read-only, local. Useful before `cache clear` to see what would be dropped, and when a stale entry
    is suspected (the file path shown here is the one to inspect).
    """
    cache = ResourceCache()
    stats = cache.stats()
    if ctx.json_output:
        click.echo(json_formatter.format_json(stats))
        return
    click.echo(f"Path: {stats['path']}")
    click.echo(f"Total: {stats['total']}")
    for resource_type in RESOURCE_TYPES:
        click.echo(f"{resource_type}: {stats['by_type'].get(resource_type, 0)}")


@cache_cmd.command("clear")
@click.option(
    "--yes",
    "--force",
    "yes",
    is_flag=True,
    help="Clear without an interactive confirmation (alias: --force)",
)
@pass_context
def cache_clear_cmd(ctx: Context, yes: bool) -> None:
    """Delete the local resource-cache entries and report how many were removed.

    This affects only this machine's resolution cache - no platform object is touched, and nothing the
    platform stores is removed. Use it when a shorthand resolves to a stale or deleted resource.
    """
    if not confirmed_or_error(ctx, assume_yes=yes, subject="clear the local resource cache"):
        raise click.Abort()

    cache = ResourceCache()
    count = cache.clear()
    if ctx.json_output:
        click.echo(json_formatter.format_json({"cleared": count, "path": str(cache.cache_path)}))
        return
    click.echo(f"Cleared {count} resource cache entr{'y' if count == 1 else 'ies'}.")


@click.command("status", context_settings=_PASSTHROUGH_CONTEXT)
@click.argument("reference")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@pass_context
def status_shortcut(ctx: Context, reference: str, args: tuple[str, ...]) -> None:
    """Show status for a job, notebook, or model serving.

    These targets take no options, so nothing is hidden from this help; anything after REFERENCE is
    forwarded to the resolved command.
    """

    _shortcut(ctx, "status", reference, args)


@click.command("stop", context_settings=_PASSTHROUGH_CONTEXT)
@click.argument("reference")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@pass_context
def stop_shortcut(ctx: Context, reference: str, args: tuple[str, ...]) -> None:
    """Stop a job, notebook, or model serving.

    These targets take no options, so nothing is hidden from this help; anything after REFERENCE is
    forwarded to the resolved command.
    """

    _shortcut(ctx, "stop", reference, args)


@click.command("logs", context_settings=_PASSTHROUGH_CONTEXT)
@click.argument("reference")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@pass_context
def logs_shortcut(ctx: Context, reference: str, args: tuple[str, ...]) -> None:
    """Show logs for a job or model serving.

    Everything after REFERENCE is forwarded to `job logs`, which has the options this shortcut used to
    hide (Codeberg #23):

    \b
      --tail --head --worker --attempt --attempts --instance

    Full list: `inspire job logs --help`.
    """

    _shortcut(ctx, "logs", reference, args)


@click.command(
    "exec",
    context_settings=_PASSTHROUGH_CONTEXT,
    epilog=(
        "--json is global and goes before REFERENCE. An option the target does not know is not caught"
        " here - it travels to the target and fails there."
    ),
)
@click.argument("reference")
@click.argument("args", nargs=-1, required=True, type=click.UNPROCESSED)
@pass_context
def exec_shortcut(ctx: Context, reference: str, args: tuple[str, ...]) -> None:
    """Execute a command in a job, notebook, or model serving.

    Everything after REFERENCE is forwarded verbatim to the command for the resolved resource, so
    options belong after it, not before it.

    \b
      jobs:      --timeout --worker --attempt --instance
      notebooks: --timeout --bridge --notebook --detach --attach --after --follow

    Exact list: `inspire job exec --help` or `inspire notebook exec --help`. This shortcut used to
    advertise nothing but --help, while it is the documented entry point for driving the platform from a
    script - a reader could not discover that --timeout exists (Codeberg #23).
    """

    _shortcut(ctx, "exec", reference, args)


__all__ = [
    "cache_cmd",
    "exec_shortcut",
    "logs_shortcut",
    "resolve_cmd",
    "status_shortcut",
    "stop_shortcut",
]
