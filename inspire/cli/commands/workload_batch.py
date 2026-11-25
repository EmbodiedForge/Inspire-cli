"""Shared batch command surface for HPC and Ray launches."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
import time
from typing import Any

import click

from inspire.cli.context import Context, EXIT_API_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.batch_matrix import expand_batch, load_batch_file
from inspire.cli.utils.workload_launch import (
    PreparedWorkloadLaunch,
    prepare_hpc_launch,
    prepare_ray_launch,
    submit_prepared_launch,
)
from inspire.config import Config, ConfigError
from inspire.config.workload_profiles import normalize_profiles
from inspire.platform.web import browser_api
from inspire.platform.web.session import get_web_session


def _merged_batch_config(config: Config, data: dict[str, Any]) -> Config:
    local = normalize_profiles(data.get("profiles", {}))
    profiles = deepcopy(config.profiles)
    for kind, entries in local.items():
        profiles.setdefault(kind, {}).update(entries)
    return replace(config, profiles=profiles)


def _prepare_item(
    *,
    kind: str,
    config: Config,
    item: dict[str, Any],
    session: Any,
) -> PreparedWorkloadLaunch:
    name = str(item.get("name") or "").strip()
    entrypoint = str(item.get("entrypoint") or item.get("command") or "").strip()
    profile_name = str(item.get("profile") or "").strip() or None
    workspace_id = str(item.get("workspace_id") or "").strip() or None
    explicit = {
        key: value
        for key, value in item.items()
        if key
        not in {
            "_matrix",
            "command",
            "entrypoint",
            "name",
            "profile",
            "workspace_id",
        }
    }
    prepare = prepare_hpc_launch if kind == "hpc" else prepare_ray_launch
    return prepare(
        config=config,
        name=name,
        entrypoint=entrypoint,
        profile_name=profile_name,
        explicit=explicit,
        explicit_workspace_id=workspace_id,
        session=session,
    )


def _created_id(kind: str, result: dict[str, Any]) -> str:
    keys = ("job_id", "id") if kind == "hpc" else ("ray_job_id", "id")
    return next((str(result.get(key) or "").strip() for key in keys if result.get(key)), "")


def _not_found(exc: Exception) -> bool:
    message = str(exc).lower()
    return "not found" in message or "resourcenotfound" in message or "record not found" in message


def _cleanup_submission(
    *,
    kind: str,
    workload_id: str,
    session: Any,
    delete_attempts: int = 3,
) -> dict[str, Any]:
    stop_error = None
    try:
        if kind == "hpc":
            browser_api.stop_hpc_job(workload_id, session=session)
        else:
            browser_api.stop_ray_job(workload_id, session=session)
    except Exception as exc:  # noqa: BLE001 - cleanup continues through terminal states
        if not _not_found(exc):
            stop_error = str(exc)

    delete_error = None
    deleted = False
    for attempt in range(1, delete_attempts + 1):
        try:
            if kind == "hpc":
                browser_api.delete_hpc_job(workload_id, session=session)
            else:
                browser_api.delete_ray_job(workload_id, session=session)
            deleted = True
            delete_error = None
            break
        except Exception as exc:  # noqa: BLE001 - bounded cleanup retry
            if _not_found(exc):
                deleted = True
                delete_error = None
                break
            delete_error = str(exc)
            if attempt < delete_attempts:
                time.sleep(1)

    return {
        "id": workload_id,
        "stopped": stop_error is None,
        "stop_error": stop_error,
        "deleted": deleted,
        "delete_error": delete_error,
        "cleanup_complete": deleted,
    }


def _rollback_submissions(
    *,
    kind: str,
    submissions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        _cleanup_submission(
            kind=kind,
            workload_id=str(item["id"]),
            session=item["prepared"].session,
        )
        for item in reversed(submissions)
    ]


def _emit_batch_failure(ctx: Context, payload: dict[str, Any]) -> None:
    message = str(payload["failure"]["message"])
    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error(
                "BatchMutationError",
                message,
                EXIT_API_ERROR,
                data=payload,
            ),
            err=True,
        )
    else:
        click.echo(f"Error: {message}", err=True)
        rollback = payload.get("rollback") or {}
        if rollback.get("attempted"):
            click.echo(
                f"Rollback: {'complete' if rollback.get('complete') else 'incomplete'}",
                err=True,
            )
        retained = payload.get("retained_ids") or []
        if retained:
            click.echo(f"Retained workload IDs: {', '.join(retained)}", err=True)
        untracked = payload.get("untracked_names") or []
        if untracked:
            click.echo(f"Untracked successful item names: {', '.join(untracked)}", err=True)
    sys.exit(EXIT_API_ERROR)


def build_batch_command(kind: str) -> click.Command:
    """Build a workload-scoped JSON/TOML matrix batch command."""

    @click.command("batch")
    @click.argument("path", type=click.Path(path_type=Path, exists=True, dir_okay=False))
    @click.option("--dry-run", is_flag=True, help="Prepare every launch without mutation")
    @click.option(
        "--keep-partial",
        is_flag=True,
        help="Keep successful submissions if a later batch item fails",
    )
    @pass_context
    def batch(ctx: Context, path: Path, dry_run: bool, keep_partial: bool) -> None:
        """Expand and prepare a JSON/TOML Cartesian launch matrix."""
        try:
            data = load_batch_file(path)
            config, _ = Config.from_files_and_env(require_target_dir=False)
            config = _merged_batch_config(config, data)
            items = expand_batch(data, kind=kind)
            session = get_web_session(account=config.account_alias)
            prepared = [
                _prepare_item(kind=kind, config=config, item=item, session=session)
                for item in items
            ]
        except (ConfigError, ValueError, TypeError) as exc:
            raise click.ClickException(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(f"Batch launch failed: {exc}") from exc

        results: list[dict[str, Any]] = []
        submissions: list[dict[str, Any]] = []
        if not dry_run:
            for index, item in enumerate(prepared):
                name = str(item.preview.intent.get("name") or "")
                untracked_names: list[str] = []
                try:
                    result = submit_prepared_launch(item)
                    workload_id = _created_id(kind, result)
                    if not workload_id:
                        untracked_names.append(name)
                        raise ValueError(
                            f"{kind} create response for batch item {index} did not include an ID"
                        )
                    results.append(result)
                    submissions.append(
                        {
                            "index": index,
                            "name": name,
                            "id": workload_id,
                            "result": result,
                            "prepared": item,
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - mutation failure includes rollback
                    rollback_items = (
                        []
                        if keep_partial
                        else _rollback_submissions(kind=kind, submissions=submissions)
                    )
                    retained_ids = (
                        [str(submission["id"]) for submission in submissions]
                        if keep_partial
                        else [
                            str(rollback["id"])
                            for rollback in rollback_items
                            if not rollback["cleanup_complete"]
                        ]
                    )
                    failure_payload = {
                        "workload": kind,
                        "dry_run": False,
                        "count": len(prepared),
                        "previews": [prepared_item.preview.to_dict() for prepared_item in prepared],
                        "submissions": [
                            {key: value for key, value in submission.items() if key != "prepared"}
                            for submission in submissions
                        ],
                        "failure": {
                            "index": index,
                            "name": name,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        },
                        "rollback": {
                            "attempted": not keep_partial and bool(submissions),
                            "complete": all(
                                rollback["cleanup_complete"] for rollback in rollback_items
                            )
                            and not untracked_names,
                            "items": rollback_items,
                        },
                        "retained_ids": retained_ids,
                        "untracked_names": untracked_names,
                        "keep_partial": keep_partial,
                    }
                    _emit_batch_failure(ctx, failure_payload)

        payload = {
            "workload": kind,
            "dry_run": dry_run,
            "count": len(prepared),
            "previews": [item.preview.to_dict() for item in prepared],
            "results": results,
            "submissions": [
                {key: value for key, value in submission.items() if key != "prepared"}
                for submission in submissions
            ],
        }
        if ctx.json_output:
            click.echo(json_formatter.format_json(payload))
            return
        action = "Prepared" if dry_run else "Submitted"
        click.echo(f"{action} {len(prepared)} {kind} launch(es).")
        for index, item in enumerate(prepared):
            click.echo(f"{index}\t{item.preview.intent.get('name') or '-'}")

    return batch


__all__ = ["build_batch_command"]
