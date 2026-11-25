"""Dataset catalogue commands backed by the SII data square.

Two platforms answer here and they are not interchangeable. The data square
(``aip.sii.edu.cn``) is the public catalogue - what exists, who maintains it, which versions were
ever published. The compute platform (``qz.sii.edu.cn``) is the one that decides whether a given
version can actually be attached to a job in a given workspace. ``check`` is the only command that
asks the second question, and it answers per item: a catalogue entry that will not mount is a
result, not a transport failure.
"""

from __future__ import annotations

import json
import shlex
from datetime import datetime, timezone
from typing import Any

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.logging_setup import redact_text
from inspire.cli.utils.datasets import (
    DATASET_REFERENCE_HINT,
    NO_VERDICT_MESSAGE,
    dataset_mount_path,
    index_validation_results,
    parse_backend_error,
    parse_dataset_target,
    validation_pair_key,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import require_web_session, resolve_json_output
from inspire.platform.web.browser_api import datasets as compute_datasets
from inspire.platform.web.datasquare import client as datasquare_client
from inspire.platform.web.datasquare import datasets as datasquare_datasets
from inspire.platform.web.datasquare.session import get_datasquare_session
from inspire.platform.web.session import SessionExpiredError, WebSession

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

#: Description length that keeps a `list` row on one terminal line.
_DESCRIPTION_WIDTH = 60


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


def _emit_payload(ctx: Context, payload: dict[str, Any], *, text_lines: list[str]) -> None:
    safe_payload = _redact_value(payload)
    if resolve_json_output(ctx, False):
        click.echo(json_formatter.format_json(safe_payload))
        return
    if text_lines:
        for line in text_lines:
            click.echo(redact_text(line))
    else:
        click.echo("No datasets found.")


def _datasquare_login_hint(ctx: Context) -> str:
    account = shlex.quote(ctx.account_alias or "<account-alias>")
    return (
        "From the Inspire CLI checkout, log in to the data square with your vault keys. "
        f"Run `node scripts/login_web_session.mjs --portal aip --account {account} "
        "--account-key '<vault-account-key>' --password-key '<vault-password-key>'`."
    )


def require_datasquare_session(ctx: Context, *, hint: str) -> WebSession:
    """Return a cached data-square session, or exit 11 with the login command to run.

    Unlike the compute-platform session there is nothing to re-authenticate against: the portal
    issues its cookie only through its own login page, so a missing or expired cache is the end
    of the road for this invocation.
    """
    session = get_datasquare_session(account=ctx.account_alias)
    if session is not None:
        return session
    _handle_error(
        ctx,
        "AuthenticationError",
        "No usable data-square session cached for account "
        f"{ctx.account_alias or '<default>'}. {hint}",
        EXIT_AUTH_ERROR,
        hint=_datasquare_login_hint(ctx),
    )
    raise SystemExit(EXIT_AUTH_ERROR)  # pragma: no cover - _handle_error exits


def _fail_from_datasquare(ctx: Context, exc: Exception) -> None:
    """Map a data-square failure onto the contract's exit codes.

    A zero-cookie portal answers HTTP 401 and the body code ``7`` for the same condition, so both
    are authentication, not platform, errors.
    """
    if isinstance(exc, datasquare_client.DatasquareSessionExpired) or (
        isinstance(exc, datasquare_client.DatasquareAPIError)
        and exc.code == datasquare_client.DATASQUARE_CODE_UNAUTHORIZED
    ):
        _handle_error(
            ctx,
            "AuthenticationError",
            str(exc),
            EXIT_AUTH_ERROR,
            hint=_datasquare_login_hint(ctx),
        )
        return
    _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)


def _parse_target_or_error(ctx: Context, raw: str) -> tuple[str, str] | None:
    """Split a ``DATASET_CODE@VERSION`` argument, exiting 12 with the syntax when it is unusable.

    Returns None only after the error has been reported, so the caller just stops.
    """
    try:
        return parse_dataset_target(raw)
    except ValueError as exc:
        _handle_error(
            ctx,
            "ValidationError",
            str(exc),
            EXIT_VALIDATION_ERROR,
            hint=DATASET_REFERENCE_HINT,
        )
        return None


def _map_tag(raw: Any) -> dict[str, Any]:
    tag = raw if isinstance(raw, dict) else {}
    return {
        "tag_id": tag.get("tagId"),
        "tag_name": tag.get("tagName"),
        "tag_path": tag.get("tagPath"),
        "category_id": tag.get("categoryId"),
    }


def _map_dataset_fields(row: dict[str, Any]) -> dict[str, Any]:
    """The fields the catalogue and the detail record agree on, in snake_case."""
    return {
        "dataset_id": row.get("datasetId"),
        "dataset_code": row.get("datasetCode"),
        "project_name": row.get("projectName"),
        "director": row.get("director"),
        "maintainer": row.get("maintenance"),
        "level": row.get("super"),
        "level_sub": row.get("superSub"),
        "owner_name": row.get("ownerName"),
        "description": row.get("description"),
        "star_count": row.get("starCount"),
        "view_count": row.get("viewCount"),
        "created_at": row.get("createdAt"),
        "updated_at": row.get("updatedAt"),
        "state": row.get("state"),
        "has_permission": bool(row.get("hasPermission")),
        "tags": [_map_tag(tag) for tag in row.get("tags") or []],
    }


def _parse_replica_status(raw: Any) -> Any:
    """The replica strategy as an object, or the raw string when it is not JSON.

    The portal stores ``replicaStatus`` as a JSON string inside its JSON response. Parsing it is
    a convenience; keeping an unparseable value verbatim is the guarantee that makes the
    convenience safe to take.
    """
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _map_version(dataset_code: str, raw: Any) -> dict[str, Any]:
    version = raw if isinstance(raw, dict) else {}
    version_code = str(version.get("versionCode") or "")
    return {
        "version_id": version.get("versionId"),
        "version_code": version_code,
        "file_path": version.get("filePath"),
        "files_count": version.get("filesCount"),
        "files_size": version.get("filesSize"),
        "version_state": version.get("versionState"),
        "replica_strategy": version.get("replicaStrategy"),
        "replica_status": _parse_replica_status(version.get("replicaStatus")),
        "update_time": version.get("updateTime"),
        "description": version.get("description"),
        "mount_path": dataset_mount_path(dataset_code, version_code),
    }


def _map_dataset_detail(row: dict[str, Any]) -> dict[str, Any]:
    detail = _map_dataset_fields(row)
    detail.update(
        {
            "description_html": row.get("descriptionHtml"),
            "license_name": row.get("licenseName"),
            "license_url": row.get("licenseUrl"),
            "versions": [
                _map_version(str(row.get("datasetCode") or ""), version)
                for version in row.get("versions") or []
            ],
        }
    )
    return detail


def _dataset_row_label(row: dict[str, Any]) -> str:
    description = " ".join(str(row.get("description") or "").split())
    if len(description) > _DESCRIPTION_WIDTH:
        description = description[:_DESCRIPTION_WIDTH].rstrip() + "…"
    permission = "yes" if row.get("has_permission") else "no"
    return (
        f"{row.get('dataset_code') or '-'}  id={row.get('dataset_id')}  "
        f"level={row.get('level') or '-'}  state={row.get('state') or '-'}  "
        f"permission={permission}  stars={row.get('star_count') or 0}  {description}"
    )


@click.group("dataset")
def dataset() -> None:
    """Browse the data-square dataset catalogue and check what will mount."""


@dataset.command("list")
@click.argument("query", required=False, default="")
@click.option("--page", type=int, default=1, show_default=True)
@click.option("--page-size", type=int, default=20, show_default=True)
@pass_context
def list_datasets_cmd(ctx: Context, query: str, page: int, page_size: int) -> None:
    """List datasets in the data-square catalogue."""
    if page < 1 or page_size < 1:
        _handle_error(
            ctx,
            "ValidationError",
            "--page must be >= 1 and --page-size must be >= 1",
            EXIT_VALIDATION_ERROR,
        )
        return

    session = require_datasquare_session(
        ctx, hint="Listing datasets requires a data-square session."
    )
    try:
        payload = datasquare_datasets.list_datasets(
            session, keyword=query, page=page, page_size=page_size
        )
    except Exception as exc:  # noqa: BLE001
        _fail_from_datasquare(ctx, exc)
        return

    raw_items = payload.get("list")
    items = [_map_dataset_fields(item) for item in raw_items or [] if isinstance(item, dict)]
    total = payload.get("total")
    text_lines = [_dataset_row_label(item) for item in items]
    if not text_lines:
        text_lines = [
            "No datasets found"
            + (f" for {query!r}." if query else ".")
            + " Try a broader query: inspire dataset list"
        ]
    _emit_payload(
        ctx,
        {
            "items": items,
            "total": total if isinstance(total, int) else len(items),
            "page": page,
            "page_size": page_size,
            "query": query,
        },
        text_lines=text_lines,
    )


@dataset.command("versions")
@click.argument("dataset_code")
@pass_context
def versions_dataset_cmd(ctx: Context, dataset_code: str) -> None:
    """List the published versions of one dataset."""
    session = require_datasquare_session(
        ctx, hint="Reading dataset versions requires a data-square session."
    )
    try:
        rows = datasquare_datasets.get_datasets_with_versions(session, dataset_code)
    except Exception as exc:  # noqa: BLE001
        _fail_from_datasquare(ctx, exc)
        return

    matches = [row for row in rows if isinstance(row, dict)]
    exact = [row for row in matches if str(row.get("datasetCode") or "") == dataset_code]
    # The code search is a substring search: an exact hit is the requested dataset, anything
    # else is every code that merely contains it, which the caller has to choose between.
    chosen = exact or matches
    items: list[dict[str, Any]] = []
    for row in chosen:
        row_code = str(row.get("datasetCode") or dataset_code)
        for version in row.get("versions") or []:
            mapped = version if isinstance(version, dict) else {}
            version_code = str(mapped.get("versionCode") or "")
            items.append(
                {
                    "version_id": mapped.get("versionId"),
                    "version_code": version_code,
                    "mount_path": dataset_mount_path(row_code, version_code),
                }
            )

    payload: dict[str, Any] = {
        "dataset_code": dataset_code,
        "items": items,
        "total": len(items),
    }
    if not exact and len(matches) > 1:
        payload["matches"] = [str(row.get("datasetCode") or "") for row in matches]

    text_lines = [f"{item['version_code']}  {item['mount_path']}" for item in items]
    if not text_lines:
        text_lines = [f"No versions found for {dataset_code!r}."]
    elif not exact and len(matches) > 1:
        text_lines.insert(
            0,
            f"{dataset_code!r} is not an exact code; versions of: "
            + ", ".join(str(row.get("datasetCode") or "") for row in matches),
        )
    _emit_payload(ctx, payload, text_lines=text_lines)


@dataset.command("get")
@click.argument("dataset_id_or_code")
@pass_context
def get_dataset_cmd(ctx: Context, dataset_id_or_code: str) -> None:
    """Show one dataset with its versions and storage paths."""
    session = require_datasquare_session(
        ctx, hint="Reading a dataset requires a data-square session."
    )
    reference = dataset_id_or_code.strip()

    try:
        if reference.isdigit():
            dataset_id: Any = int(reference)
        else:
            listed = datasquare_datasets.list_datasets(
                session, keyword=reference, page=1, page_size=20
            )
            raw_rows = listed.get("list")
            exact = [
                row
                for row in raw_rows or []
                if isinstance(row, dict) and str(row.get("datasetCode") or "") == reference
            ]
            if not exact:
                _handle_error(
                    ctx,
                    "ValidationError",
                    f"No dataset with code {reference!r} in the data square.",
                    EXIT_VALIDATION_ERROR,
                    hint=f"Search the catalogue: inspire dataset list {reference}",
                )
                return
            dataset_id = exact[0].get("datasetId")

        row = datasquare_datasets.find_dataset(session, dataset_id)
    except Exception as exc:  # noqa: BLE001
        _fail_from_datasquare(ctx, exc)
        return

    if not row:
        _handle_error(
            ctx,
            "ValidationError",
            f"No dataset with id {reference} in the data square.",
            EXIT_VALIDATION_ERROR,
            hint="Search the catalogue: inspire dataset list",
        )
        return

    detail = _map_dataset_detail(row)
    safe_payload = _redact_value(detail)
    if resolve_json_output(ctx, False):
        click.echo(json_formatter.format_json(safe_payload))
        return

    click.echo(_dataset_row_label(detail))
    for field in ("project_name", "maintainer", "owner_name", "license_name"):
        if detail.get(field):
            click.echo(f"{field}: {redact_text(str(detail.get(field)))}")
    if detail.get("description"):
        click.echo(redact_text(str(detail.get("description"))))
    versions = detail.get("versions") or []
    if versions:
        click.echo(f"versions ({len(versions)}):")
        for version in versions:
            click.echo(
                f"  {version.get('version_code') or '-'}  "
                f"state={version.get('version_state') or '-'}  "
                f"files={version.get('files_count')}  {version.get('mount_path')}"
            )
    else:
        click.echo("No published versions.")


def _find_catalogue_row(datasquare_session: WebSession, dataset_code: str) -> dict[str, Any] | None:
    """Exact-match a dataset code in the square's catalogue, or None."""
    try:
        listed = datasquare_datasets.list_datasets(
            datasquare_session, keyword=dataset_code, page=1, page_size=20
        )
    except Exception:  # noqa: BLE001 - the join is a diagnosis, not a dependency
        return None
    for row in listed.get("list") or []:
        if isinstance(row, dict) and str(row.get("datasetCode") or "") == dataset_code:
            return row
    return None


def _diagnose_failed_pair(
    *,
    catalogue_row: dict[str, Any] | None,
    version_code: str,
) -> str | None:
    """Say *why* a pair failed by reading the catalogue record the square already holds.

    ``code: 2001, message: 版本不存在`` is one string for at least three situations that call
    for opposite remedies (#81): a typo, a version that exists but has no replica yet, and a
    dataset that was registered without any version at all. The catalogue separates them;
    the raw platform string stays in the payload verbatim, this diagnosis is added next to it.
    """
    if not catalogue_row:
        return None
    maintainer = str(catalogue_row.get("maintenance") or "").strip()
    ask = f" (ask {maintainer})" if maintainer else ""
    state = str(catalogue_row.get("state") or "").strip()

    versions = [
        version
        for version in catalogue_row.get("versions") or []
        if isinstance(version, dict) and str(version.get("versionCode") or "") == version_code
    ]

    if not catalogue_row.get("hasPermission"):
        return f"no access grant; request it from the maintainer{ask}"
    if not versions:
        if not (catalogue_row.get("versions") or []):
            return f"dataset registered, no version created (state={state or 'unknown'})"
        return None  # the version code genuinely does not exist: the raw verdict is the truth
    version_state = str(versions[0].get("versionState") or "").strip()
    if version_state and version_state != "active":
        return (
            f"registered but {version_state}; the platform has no replica yet - re-check later{ask}"
        )
    return None


def _annotate_failed_items_with_catalogue(
    ctx: Context,
    items: list[dict[str, Any]],
) -> None:
    """Join failed check verdicts with the square's catalogue, best-effort.

    Only failed pairs are looked up, and only when a cached data-square session exists: the
    verdict itself needs just the compute session, so a missing square session degrades to the
    pre-#81 output instead of failing the command.
    """
    failed = [item for item in items if not item.get("ok")]
    if not failed:
        return
    datasquare_session = get_datasquare_session(account=ctx.account_alias)
    if datasquare_session is None:
        return

    catalogue: dict[str, dict[str, Any] | None] = {}
    for item in failed:
        code = str(item.get("dataset") or "")
        if code not in catalogue:
            catalogue[code] = _find_catalogue_row(datasquare_session, code)
        row = catalogue[code]
        if row is None:
            continue
        item["catalogue_state"] = row.get("state")
        item["has_permission"] = bool(row.get("hasPermission"))
        if row.get("maintenance"):
            item["maintainer"] = row.get("maintenance")
        diagnosis = _diagnose_failed_pair(
            catalogue_row=row,
            version_code=str(item.get("version") or ""),
        )
        if diagnosis:
            item["diagnosis"] = diagnosis


@dataset.command("check")
@click.argument("targets", nargs=-1, required=True, metavar="DATASET_CODE@VERSION...")
@click.option("--workspace-id", default=None, help="Workspace ID. Defaults to current session.")
@pass_context
def check_datasets_cmd(ctx: Context, targets: tuple[str, ...], workspace_id: str | None) -> None:
    """Check whether dataset versions can be mounted in a workspace."""
    pairs: list[dict[str, str]] = []
    for raw in targets:
        parsed = _parse_target_or_error(ctx, raw)
        if parsed is None:
            return
        dataset_code, version_code = parsed
        pairs.append({"dataset_id": dataset_code, "version_id": version_code})

    session: WebSession = require_web_session(
        ctx, hint="Checking datasets requires web authentication."
    )
    resolved_workspace_id = workspace_id or getattr(session, "workspace_id", None)
    if not resolved_workspace_id:
        _handle_error(
            ctx,
            "ValidationError",
            "--workspace-id is required and the current session has none.",
            EXIT_VALIDATION_ERROR,
        )
        return

    try:
        results = compute_datasets.validate_datasets(pairs, resolved_workspace_id, session=session)
    except SessionExpiredError as exc:
        # The compute-platform session re-authenticates itself once and only reports this when
        # that failed, which is an authentication finding, not a platform one.
        _handle_error(
            ctx,
            "AuthenticationError",
            str(exc),
            EXIT_AUTH_ERROR,
            hint="Run `inspire init` to re-authenticate the web session.",
        )
        return
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return

    verdicts = index_validation_results(results)

    items: list[dict[str, Any]] = []
    for pair in pairs:
        dataset_code = pair["dataset_id"]
        version_code = pair["version_id"]
        result = verdicts.get(validation_pair_key(dataset_code, version_code))
        if result is None:
            items.append(
                {
                    "dataset": dataset_code,
                    "version": version_code,
                    "ok": False,
                    "error_code": None,
                    "error_message": NO_VERDICT_MESSAGE,
                    "backend_path": None,
                    "mount_path": dataset_mount_path(dataset_code, version_code),
                }
            )
            continue
        ok = bool(result.get("success"))
        error_code, error_message = parse_backend_error(result.get("error_message"))
        backend_path = str(result.get("path") or "").strip()
        items.append(
            {
                "dataset": dataset_code,
                "version": version_code,
                "ok": ok,
                "error_code": error_code,
                "error_message": "" if ok else error_message,
                "backend_path": backend_path or None,
                "mount_path": dataset_mount_path(dataset_code, version_code),
            }
        )

    all_ok = all(item["ok"] for item in items)
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for item in items:
        # A mount verdict is a statement about a moment, not about a dataset (#81): the
        # platform is eventually consistent about replicas, so stamp every item.
        item["checked_at"] = checked_at
    _annotate_failed_items_with_catalogue(ctx, items)
    _emit_payload(
        ctx,
        {
            "items": items,
            "all_ok": all_ok,
            "workspace_id": resolved_workspace_id,
            "checked_at": checked_at,
        },
        text_lines=[
            (
                f"{item['dataset']}@{item['version']}  ok  path={item['backend_path']}"
                if item["ok"]
                else (
                    f"{item['dataset']}@{item['version']}  FAILED  {item['error_message']}"
                    + (f"  [{item['diagnosis']}]" if item.get("diagnosis") else "")
                )
            )
            for item in items
        ],
    )


@dataset.command("mount-path")
@click.argument("targets", nargs=-1, required=True, metavar="DATASET_CODE@VERSION...")
@pass_context
def mount_path_dataset_cmd(ctx: Context, targets: tuple[str, ...]) -> None:
    """Print the in-container mount path for dataset versions."""
    items: list[dict[str, Any]] = []
    for raw in targets:
        parsed = _parse_target_or_error(ctx, raw)
        if parsed is None:
            return
        dataset_code, version_code = parsed
        items.append(
            {
                "dataset": dataset_code,
                "version": version_code,
                "mount_path": dataset_mount_path(dataset_code, version_code),
            }
        )

    _emit_payload(
        ctx,
        {"items": items},
        text_lines=[str(item["mount_path"]) for item in items],
    )


__all__ = [
    "dataset",
    "require_datasquare_session",
]
