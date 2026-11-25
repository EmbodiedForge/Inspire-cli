"""Storage probe commands."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.confirm import confirmed_or_error
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import load_config, require_web_session, resolve_json_output
from inspire.config import ConfigError
from inspire.platform.web.browser_api import (
    DEFAULT_STORAGE_NAME,
    get_sftpgo_connection_info,
    list_storage_roots,
)
from inspire.platform.web.storage import (
    WebDAVEntry,
    WebDAVStorageClient,
    WebDAVStorageProbeError,
    normalize_remote_path,
    probe_webdav_storage,
)


def _resolve_probe_base_path(config, explicit_path: str | None) -> str:  # noqa: ANN001
    candidates = [
        explicit_path,
        getattr(config, "target_dir", None),
        getattr(config, "account_train_job_workdir", None),
        getattr(config, "account_shared_path_group", None),
    ]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        if not value.startswith("/"):
            raise ConfigError(f"Storage probe path must be absolute, got {value!r}")
        return value.rstrip("/")
    raise ConfigError(
        "Storage probe requires --path or a configured absolute shared path "
        "([defaults].target_dir, discovered train job workdir, or account shared_path_group)."
    )


@click.group(name="storage")
def storage() -> None:
    """Inspect and probe Inspire platform storage surfaces."""


def _entry_to_dict(entry: WebDAVEntry) -> dict:
    return entry.to_dict()


def _handle_storage_error(ctx: Context, exc: Exception) -> None:
    if isinstance(exc, ValueError):
        _handle_error(ctx, "ValidationError", str(exc), EXIT_VALIDATION_ERROR)
        return
    text = str(exc)
    if "403" in text:
        # Measured, not assumed: PUT and MKCOL are denied for ordinary accounts in every scope tried
        # (global_user, project root, global_public), while PROPFIND and GET work. The web console
        # uploads over the same WebDAV PUT, so this is a file-service permission, not a missing API
        # (Codeberg #37). Saying only "403 Forbidden" leaves the user to rediscover all of that.
        _handle_error(
            ctx,
            "StorageForbidden",
            text,
            EXIT_API_ERROR,
            hint=(
                "this account's WebDAV surface appears to be read-only (reads work; writes are "
                "denied). Options: copy through a running pod with `inspire job upload JOB_ID LOCAL "
                "REMOTE`, upload in the web console, or ask for write access on the file service. "
                "`inspire storage probe` shows which verbs are denied."
            ),
        )
        return
    _handle_error(ctx, "APIError", text, EXIT_API_ERROR)


def _get_storage_client(storage_name: str, session) -> WebDAVStorageClient:  # noqa: ANN001
    connection = get_sftpgo_connection_info(storage_name=storage_name, session=session)
    return WebDAVStorageClient(connection)


def _print_entries(entries: list[WebDAVEntry], *, long: bool) -> None:
    for entry in entries:
        name = entry.name + "/" if entry.is_dir and not entry.name.endswith("/") else entry.name
        if long:
            size = "-" if entry.size is None else str(entry.size)
            modified = entry.modified_at or "-"
            kind = "dir" if entry.is_dir else "file"
            click.echo(f"{kind:4} {size:>12} {modified:>29} {name}")
        else:
            click.echo(name)


@storage.command("roots")
@click.option(
    "--workspace-id",
    default=None,
    help="Workspace ID. Defaults to the current web session workspace.",
)
@click.option(
    "--storage",
    "storage_name",
    default=None,
    help="Only show one storage name, for example hdd.",
)
@pass_context
def storage_roots_cmd(
    ctx: Context,
    workspace_id: str | None,
    storage_name: str | None,
) -> None:
    """Discover platform storage roots."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Storage roots requires web authentication.")
    try:
        roots = list_storage_roots(
            workspace_id=workspace_id,
            storage_name=storage_name,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        _handle_storage_error(ctx, exc)
        return

    if json_output:
        click.echo(json_formatter.format_json({"roots": roots}))
        return

    for root in roots:
        click.echo(
            f"{root.get('storage', '-'):<8} "
            f"{root.get('category', '-'):<13} "
            f"{root.get('path', '')}"
        )


@storage.command("ls")
@click.argument("path")
@click.option(
    "--storage",
    "storage_name",
    default=DEFAULT_STORAGE_NAME,
    show_default=True,
    help="SFTPGo storage name.",
)
@click.option("--long", "long_output", is_flag=True, help="Show size and modified time.")
@pass_context
def storage_ls_cmd(ctx: Context, path: str, storage_name: str, long_output: bool) -> None:
    """List a remote WebDAV directory."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Storage ls requires web authentication.")
    try:
        remote_path = normalize_remote_path(path)
        entries = _get_storage_client(storage_name, session).listdir(remote_path)
    except Exception as exc:  # noqa: BLE001
        _handle_storage_error(ctx, exc)
        return

    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "path": remote_path,
                    "entries": [_entry_to_dict(entry) for entry in entries],
                }
            )
        )
        return

    _print_entries(entries, long=long_output)


@storage.command("stat")
@click.argument("path")
@click.option(
    "--storage",
    "storage_name",
    default=DEFAULT_STORAGE_NAME,
    show_default=True,
    help="SFTPGo storage name.",
)
@pass_context
def storage_stat_cmd(ctx: Context, path: str, storage_name: str) -> None:
    """Show remote file or directory metadata."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Storage stat requires web authentication.")
    try:
        entry = _get_storage_client(storage_name, session).stat(path)
    except Exception as exc:  # noqa: BLE001
        _handle_storage_error(ctx, exc)
        return

    payload = _entry_to_dict(entry)
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return

    click.echo(f"Path: {payload['path']}")
    click.echo(f"Type: {'directory' if payload['is_dir'] else 'file'}")
    click.echo(f"Size: {payload['size'] if payload['size'] is not None else '-'}")
    click.echo(f"Modified: {payload['modified_at'] or '-'}")
    if payload["etag"]:
        click.echo(f"ETag: {payload['etag']}")


@storage.command("cat")
@click.argument("path")
@click.option(
    "--storage",
    "storage_name",
    default=DEFAULT_STORAGE_NAME,
    show_default=True,
    help="SFTPGo storage name.",
)
@pass_context
def storage_cat_cmd(ctx: Context, path: str, storage_name: str) -> None:
    """Print remote file contents."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Storage cat requires web authentication.")
    try:
        status, content = _get_storage_client(storage_name, session).get(path)
    except Exception as exc:  # noqa: BLE001
        _handle_storage_error(ctx, exc)
        return

    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "path": normalize_remote_path(path),
                    "status_code": status,
                    "content": content.decode("utf-8", errors="replace"),
                    "size_bytes": len(content),
                }
            )
        )
        return

    sys.stdout.buffer.write(content)
    if not content.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")


@storage.command("get")
@click.argument("remote")
@click.argument("local")
@click.option(
    "--storage",
    "storage_name",
    default=DEFAULT_STORAGE_NAME,
    show_default=True,
    help="SFTPGo storage name.",
)
@pass_context
def storage_get_cmd(ctx: Context, remote: str, local: str, storage_name: str) -> None:
    """Download a remote file."""
    json_output = resolve_json_output(ctx, False)
    if json_output and local == "-":
        _handle_error(
            ctx,
            "ValidationError",
            "Cannot stream binary file content to stdout in JSON mode.",
            EXIT_VALIDATION_ERROR,
            hint="Use a local file path instead of '-', or omit --json to stream to stdout.",
        )
    session = require_web_session(ctx, hint="Storage get requires web authentication.")
    try:
        status, content = _get_storage_client(storage_name, session).get(remote)
        if local == "-":
            sys.stdout.buffer.write(content)
            return
        local_path = Path(local)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(content)
    except Exception as exc:  # noqa: BLE001
        _handle_storage_error(ctx, exc)
        return

    payload = {
        "remote": normalize_remote_path(remote),
        "local": str(local_path),
        "status_code": status,
        "size_bytes": len(content),
    }
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(
        f"Downloaded {payload['remote']} -> {payload['local']} ({payload['size_bytes']} bytes)"
    )


@storage.command("put")
@click.argument("local")
@click.argument("remote")
@click.option(
    "--storage",
    "storage_name",
    default=DEFAULT_STORAGE_NAME,
    show_default=True,
    help="SFTPGo storage name.",
)
@pass_context
def storage_put_cmd(ctx: Context, local: str, remote: str, storage_name: str) -> None:
    """Upload a local file to WebDAV."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Storage put requires web authentication.")
    try:
        content = sys.stdin.buffer.read() if local == "-" else Path(local).read_bytes()
        status = _get_storage_client(storage_name, session).put(remote, content)
    except Exception as exc:  # noqa: BLE001
        _handle_storage_error(ctx, exc)
        return

    payload = {
        "local": local,
        "remote": normalize_remote_path(remote),
        "status_code": status,
        "size_bytes": len(content),
    }
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(
        f"Uploaded {payload['local']} -> {payload['remote']} ({payload['size_bytes']} bytes)"
    )


@storage.command("rm")
@click.argument("path")
@click.option("--missing-ok", is_flag=True, help="Exit 0 when the path is already absent.")
@click.option(
    "--storage",
    "storage_name",
    default=DEFAULT_STORAGE_NAME,
    show_default=True,
    help="SFTPGo storage name.",
)
@click.option(
    "--yes",
    "--force",
    "yes",
    is_flag=True,
    help="Delete without an interactive confirmation (alias: --force)",
)
@pass_context
def storage_rm_cmd(ctx: Context, path: str, missing_ok: bool, storage_name: str, yes: bool) -> None:
    """Delete a remote file or directory."""
    # The raw path is quoted back rather than the normalized one: normalizing validates, and an invalid
    # path must still reach the storage error mapping below rather than raise on the way to a prompt.
    if not confirmed_or_error(ctx, assume_yes=yes, subject=f"delete remote path {path}"):
        raise click.Abort()

    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Storage rm requires web authentication.")
    try:
        status = _get_storage_client(storage_name, session).delete(path, missing_ok=missing_ok)
    except Exception as exc:  # noqa: BLE001
        _handle_storage_error(ctx, exc)
        return

    payload = {"path": normalize_remote_path(path), "status_code": status}
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(f"Deleted {payload['path']}")


@storage.command("mkdir")
@click.argument("path")
@click.option("-p", "--parents", is_flag=True, help="Create missing parent directories.")
@click.option("--exist-ok", is_flag=True, help="Do not fail if the directory already exists.")
@click.option(
    "--storage",
    "storage_name",
    default=DEFAULT_STORAGE_NAME,
    show_default=True,
    help="SFTPGo storage name.",
)
@pass_context
def storage_mkdir_cmd(
    ctx: Context,
    path: str,
    parents: bool,
    exist_ok: bool,
    storage_name: str,
) -> None:
    """Create a remote directory."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Storage mkdir requires web authentication.")
    try:
        client = _get_storage_client(storage_name, session)
        if parents:
            client.mkdir_parents(path, exist_ok=exist_ok)
            status = 201
        else:
            status = client.mkcol(path, exist_ok=exist_ok)
    except Exception as exc:  # noqa: BLE001
        _handle_storage_error(ctx, exc)
        return

    payload = {"path": normalize_remote_path(path).rstrip("/") + "/", "status_code": status}
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(f"Created {payload['path']}")


@storage.command("mv")
@click.argument("source")
@click.argument("dest")
@click.option("--overwrite", is_flag=True, help="Replace destination if it exists.")
@click.option(
    "--storage",
    "storage_name",
    default=DEFAULT_STORAGE_NAME,
    show_default=True,
    help="SFTPGo storage name.",
)
@pass_context
def storage_mv_cmd(
    ctx: Context,
    source: str,
    dest: str,
    overwrite: bool,
    storage_name: str,
) -> None:
    """Move or rename a remote file or directory."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Storage mv requires web authentication.")
    try:
        status = _get_storage_client(storage_name, session).move(
            source,
            dest,
            overwrite=overwrite,
        )
    except Exception as exc:  # noqa: BLE001
        _handle_storage_error(ctx, exc)
        return

    payload = {
        "source": normalize_remote_path(source),
        "dest": normalize_remote_path(dest),
        "status_code": status,
    }
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(f"Moved {payload['source']} -> {payload['dest']}")


@storage.command("connection-info")
@click.option(
    "--storage",
    "storage_name",
    default=DEFAULT_STORAGE_NAME,
    show_default=True,
    help="SFTPGo storage name.",
)
@click.option(
    "--refresh", is_flag=True, help="Accepted for compatibility; connection info is fetched live."
)
@pass_context
def storage_connection_info_cmd(
    ctx: Context,
    storage_name: str,
    refresh: bool,
) -> None:
    """Fetch SFTPGo/WebDAV connection info with secrets redacted."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Storage connection info requires web authentication.")
    try:
        info = get_sftpgo_connection_info(storage_name=storage_name, session=session)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return

    payload = info.redacted()
    payload["refresh_requested"] = bool(refresh)
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return

    click.echo(f"Storage: {payload['storage_name']}")
    click.echo(f"WebDAV: {payload['webdav_base_url']}")
    click.echo(f"User: {payload['username']}")
    click.echo("Password: <redacted>")


@storage.command("probe")
@click.option(
    "--path",
    "base_path",
    default=None,
    help="Absolute remote base directory for the probe. Defaults to configured shared path.",
)
@click.option(
    "--storage",
    "storage_name",
    default=DEFAULT_STORAGE_NAME,
    show_default=True,
    help="SFTPGo storage name.",
)
@pass_context
def storage_probe_cmd(
    ctx: Context,
    base_path: Optional[str],
    storage_name: str,
) -> None:
    """Run a tiny WebDAV create/read/delete probe."""
    json_output = resolve_json_output(ctx, False)
    try:
        config = load_config(ctx)
        resolved_base_path = _resolve_probe_base_path(config, base_path)
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", str(exc), EXIT_CONFIG_ERROR)
        return

    session = require_web_session(ctx, hint="Storage probe requires web authentication.")
    try:
        result = probe_webdav_storage(
            base_path=resolved_base_path,
            storage_name=storage_name,
            session=session,
        )
    except WebDAVStorageProbeError as exc:
        if json_output:
            _handle_error(
                ctx,
                "StorageProbeError",
                str(exc),
                EXIT_API_ERROR,
                data=exc.to_dict(),
            )
        else:
            click.echo(f"Error: {exc}", err=True)
            if exc.cleanup_needed:
                click.echo(
                    f"Cleanup OK: {str(exc.cleanup_ok).lower()} "
                    f"(removed the {len(exc.created_paths)} path(s) the probe created)",
                    err=True,
                )
            else:
                # Deleting paths that were never created is not a cleanup success; saying "Cleanup OK"
                # implied the probe had written something and tidied it up (issue #37).
                click.echo(
                    "Cleanup: not needed - the probe created nothing; the first operation was denied",
                    err=True,
                )
            if exc.probe_file:
                click.echo(f"Probe file: {exc.probe_file}", err=True)
            if exc.probe_dir:
                click.echo(f"Probe dir: {exc.probe_dir}", err=True)
        sys.exit(EXIT_API_ERROR)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return

    payload = result.to_dict()
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return

    click.echo("Storage probe succeeded.")
    click.echo(f"Storage: {result.storage_name}")
    click.echo(f"Base path: {result.base_path}")
    click.echo(f"Probe file: {result.probe_file}")
    click.echo(f"Content verified: {str(result.content_verified).lower()}")
    click.echo(f"Cleanup OK: {str(result.cleanup_ok).lower()}")


__all__ = ["storage"]
