"""Project subcommands."""

from __future__ import annotations

from collections.abc import Iterable
import concurrent.futures
import hashlib
import json
import os
import time
import click
import tomlkit

from inspire.cli.context import (
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    Context,
    EXIT_API_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import (
    require_web_session,
    resolve_json_output,
)
from inspire.cli.utils import project_order as project_order_utils
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session.models import SESSION_CACHE_DIR, normalize_account_for_cache
from inspire.config import Config, ConfigError

_ZERO_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"
_PROJECT_LIST_MAX_WORKERS = 16
_PROJECT_LIST_WORKSPACE_FANOUT_LIMIT = 6
_PROJECT_LIST_CACHE_TTL_SECONDS = 60
_PROJECT_LIST_CACHE_MAX_ENTRIES = 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_to_dict(proj: browser_api_module.ProjectInfo) -> dict:
    """Convert a ProjectInfo to a plain dict for JSON output."""
    return {
        "project_id": proj.project_id,
        "name": proj.name,
        "workspace_id": proj.workspace_id,
        "workspace_ids": proj.workspace_ids or [proj.workspace_id],
        "budget": proj.budget,
        "remain_budget": proj.remain_budget,
        "member_remain_budget": proj.member_remain_budget,
        "gpu_limit": proj.gpu_limit,
        "member_gpu_limit": proj.member_gpu_limit,
        "priority_level": proj.priority_level,
        "priority_name": proj.priority_name,
    }


def _unique_workspace_ids(values: Iterable[str | None]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        ws_id = str(value or "").strip()
        if not ws_id or ws_id == _ZERO_WORKSPACE_ID:
            continue
        if ws_id in seen:
            continue
        seen.add(ws_id)
        unique.append(ws_id)
    return unique


def _merge_projects(
    projects: list[browser_api_module.ProjectInfo],
    additional: list[browser_api_module.ProjectInfo],
    *,
    seen: set[str],
) -> None:
    existing_by_id = {project.project_id: project for project in projects}
    for project in additional:
        existing = existing_by_id.get(project.project_id)
        if existing is None:
            seen.add(project.project_id)
            existing_by_id[project.project_id] = project
            projects.append(project)
            continue
        if not existing.workspace_ids and existing.workspace_id:
            existing.workspace_ids = [existing.workspace_id]
        for workspace_id in project.workspace_ids or [project.workspace_id]:
            if workspace_id and workspace_id not in existing.workspace_ids:
                existing.workspace_ids.append(workspace_id)


def _collect_workspace_projects(
    workspace_ids: list[str],
    *,
    session,
) -> tuple[list[browser_api_module.ProjectInfo], list[tuple[str, str]]]:
    """Collect projects across workspace IDs.

    The first workspace is queried serially to establish the request mode
    (HTTP vs browser fallback). Remaining workspaces are fetched in parallel.
    Browser fallback is safe because clients are cached per-thread.
    """
    projects: list[browser_api_module.ProjectInfo] = []
    seen: set[str] = set()
    workspace_errors: list[tuple[str, str]] = []

    if not workspace_ids:
        return projects, workspace_errors

    first_ws_id = workspace_ids[0]
    try:
        first_projects = browser_api_module.list_projects(workspace_id=first_ws_id, session=session)
        _merge_projects(projects, first_projects, seen=seen)
    except Exception as exc:
        workspace_errors.append((first_ws_id, str(exc)))

    remaining_ws_ids = workspace_ids[1:]
    if not remaining_ws_ids:
        return projects, workspace_errors

    if len(remaining_ws_ids) > 1:
        max_workers = min(len(remaining_ws_ids), _PROJECT_LIST_MAX_WORKERS)
        results_by_workspace: dict[str, list[browser_api_module.ProjectInfo]] = {}
        errors_by_workspace: dict[str, str] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    browser_api_module.list_projects, workspace_id=ws_id, session=session
                ): ws_id
                for ws_id in remaining_ws_ids
            }
            for future in concurrent.futures.as_completed(futures):
                ws_id = futures[future]
                try:
                    results_by_workspace[ws_id] = future.result()
                except Exception as exc:
                    errors_by_workspace[ws_id] = str(exc)

        for ws_id in remaining_ws_ids:
            if ws_id in errors_by_workspace:
                workspace_errors.append((ws_id, errors_by_workspace[ws_id]))
                continue
            _merge_projects(projects, results_by_workspace.get(ws_id, []), seen=seen)
        return projects, workspace_errors

    for ws_id in remaining_ws_ids:
        try:
            ws_projects = browser_api_module.list_projects(workspace_id=ws_id, session=session)
            _merge_projects(projects, ws_projects, seen=seen)
        except Exception as exc:
            workspace_errors.append((ws_id, str(exc)))
    return projects, workspace_errors


def _select_workspace_ids_for_listing(
    workspace_ids: list[str],
    *,
    session_workspace_id: str | None,
    all_workspaces: bool,
) -> list[str]:
    if all_workspaces or len(workspace_ids) <= _PROJECT_LIST_WORKSPACE_FANOUT_LIMIT:
        return workspace_ids

    selected: list[str] = []
    seen: set[str] = set()

    preferred = str(session_workspace_id or "").strip()
    if preferred and preferred in workspace_ids:
        selected.append(preferred)
        seen.add(preferred)

    for ws_id in workspace_ids:
        if ws_id in seen:
            continue
        selected.append(ws_id)
        seen.add(ws_id)
        if len(selected) >= _PROJECT_LIST_WORKSPACE_FANOUT_LIMIT:
            break

    return selected


def _project_list_cache_file(session) -> str:  # noqa: ANN001
    account = normalize_account_for_cache(getattr(session, "login_username", None))
    if account:
        return str(SESSION_CACHE_DIR / f"project_list-{account}.json")
    return str(SESSION_CACHE_DIR / "project_list.json")


def _project_session_fingerprint(session) -> str:  # noqa: ANN001
    storage_state = getattr(session, "storage_state", None) or {}
    cookies = storage_state.get("cookies") if isinstance(storage_state, dict) else []
    payload = json.dumps(
        [
            {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain"),
                "path": c.get("path"),
            }
            for c in cookies or []
            if isinstance(c, dict)
        ],
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _project_list_cache_key(
    *,
    session,
    mode: str,
    all_workspaces: bool,
    workspace_ids: list[str],
) -> str:
    normalized_ws_ids = sorted(
        {str(ws_id).strip() for ws_id in workspace_ids if str(ws_id).strip()}
    )
    payload = {
        "mode": mode,
        "all_workspaces": all_workspaces,
        "workspace_ids": normalized_ws_ids,
        "workspace_id": getattr(session, "workspace_id", None),
        "base_url": getattr(session, "base_url", None),
        "session_fp": _project_session_fingerprint(session),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def _project_info_to_dict(project: browser_api_module.ProjectInfo) -> dict:
    return {
        "project_id": project.project_id,
        "name": project.name,
        "workspace_id": project.workspace_id,
        "workspace_ids": project.workspace_ids or [project.workspace_id],
        "budget": project.budget,
        "remain_budget": project.remain_budget,
        "member_remain_budget": project.member_remain_budget,
        "member_remain_gpu_hours": project.member_remain_gpu_hours,
        "gpu_limit": project.gpu_limit,
        "member_gpu_limit": project.member_gpu_limit,
        "priority_level": project.priority_level,
        "priority_name": project.priority_name,
    }


def _project_info_from_dict(data: dict) -> browser_api_module.ProjectInfo:
    def _float(value) -> float:  # noqa: ANN001
        if value is None or value == "":
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    return browser_api_module.ProjectInfo(
        project_id=str(data.get("project_id", "")),
        name=str(data.get("name", "")),
        workspace_id=str(data.get("workspace_id", "")),
        workspace_ids=[
            str(workspace_id)
            for workspace_id in data.get("workspace_ids", [])
            if str(workspace_id).strip()
        ]
        or [str(data.get("workspace_id", ""))],
        budget=_float(data.get("budget")),
        remain_budget=_float(data.get("remain_budget")),
        member_remain_budget=_float(data.get("member_remain_budget")),
        member_remain_gpu_hours=_float(data.get("member_remain_gpu_hours")),
        gpu_limit=bool(data.get("gpu_limit", False)),
        member_gpu_limit=bool(data.get("member_gpu_limit", False)),
        priority_level=str(data.get("priority_level", "")),
        priority_name=str(data.get("priority_name", "")),
    )


def _load_project_cache(
    *,
    session,
    mode: str,
    all_workspaces: bool,
    workspace_ids: list[str],
) -> list[browser_api_module.ProjectInfo] | None:
    cache_file = _project_list_cache_file(session)
    now = time.time()
    key = _project_list_cache_key(
        session=session,
        mode=mode,
        all_workspaces=all_workspaces,
        workspace_ids=workspace_ids,
    )
    try:
        with open(cache_file) as f:
            payload = json.load(f)
    except Exception:
        return None

    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return None
    entry = entries.get(key)
    if not isinstance(entry, dict):
        return None

    created_at = entry.get("created_at")
    if not isinstance(created_at, (int, float)):
        return None
    if (now - float(created_at)) > _PROJECT_LIST_CACHE_TTL_SECONDS:
        return None

    rows = entry.get("projects")
    if not isinstance(rows, list):
        return None
    projects: list[browser_api_module.ProjectInfo] = []
    try:
        for row in rows:
            if isinstance(row, dict):
                projects.append(_project_info_from_dict(row))
    except Exception:
        return None
    return projects


def _save_project_cache(
    *,
    session,
    mode: str,
    all_workspaces: bool,
    workspace_ids: list[str],
    projects: list[browser_api_module.ProjectInfo],
) -> None:
    cache_file = _project_list_cache_file(session)
    now = time.time()
    key = _project_list_cache_key(
        session=session,
        mode=mode,
        all_workspaces=all_workspaces,
        workspace_ids=workspace_ids,
    )

    payload: dict = {"version": 1, "entries": {}}
    try:
        with open(cache_file) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            payload = loaded
    except Exception:
        pass

    entries = payload.get("entries")
    if not isinstance(entries, dict):
        entries = {}

    # Drop stale entries before writing.
    fresh_entries: dict[str, dict] = {}
    for entry_key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        created_at = entry.get("created_at")
        if not isinstance(created_at, (int, float)):
            continue
        if (now - float(created_at)) <= (_PROJECT_LIST_CACHE_TTL_SECONDS * 2):
            fresh_entries[str(entry_key)] = entry

    fresh_entries[key] = {
        "created_at": now,
        "projects": [_project_info_to_dict(project) for project in projects],
    }

    if len(fresh_entries) > _PROJECT_LIST_CACHE_MAX_ENTRIES:
        ordered = sorted(
            fresh_entries.items(),
            key=lambda item: float(item[1].get("created_at", 0.0)),
            reverse=True,
        )
        fresh_entries = dict(ordered[:_PROJECT_LIST_CACHE_MAX_ENTRIES])

    payload["version"] = 1
    payload["entries"] = fresh_entries

    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        tmp = f"{cache_file}.tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, cache_file)
        try:
            os.chmod(cache_file, 0o600)
        except Exception:
            pass
    except Exception:
        return


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@click.command("list")
@click.option(
    "--all-workspaces",
    "all_workspaces",
    is_flag=True,
    default=True,
    help="Query all discovered workspaces (default, exhaustive).",
)
@pass_context
def list_projects_cmd(
    ctx: Context,
    all_workspaces: bool,
) -> None:
    """List projects and their GPU quota.

    \b
    Examples:
        inspire project list          # Show project quota table
        inspire --json project list   # JSON output with all fields
    """
    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint=(
            "Listing projects requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    try:
        workspace_ids = session.all_workspace_ids
        if workspace_ids is None:
            from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces

            candidates = [str(item.get("id") or "") for item in try_enumerate_workspaces(session)]
            candidates.append(str(getattr(session, "workspace_id", "") or ""))
            workspace_ids = _unique_workspace_ids(candidates)
        else:
            workspace_ids = _unique_workspace_ids(list(workspace_ids))
        if not workspace_ids:
            # No discovered workspaces — only default query path applies.
            projects = _load_project_cache(
                session=session,
                mode="default-query",
                all_workspaces=False,
                workspace_ids=[],
            )
            if projects is None:
                projects = browser_api_module.list_projects(session=session)
                _save_project_cache(
                    session=session,
                    mode="default-query",
                    all_workspaces=False,
                    workspace_ids=[],
                    projects=projects,
                )
        elif not all_workspaces:
            # API-side reduction: prefer a single default project-list query
            # before probing per-workspace endpoints.
            projects = _load_project_cache(
                session=session,
                mode="default-query",
                all_workspaces=False,
                workspace_ids=[],
            )
            if projects is None:
                default_query_error: Exception | None = None
                try:
                    projects = browser_api_module.list_projects(session=session)
                except Exception as exc:
                    projects = []
                    default_query_error = exc

                if projects:
                    _save_project_cache(
                        session=session,
                        mode="default-query",
                        all_workspaces=False,
                        workspace_ids=[],
                        projects=projects,
                    )
                else:
                    query_workspace_ids = _select_workspace_ids_for_listing(
                        workspace_ids,
                        session_workspace_id=getattr(session, "workspace_id", None),
                        all_workspaces=False,
                    )
                    projects = _load_project_cache(
                        session=session,
                        mode="limited-fanout",
                        all_workspaces=False,
                        workspace_ids=query_workspace_ids,
                    )
                    if projects is None:
                        projects, workspace_errors = _collect_workspace_projects(
                            query_workspace_ids,
                            session=session,
                        )
                        if projects:
                            _save_project_cache(
                                session=session,
                                mode="limited-fanout",
                                all_workspaces=False,
                                workspace_ids=query_workspace_ids,
                                projects=projects,
                            )
                        elif workspace_errors and default_query_error is not None:
                            error_samples = ", ".join(
                                f"{ws_id}: {message}" for ws_id, message in workspace_errors[:3]
                            )
                            if len(workspace_errors) > 3:
                                error_samples += ", ..."
                            raise ValueError(
                                f"Failed to list projects across configured workspaces "
                                f"({len(workspace_errors)} failed: {error_samples}); "
                                f"default query failed: {default_query_error}"
                            ) from default_query_error
        else:
            query_workspace_ids = _select_workspace_ids_for_listing(
                workspace_ids,
                session_workspace_id=getattr(session, "workspace_id", None),
                all_workspaces=all_workspaces,
            )
            projects = _load_project_cache(
                session=session,
                mode="all-workspaces-fanout",
                all_workspaces=True,
                workspace_ids=query_workspace_ids,
            )
            if projects is None:
                projects, workspace_errors = _collect_workspace_projects(
                    query_workspace_ids,
                    session=session,
                )
                if not projects and workspace_errors:
                    try:
                        projects = browser_api_module.list_projects(session=session)
                    except Exception as e:
                        error_samples = ", ".join(
                            f"{ws_id}: {message}" for ws_id, message in workspace_errors[:3]
                        )
                        if len(workspace_errors) > 3:
                            error_samples += ", ..."
                        raise ValueError(
                            f"Failed to list projects across configured workspaces "
                            f"({len(workspace_errors)} failed: {error_samples}); "
                            f"default query failed: {e}"
                        ) from e
                _save_project_cache(
                    session=session,
                    mode="all-workspaces-fanout",
                    all_workspaces=True,
                    workspace_ids=query_workspace_ids,
                    projects=projects,
                )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to list projects: {e}", EXIT_API_ERROR)
        return

    results = [_project_to_dict(p) for p in projects]

    if json_output:
        click.echo(json_formatter.format_json({"items": results, "total": len(results)}))
        return

    click.echo(human_formatter.format_project_list(results))


@click.command("use")
@click.argument("project_name")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(),
    help="Working directory for the project",
)
@pass_context
def use_project_cmd(
    ctx: Context,
    project_name: str,
    workdir: str | None,
) -> None:
    """Quickly set the active project.

    Sets the project as the first item in project_order, making it the
    default project for subsequent commands.

    \b
    Examples:
        inspire project use my-project
        inspire project use "CQ项目-科研驾驶舱"
        inspire project use my-project --workdir /path/to/workdir
    """
    from inspire.cli.utils.output import emit_success

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        projects = project_order_utils.fetch_current_account_projects(config)
        resolved = project_order_utils.resolve_project_ref(project_name, projects=projects)
        current_order = [
            entry
            for entry in project_order_utils.normalize_project_order(
                project_order_utils.read_project_order(),
                projects=projects,
            )
            if entry != resolved.project_id
        ]
        current_order.insert(0, resolved.project_id)
        project_path = project_order_utils.project_config_path()
        doc = project_order_utils.load_toml_doc(project_path)
        if "defaults" not in doc or not isinstance(doc["defaults"], dict):
            doc["defaults"] = tomlkit.table()
        doc["defaults"]["project_order"] = current_order

        if workdir:
            if "projects" not in doc:
                doc["projects"] = tomlkit.table()
            if resolved.project_id not in doc["projects"]:
                doc["projects"][resolved.project_id] = tomlkit.table()
            doc["projects"][resolved.project_id]["workdir"] = workdir

        project_path.parent.mkdir(parents=True, exist_ok=True)
        project_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

        emit_success(
            ctx,
            text=f'Project "{resolved.name or resolved.project_id}" is now active',
            payload={
                "project": resolved.project_id,
                "project_name": resolved.name,
                "workdir": workdir,
            },
        )

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


@click.command("init")
@click.argument("project_name", required=False)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(),
    default=".",
    help="Working directory for the project (default: current directory)",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Overwrite existing config if present",
)
@pass_context
def init_project_cmd(
    ctx: Context,
    project_name: str | None,
    workdir: str,
    force: bool,
) -> None:
    """Initialize a lightweight project config.

    Creates ./.inspire/config.toml with minimal configuration.
    Uses current directory name as project name if not specified.

    \b
    Examples:
        inspire project init                    # Use current dir name
        inspire project init my-project         # Specify project name
        inspire project init --workdir /path    # Set different workdir
        inspire project init --force            # Overwrite existing
    """
    from pathlib import Path
    import tomlkit
    from inspire.config import PROJECT_CONFIG_DIR
    from inspire.cli.utils.output import emit_success, emit_info

    try:
        # Auto-detect project name from directory if not provided
        if not project_name:
            project_name = Path.cwd().name
            emit_info(ctx, f'Using current directory name: "{project_name}"')

        project_path = Path.cwd() / PROJECT_CONFIG_DIR / "config.toml"

        # Check if config already exists
        if project_path.exists() and not force:
            _handle_error(
                ctx,
                "ConfigError",
                f"Project config already exists: {project_path}",
                EXIT_CONFIG_ERROR,
                hint="Use --force to overwrite",
            )
            return

        # Create fresh config
        doc = tomlkit.document()
        doc.add(tomlkit.comment("Inspire CLI Project Configuration"))
        doc.add(tomlkit.nl())

        # Add defaults section for future project-level defaults.
        doc["defaults"] = tomlkit.table()

        # Add project-specific workdir
        doc["projects"] = tomlkit.table()
        doc["projects"][project_name] = tomlkit.table()
        if workdir != ".":
            doc["projects"][project_name]["workdir"] = workdir
        else:
            doc["projects"][project_name]["workdir"] = str(Path.cwd())

        # Write config
        project_path.parent.mkdir(parents=True, exist_ok=True)
        project_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

        emit_success(
            ctx,
            text=f'Project "{project_name}" initialized',
            payload={
                "project": project_name,
                "config_path": str(project_path),
                "workdir": workdir if workdir != "." else str(Path.cwd()),
            },
        )

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
