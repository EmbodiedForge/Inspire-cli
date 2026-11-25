"""Notebook subcommands."""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path
from typing import Optional

import click

from .notebook_create_flow import maybe_run_post_start, run_notebook_create
from .notebook_lookup import (
    _ZERO_WORKSPACE_ID,
    _list_notebooks_for_workspace,
    _normalize_notebook_id,
    _normalized_tunnel_notebook_ids,
    _resolve_notebook_id,
    _sort_notebook_items_by_tunnel_priority,
    _try_get_current_user_ids,
    _unique_workspace_ids,
)
from .notebook_presenters import (
    DEFAULT_NOTEBOOK_LIST_COLUMNS,
    _print_notebook_detail,
    _print_notebook_list,
    _public_notebook_list_items,
)
from .notebook_ssh_flow import run_notebook_ssh
from .notebook_exec_flow import run_attach_exec, run_detached_exec, run_streamed_exec

from inspire.platform.web.browser_api.playwright_notebooks import run_command_in_notebook
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_JOB_NOT_FOUND,
    EXIT_TIMEOUT,
    EXIT_USAGE_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.timestamps import format_timestamp_utc
from inspire.cli.utils.scheduling_diagnostics import (
    pending_project_slot_hint,
    project_quota_pending_hint,
)
from inspire.cli.utils.confirm import confirmed_or_error
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import (
    get_base_url,
    load_config,
    require_web_session,
    resolve_json_output,
)
from inspire.cli.utils.notebook_post_start import (
    NO_WAIT_POST_START_WARNING,
    POST_START_MODE_RUN,
    POST_START_MODE_START,
    resolve_notebook_post_start_spec,
)
from inspire.config import (
    ConfigError,
    build_runtime_export_commands,
    load_runtime_session_state_for_config,
    resolve_effective_remote_runtime,
)
from inspire.config.workspaces import WORKSPACE_ALIAS_HELP, select_workspace_id
from inspire.bridge.tunnel import load_tunnel_config
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import NotebookFailedError
from inspire.platform.web.browser_api.notebook_detach import validate_exec_id

DEFAULT_NOTEBOOK_LIST_LIMIT = 20

_NOTEBOOK_STATUS_ALIASES = {
    "RUNNING": "running",
    "NOTEBOOK_RUNNING": "running",
    "STARTING": "starting",
    "PENDING": "pending",
    "STOPPING": "stopping",
    "STOPPED": "stopped",
    "NOTEBOOK_STOPPED": "stopped",
    "FAILED": "failed",
    "ERROR": "failed",
    "NOTEBOOK_FAILED": "failed",
}
_NOTEBOOK_TERMINAL_STATUSES = {"stopped", "failed"}


def _normalize_notebook_status(status: object) -> str:
    raw = str(status or "").strip()
    if not raw:
        return "unknown"
    return _NOTEBOOK_STATUS_ALIASES.get(raw.upper(), raw.lower())


def _notebook_wait_target(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"running", "stopped", "terminal"}:
        raise click.BadParameter("Use one of: running, stopped, terminal.")
    return normalized


def _notebook_wait_matches(status: str, target: str) -> bool:
    if target == "terminal":
        return status in _NOTEBOOK_TERMINAL_STATUSES
    return status == target


def _with_normalized_created_at(notebook: dict) -> dict:
    """Answer a notebook detail with ``created_at`` in the CLI's one UTC ISO 8601 spelling.

    Unset sentinels (.NET's empty date, non-positive epochs) become null; a detail without
    the field keeps its shape.
    """
    if "created_at" not in notebook:
        return notebook
    shaped = dict(notebook)
    shaped["created_at"] = format_timestamp_utc(notebook.get("created_at"))
    return shaped


def _emit_notebook_wait_result(
    ctx: Context,
    *,
    notebook_id: str,
    target: str,
    raw_status: str,
    status: str,
    elapsed_seconds: int,
) -> None:
    payload = {
        "notebook_id": notebook_id,
        "target": target,
        "status": status,
        "raw_status": raw_status,
        "elapsed_seconds": elapsed_seconds,
    }
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(
        f"Notebook {notebook_id} reached {status} "
        f"(raw: {raw_status or 'unknown'}) in {elapsed_seconds}s."
    )


@click.command("create")
@click.option(
    "--name",
    "-n",
    help="Notebook name (auto-generated if omitted)",
)
@click.option(
    "--workspace",
    help=WORKSPACE_ALIAS_HELP,
)
@click.option(
    "--workspace-id",
    help="Workspace ID override (escape hatch; overrides auto-selection)",
)
@click.option(
    "--resource",
    "-r",
    default=None,
    help="Resource spec (e.g., 1xH200, 4xH100, 4CPU) (default from config [notebook].resource or [defaults].resource)",
)
@click.option(
    "--compute-group",
    default=None,
    help=(
        "Explicit compute group name or logic_compute_group_id override; run "
        "'inspire notebook offers' for the groups that can run a notebook"
    ),
)
@click.option(
    "--project",
    "-p",
    default=None,
    help="Project name or ID (default from config [notebook].project_id or [defaults].project_order)",
)
@click.option(
    "--image",
    "-i",
    default=None,
    help=(
        "Image name/URL (default from config [notebook].image or [defaults].image; prompts "
        "interactively if still omitted)"
    ),
)
@click.option(
    "--shm-size",
    type=int,
    default=None,
    help="Shared memory size in GB (default from config [notebook].shm_size or [defaults].shm_size, else 32)",
)
@click.option(
    "--auto-stop/--no-auto-stop",
    default=False,
    help="Auto-stop when idle",
)
@click.option(
    "--auto/--no-auto",
    default=True,
    help=(
        "Auto-select the group that fits the resource with the most free capacity, "
        "among groups the platform reports able to run a notebook; a group that only "
        "has quota rows is never selected, however much capacity it shows "
        "(default: auto)"
    ),
)
@click.option(
    "--wait/--no-wait",
    default=True,
    help=(
        "Wait for notebook to reach RUNNING status "
        "(default: enabled; still required when a post-start action is configured)"
    ),
)
@click.option(
    "--post-start",
    type=str,
    default=None,
    help="Post-start action after RUNNING: none or a shell command",
)
@click.option(
    "--post-start-script",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    default=None,
    help="Local shell script to upload and run in the notebook after RUNNING",
)
@click.option(
    "--post-start-mode",
    type=click.Choice([POST_START_MODE_START, POST_START_MODE_RUN], case_sensitive=False),
    default=None,
    help='Post-start mode: "start" backgrounds the action, "run" waits and checks exit status',
)
@click.option(
    "--priority",
    type=click.IntRange(1, 10),
    default=None,
    help="Task priority (1-10, default from config [notebook].priority or [defaults].priority or 6)",
)
@click.option(
    "--dataset",
    "datasets",
    multiple=True,
    metavar="CODE@VERSION",
    help=(
        "Dataset version to mount, repeatable: DATASET_CODE@VERSION. Mounted at "
        "/inspire/dataset/<code>/<version> and checked against the target workspace before the "
        "notebook is created. List versions with: inspire dataset versions DATASET_CODE"
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Resolve the launch and print the platform request (secrets redacted) without creating a notebook",
)
@pass_context
def create_notebook_cmd(
    ctx: Context,
    name: Optional[str],
    workspace: Optional[str],
    workspace_id: Optional[str],
    resource: Optional[str],
    compute_group: Optional[str],
    project: Optional[str],
    image: Optional[str],
    shm_size: Optional[int],
    auto_stop: bool,
    auto: bool,
    wait: bool,
    post_start: Optional[str],
    post_start_script: Optional[Path],
    post_start_mode: Optional[str],
    priority: Optional[int],
    datasets: tuple[str, ...] = (),
    dry_run: bool = False,
) -> None:
    """Create a new interactive notebook instance.

    \b
    Examples:
        inspire notebook create                     # Interactive mode, auto-select GPU
        inspire notebook create -r 1xH200           # 1 GPU H200
        inspire notebook create -r 4xH100 -n mytest # 4 GPUs H100
        inspire notebook create -r 4x               # 4 GPUs, auto-select type
        inspire notebook create -r 8x               # 8 GPUs (full node), auto-select type
        inspire notebook create -r 4CPU             # 4 CPUs
        inspire notebook create -r 1xH100 --shm-size 64  # With 64GB shared memory
        inspire notebook create --no-auto -r 1xH200 # Disable auto-select
        inspire notebook create --post-start 'bash /workspace/bootstrap.sh'
        inspire notebook create --post-start-script scripts/notebook_bootstrap.sh
        inspire notebook create --post-start none --no-wait
        inspire notebook create --priority 5        # Set task priority to 5
        inspire notebook create -r 1xH200 --dry-run # Preview without creating
        inspire notebook create --dataset PointWorld@v1        # Mount one dataset version
        inspire notebook create --dataset PointWorld@v1 --dataset OpenX@v3  # Mount two

    A --dataset DATASET_CODE@VERSION version is validated against the target workspace first and
    then mounted at /inspire/dataset/<code>/<version>. Creation stops without a notebook if the
    platform rejects any of them.
    """
    if post_start and post_start_script:
        raise click.UsageError("Use either --post-start or --post-start-script, not both.")

    json_output = resolve_json_output(ctx, False)

    project_explicit = bool(project)

    run_notebook_create(
        ctx,
        name=name,
        workspace=workspace,
        workspace_id=workspace_id,
        resource=resource,
        compute_group_name=compute_group,
        project=project,
        image=image,
        shm_size=shm_size,
        auto_stop=auto_stop,
        auto=auto,
        wait=wait,
        post_start=post_start,
        post_start_script=post_start_script,
        post_start_mode=post_start_mode,
        json_output=json_output,
        priority=priority,
        project_explicit=project_explicit,
        datasets=datasets,
        dry_run=dry_run,
    )


@click.command("stop")
@click.argument("notebook")
@click.option(
    "--yes",
    "--force",
    "yes",
    is_flag=True,
    help="Stop without an interactive confirmation (alias: --force)",
)
@pass_context
def stop_notebook_cmd(
    ctx: Context,
    notebook: str,
    yes: bool,
) -> None:
    """Stop a running notebook instance.

    \b
    Examples:
        inspire notebook stop abc123-def456
        inspire notebook stop abc123-def456 --yes
    """
    # Confirmation first: refusing costs nothing, while resolving the reference costs a lookup the
    # caller may never get to use.
    if not confirmed_or_error(ctx, assume_yes=yes, subject=f"stop notebook {notebook}"):
        raise click.Abort()

    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint=(
            "Stopping notebooks requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    base_url = get_base_url()
    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
    )

    try:
        result = browser_api_module.stop_notebook(notebook_id=notebook_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to stop notebook: {e}", EXIT_API_ERROR)
        return

    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "notebook_id": notebook_id,
                    "status": "stopping",
                    "result": result,
                }
            )
        )
        return

    click.echo(f"Notebook '{notebook_id}' is being stopped.")
    click.echo(f"Use 'inspire notebook status {notebook_id}' to check status.")


@click.command("delete")
@click.argument("notebook")
@click.option(
    "--yes",
    "--force/--no-force",
    "force",
    default=False,
    help="Stop first if needed and skip confirmation. (alias: --yes)",
)
@click.option(
    "--timeout",
    type=int,
    default=600,
    show_default=True,
    help="Timeout in seconds while waiting for a forced stop.",
)
@click.option(
    "--interval",
    type=int,
    default=5,
    show_default=True,
    help="Polling interval in seconds while waiting for a forced stop.",
)
@pass_context
def delete_notebook_cmd(
    ctx: Context,
    notebook: str,
    force: bool,
    timeout: int,
    interval: int,
) -> None:
    """Delete a stopped notebook instance.

    \b
    Examples:
        inspire notebook delete abc123-def456 --force
    """
    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint=(
            "Deleting notebooks requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    base_url = get_base_url()
    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
    )

    stopped_first = False
    waited_for_stop = False
    try:
        detail = browser_api_module.get_notebook_detail(notebook_id=notebook_id, session=session)
        status = str(detail.get("status") or "").upper()
        if status not in {"", "STOPPED", "FAILED", "ERROR", "DELETED"}:
            if not force:
                _handle_error(
                    ctx,
                    "ValidationError",
                    (
                        f"Notebook '{notebook}' is {status} and cannot be deleted directly. "
                        f"Use 'inspire notebook stop {notebook}' first or pass --force."
                    ),
                    EXIT_CONFIG_ERROR,
                )
                return
            browser_api_module.stop_notebook(notebook_id=notebook_id, session=session)
            stopped_first = True
            browser_api_module.wait_for_notebook_status(
                notebook_id=notebook_id,
                target_statuses={"STOPPED"},
                session=session,
                timeout=timeout,
                poll_interval=interval,
            )
            waited_for_stop = True

        if not force:
            if json_output:
                # --json is non-interactive, so confirmation has exactly one spelling here. Deleting on
                # the strength of --json alone left `notebook delete` and `model delete` with opposite
                # rules about what --force means.
                _handle_error(
                    ctx,
                    "ValidationError",
                    (
                        f"Refusing to delete notebook {notebook_id} without confirmation. "
                        "Pass --force to delete without an interactive prompt."
                    ),
                    EXIT_VALIDATION_ERROR,
                    hint="In a script or agent run, --force is the confirmation; leave it out to stop here.",
                )
                return
            click.confirm(f"Delete notebook {notebook_id}?", abort=True)

        result = browser_api_module.delete_notebook(notebook_id=notebook_id, session=session)
    except click.Abort:
        # The user said no. Turning that into APIError/13 makes a cancelled delete indistinguishable
        # from a server failure (issue #66); let click print 'Aborted!' and exit 1.
        raise
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to delete notebook: {e}", EXIT_API_ERROR)
        return

    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "notebook_id": notebook_id,
                    "status": "deleted",
                    "stopped_first": stopped_first,
                    "waited_for_stop": waited_for_stop,
                    "result": result,
                }
            )
        )
        return

    click.echo(f"Notebook '{notebook_id}' deleted.")


@click.command("start")
@click.argument("notebook")
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Wait for notebook to reach RUNNING status (still required for post-start actions)",
)
@click.option(
    "--post-start",
    type=str,
    default=None,
    help="Post-start action after RUNNING: none or a shell command",
)
@click.option(
    "--post-start-script",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    default=None,
    help="Local shell script to upload and run in the notebook after RUNNING",
)
@click.option(
    "--post-start-mode",
    type=click.Choice([POST_START_MODE_START, POST_START_MODE_RUN], case_sensitive=False),
    default=None,
    help='Post-start mode: "start" backgrounds the action, "run" waits and checks exit status',
)
@pass_context
def start_notebook_cmd(
    ctx: Context,
    notebook: str,
    wait: bool,
    post_start: Optional[str],
    post_start_script: Optional[Path],
    post_start_mode: Optional[str],
) -> None:
    """Start a stopped notebook instance.

    \b
    Examples:
        inspire notebook start <notebook-id>
        inspire notebook start ring-8h100-test
        inspire notebook start ring-8h100-test --wait
        inspire notebook start ring-8h100-test --post-start 'bash /workspace/bootstrap.sh'
        inspire notebook start ring-8h100-test --post-start-script scripts/notebook_bootstrap.sh
        inspire notebook start ring-8h100-test --post-start none
    """
    if post_start and post_start_script:
        raise click.UsageError("Use either --post-start or --post-start-script, not both.")

    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint=(
            "Starting notebooks requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    base_url = get_base_url()
    config = load_config(ctx)
    try:
        post_start_spec = resolve_notebook_post_start_spec(
            config=config,
            post_start=post_start,
            post_start_script=post_start_script,
            post_start_mode=post_start_mode,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_CONFIG_ERROR)
        return

    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
    )

    try:
        result = browser_api_module.start_notebook(notebook_id=notebook_id, session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to start notebook: {e}", EXIT_API_ERROR)
        return

    if not json_output:
        click.echo(f"Notebook '{notebook_id}' is being started.")

    notebook_detail = None
    if wait or post_start_spec is not None:
        if not wait and post_start_spec is not None and not json_output:
            click.echo(NO_WAIT_POST_START_WARNING, err=True)
        if not json_output:
            # Progress, not result: stderr per docs/CLI-CONTRACT.md, so stdout stays
            # pipeable while the wait runs.
            click.echo("Waiting for notebook to reach RUNNING status...", err=True)
        try:
            notebook_detail = browser_api_module.wait_for_notebook_running(
                notebook_id=notebook_id, session=session
            )
            if not json_output:
                click.echo("Notebook is now RUNNING.", err=True)
        except NotebookFailedError as e:
            _handle_error(
                ctx,
                "NotebookFailed",
                f"Notebook failed to start: {e}",
                EXIT_API_ERROR,
                hint=e.events or "Check Events tab in web UI for details.",
            )
            return
        except TimeoutError as e:
            _handle_error(
                ctx,
                "Timeout",
                f"Timed out waiting for notebook to reach RUNNING: {e}",
                EXIT_API_ERROR,
            )
            return

    if notebook_detail and post_start_spec is not None:
        quota = notebook_detail.get("quota") or {}
        gpu_count = quota.get("gpu_count", 0) or 0
        maybe_run_post_start(
            ctx,
            notebook_id=notebook_id,
            session=session,
            post_start_spec=post_start_spec,
            gpu_count=gpu_count,
            json_output=json_output,
        )

    if json_output:
        payload: dict = {
            "notebook_id": notebook_id,
            "status": "starting",
            "result": result,
        }
        if notebook_detail:
            # --wait already fetched the detail; reporting 'starting' after it returned was the payload
            # claiming less than the command knew (issue #66).
            payload["status"] = _normalize_notebook_status(
                notebook_detail.get("status") or "RUNNING"
            )
            # Same created_at spelling `notebook status` answers, not the platform's mixed
            # epoch/ISO forms.
            payload["notebook_detail"] = _with_normalized_created_at(notebook_detail)
        click.echo(json_formatter.format_json(payload))
        return

    click.echo(f"Use 'inspire notebook status {notebook_id}' to check status.")


def _notebook_project_quota_hint(
    notebook_detail: dict,
    *,
    session,
) -> str | None:
    """Read the platform project quota block for a PENDING notebook; best effort."""
    project = notebook_detail.get("project") or {}
    workspace = notebook_detail.get("workspace") or {}
    project_id = str(
        (project.get("id") if isinstance(project, dict) else None)
        or notebook_detail.get("project_id")
        or ""
    ).strip()
    workspace_id = str(
        (workspace.get("id") if isinstance(workspace, dict) else None)
        or notebook_detail.get("workspace_id")
        or ""
    ).strip()
    if not project_id or not workspace_id:
        return None
    try:
        payload = browser_api_module.get_project_quota_jobs(
            workspace_id=workspace_id,
            project_id=project_id,
            session=session,
        )
    except Exception:  # noqa: BLE001 - status stands without the quota view
        return None
    quota = payload.get("quota") if isinstance(payload, dict) else None
    project_name = project.get("name") if isinstance(project, dict) else None
    return project_quota_pending_hint(quota, project_name or notebook_detail.get("project_name"))


@click.command("status")
@click.argument("notebook")
@pass_context
def notebook_status(
    ctx: Context,
    notebook: str,
) -> None:
    """Get status of a notebook instance.

    \b
    Examples:
        inspire notebook status <notebook-id>
    """
    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint=(
            "Notebook status requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    base_url = get_base_url()

    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
    )

    try:
        from inspire.platform.web.browser_api.core import _request_json as browser_request

        data = browser_request(
            session,
            "GET",
            f"/api/v1/notebook/{notebook_id}",
            referer=f"{base_url}/jobs/interactiveModeling",
            timeout=30,
        )
    except ValueError as e:
        message = str(e)
        # v1 answered 404 for a deleted notebook; v2 answers HTTP 200 with
        # ResponseMetadata.Error ResourceNotFound, which the route table raises as ValueError.
        if "API returned 404" in message or "ResourceNotFound" in message:
            # A 404 is a deleted notebook, not a slow one. EXIT_TIMEOUT told a polling script to keep
            # polling an object that would never come back (issue #66).
            _handle_error(
                ctx,
                "NotFound",
                f"Notebook instance '{notebook_id}' not found",
                EXIT_JOB_NOT_FOUND,
            )
        else:
            _handle_error(ctx, "APIError", message, EXIT_API_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if data.get("code") == 0:
        notebook_detail = _with_normalized_created_at(data.get("data", {}))
        project = notebook_detail.get("project") or {}
        pending_hint = _notebook_project_quota_hint(notebook_detail, session=session)
        if pending_hint is None:
            pending_hint = pending_project_slot_hint(
                notebook_detail.get("status"),
                (
                    project.get("name")
                    if isinstance(project, dict)
                    else notebook_detail.get("project_name")
                ),
            )
        events = ""
        if str(notebook_detail.get("status") or "").upper() in {"FAILED", "ERROR"}:
            events = browser_api_module.get_notebook_events(notebook_id, session=session)
            if events:
                notebook_detail = dict(notebook_detail)
                notebook_detail["events"] = events
        if json_output:
            if pending_hint:
                notebook_detail = {**notebook_detail, "pending_hint": pending_hint}
            click.echo(json_formatter.format_json(notebook_detail))
        else:
            _print_notebook_detail(notebook_detail)
            if pending_hint:
                click.secho(f"Pending note: {pending_hint}", fg="yellow", err=True)
            if events:
                click.echo("\nEvents:")
                click.echo(events)
        return

    _handle_error(
        ctx,
        "APIError",
        data.get("message", "Unknown error"),
        EXIT_API_ERROR,
    )
    return


@click.command("wait")
@click.argument("notebook")
@click.option(
    "--until",
    callback=lambda _ctx, _param, value: _notebook_wait_target(value),
    default="running",
    show_default=True,
    help="Target status: running, stopped, or terminal.",
)
@click.option("--timeout", type=int, default=600, show_default=True, help="Max wait seconds.")
@click.option("--interval", type=int, default=5, show_default=True, help="Poll interval seconds.")
@pass_context
def wait_notebook_cmd(
    ctx: Context,
    notebook: str,
    until: str,
    timeout: int,
    interval: int,
) -> None:
    """Wait for a notebook to reach a target status."""
    json_output = resolve_json_output(ctx, False)
    if timeout < 0:
        _handle_error(ctx, "ValidationError", "--timeout must be >= 0", EXIT_VALIDATION_ERROR)
        return
    if interval <= 0:
        _handle_error(ctx, "ValidationError", "--interval must be positive", EXIT_VALIDATION_ERROR)
        return

    session = require_web_session(
        ctx,
        hint=(
            "Notebook wait requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )
    base_url = get_base_url()
    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
    )

    start = time.time()
    if not json_output:
        # Progress goes to stderr (docs/CLI-CONTRACT.md); the final result line stays on
        # stdout so a pipe sees exactly the verdict.
        timeout_note = f"{timeout}s" if timeout else "no timeout"
        click.echo(
            f"Waiting for notebook {notebook_id} to reach {until} "
            f"(timeout: {timeout_note}, interval: {interval}s)",
            err=True,
        )
    last_reported_status: str | None = None
    while True:
        try:
            detail = browser_api_module.get_notebook_detail(
                notebook_id=notebook_id, session=session
            )
        except Exception as e:
            _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
            return
        raw_status = str(detail.get("status") or "")
        status = _normalize_notebook_status(raw_status)
        elapsed = int(time.time() - start)

        if _notebook_wait_matches(status, until):
            _emit_notebook_wait_result(
                ctx,
                notebook_id=notebook_id,
                target=until,
                raw_status=raw_status,
                status=status,
                elapsed_seconds=elapsed,
            )
            return

        if not json_output and status != last_reported_status:
            click.echo(
                f"Status: {status} (raw: {raw_status or 'unknown'}, {elapsed}s elapsed)",
                err=True,
            )
            last_reported_status = status

        if status in _NOTEBOOK_TERMINAL_STATUSES:
            _handle_error(
                ctx,
                "NotebookTerminal",
                (
                    f"Notebook '{notebook_id}' reached terminal status {status} "
                    f"(raw: {raw_status or 'unknown'}) before target {until}."
                ),
                EXIT_API_ERROR,
            )
            return

        if timeout and elapsed >= timeout:
            _handle_error(
                ctx,
                "Timeout",
                (
                    f"Notebook '{notebook_id}' did not reach {until} within {timeout}s "
                    f"(last status: {status}, raw: {raw_status or 'unknown'})."
                ),
                EXIT_TIMEOUT,
            )
            return
        time.sleep(interval)


@click.command("update")
@click.argument("notebook")
@click.option(
    "--payload-json",
    required=True,
    help="Full JSON object payload for PUT /notebook/{id}; omitted fields may reset.",
)
@pass_context
def update_notebook_cmd(
    ctx: Context,
    notebook: str,
    payload_json: str,
) -> None:
    """Update a notebook with a platform-shaped full-update payload."""
    json_output = resolve_json_output(ctx, False)
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as e:
        _handle_error(ctx, "ValidationError", f"Invalid JSON: {e}", EXIT_VALIDATION_ERROR)
        return
    if not isinstance(payload, dict):
        _handle_error(
            ctx, "ValidationError", "--payload-json must be a JSON object", EXIT_VALIDATION_ERROR
        )
        return

    session = require_web_session(
        ctx,
        hint=(
            "Notebook update requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )
    base_url = get_base_url()
    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
    )

    try:
        result = browser_api_module.update_notebook(
            notebook_id=notebook_id,
            payload=payload,
            session=session,
        )
    except Exception as e:  # noqa: BLE001
        _handle_error(ctx, "APIError", f"Failed to update notebook: {e}", EXIT_API_ERROR)
        return

    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "notebook_id": notebook_id,
                    "status": "updated",
                    "result": result,
                }
            )
        )
        return
    click.echo(f"Notebook '{notebook_id}' updated.")


@click.command("url")
@click.argument("notebook")
@click.option(
    "--kind",
    type=click.Choice(["lab", "code"], case_sensitive=False),
    default="lab",
    show_default=True,
    help="Launcher URL kind.",
)
@pass_context
def notebook_url_cmd(
    ctx: Context,
    notebook: str,
    kind: str,
) -> None:
    """Print a sanitized notebook launcher URL."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(
        ctx,
        hint=(
            "Notebook URL resolution requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )
    base_url = get_base_url()
    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook,
        json_output=json_output,
    )
    try:
        url = browser_api_module.get_notebook_launcher_url(notebook_id, kind=kind)
    except Exception as e:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return
    if json_output:
        click.echo(
            json_formatter.format_json({"notebook_id": notebook_id, "kind": kind, "url": url})
        )
        return
    click.echo(url)


@click.command("list")
@click.option(
    "--workspace",
    help=WORKSPACE_ALIAS_HELP,
)
@click.option(
    "--workspace-id",
    help="Workspace ID (defaults to configured workspace)",
)
@click.option(
    "--all",
    "-a",
    "show_all",
    is_flag=True,
    hidden=True,
    help=(
        "Deprecated and rejected: ListNotebooks is user-scoped and cannot "
        "return other users' notebooks"
    ),
)
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="List notebooks across all configured workspaces (cpu/gpu/internet)",
)
@click.option(
    "--limit",
    "-n",
    type=int,
    default=DEFAULT_NOTEBOOK_LIST_LIMIT,
    show_default=True,
    help="Max number of notebooks to show",
)
@click.option(
    "--tunneled",
    is_flag=True,
    help="Show only notebooks with active SSH tunnels",
)
@click.option(
    "--status",
    "-s",
    multiple=True,
    help="Filter by status (e.g. RUNNING, STOPPED). Repeatable.",
)
@click.option(
    "--name",
    "keyword",
    default="",
    help="Filter by notebook name (keyword search)",
)
@click.option(
    "--columns",
    "-c",
    default=DEFAULT_NOTEBOOK_LIST_COLUMNS,
    show_default=True,
    help="Comma-separated columns to display (name,status,resource,id,created,gpu,cpu,memory,image,project,workspace,node,uptime,tunnel)",
)
@pass_context
def list_notebooks(
    ctx: Context,
    workspace: Optional[str],
    workspace_id: Optional[str],
    show_all: bool,
    all_workspaces: bool,
    limit: int,
    status: tuple[str, ...],
    keyword: str,
    columns: str,
    tunneled: bool,
) -> None:
    """List your notebook/interactive instances.

    By default, shows recent notebooks with those having active tunnels listed first.
    Use --tunneled to show only notebooks with SSH tunnels.

    ``ListNotebooks`` is user-scoped. The deprecated ``--all`` flag is rejected
    with an UnsupportedOption error instead of silently returning only your own
    notebooks. The console's live all-user task view (``ListTaskDimension``) is
    a separate capability the CLI does not wrap yet.

    \b
    Examples:
        inspire notebook list
        inspire notebook list -n 10
        inspire notebook list --tunneled -n 10
        inspire notebook list -s RUNNING
        inspire notebook list -s RUNNING -s STOPPED
        inspire notebook list --name my-notebook
        inspire notebook list --workspace gpu -s RUNNING -n 5
        inspire notebook list --all-workspaces
        inspire --json notebook list
        inspire notebook list -c name,status,tunnel
        inspire notebook list -c name,status,gpu,uptime
    """
    json_output = resolve_json_output(ctx, False)

    if show_all:
        _handle_error(
            ctx,
            "UnsupportedOption",
            "--all is unsupported: ListNotebooks is user-scoped and cannot return other users' notebooks.",
            EXIT_USAGE_ERROR,
            hint=(
                "Use --all-workspaces for workspace scope. The all-user live task view "
                "is not wrapped by this command yet."
            ),
        )

    session = require_web_session(
        ctx,
        hint=(
            "Listing notebooks requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )
    config = load_config(ctx)

    workspace_ids: list[str] = []
    if workspace_id:
        workspace_ids = [workspace_id]
    elif workspace:
        try:
            resolved = select_workspace_id(
                config, explicit_workspace_name=workspace, session=session
            )
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return
        if resolved:
            workspace_ids = [resolved]
    elif all_workspaces:
        from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces

        workspace_ids = _unique_workspace_ids(
            [str(item.get("id") or "") for item in try_enumerate_workspaces(session)]
        )

    if not workspace_ids:
        resolved = (
            config.job_workspace_id
            or config.default_workspace_id
            or config.notebook_workspace_id
            or getattr(session, "workspace_id", None)
        )
        resolved = None if resolved == _ZERO_WORKSPACE_ID else resolved
        if not resolved:
            _handle_error(
                ctx,
                "ConfigError",
                "No workspace_id configured or provided.",
                EXIT_CONFIG_ERROR,
                hint="Use --workspace-id, --workspace, or [defaults].workspace_id.",
            )
            return
        workspace_ids = [str(resolved)]

    base_url = get_base_url()

    user_ids = [] if show_all else _try_get_current_user_ids(session, base_url=base_url)

    all_items: list[dict] = []
    successful_workspace_count = 0
    workspace_aliases: dict[str, str] = {}
    failed_workspaces: list[dict[str, str]] = []

    def _record_failed_workspace(ws_id: str, error: Exception) -> None:
        failed_workspaces.append(
            {
                "workspace_id": ws_id,
                "name": workspace_aliases.get(ws_id, ws_id),
                "error": str(error),
            }
        )
        if not ctx.json_output:
            click.echo(f"Warning: workspace {ws_id} failed: {error}", err=True)

    # When filtering by tunneled, fetch more to ensure we get enough after filtering
    fetch_limit = limit * 5 if tunneled else limit
    for ws_id in workspace_ids:
        status_filter = [s.upper() for s in status] if status else []
        try:
            items = _list_notebooks_for_workspace(
                session,
                base_url=base_url,
                workspace_id=ws_id,
                user_ids=user_ids,
                keyword=keyword,
                page_size=fetch_limit,
                status=status_filter,
            )
            successful_workspace_count += 1
            all_items.extend(items)
        except ValueError as e:
            if len(workspace_ids) == 1:
                _handle_error(
                    ctx,
                    "APIError",
                    str(e),
                    EXIT_API_ERROR,
                    hint="Check auth and proxy configuration.",
                )
                return
            _record_failed_workspace(ws_id, e)
            continue
        except Exception as e:
            if len(workspace_ids) == 1:
                _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
                return
            _record_failed_workspace(ws_id, e)
            continue

    if successful_workspace_count == 0 and len(workspace_ids) > 1:
        _handle_error(
            ctx,
            "APIError",
            "Failed to list notebooks from configured workspaces.",
            EXIT_API_ERROR,
        )
        return

    # Shape rows for output before any filtering or sorting, so the tunnel filter, the
    # priority sort, the JSON payload and the human table all see the same id/created_at
    # spelling (id = bare notebook UUID, platform primary key in internal_id).
    all_items = _public_notebook_list_items(all_items)

    # Filter and sort by tunnel status
    tunnel_config = load_tunnel_config()
    tunneled_ids = {
        bridge.notebook_id for bridge in tunnel_config.list_bridges() if bridge.notebook_id
    }
    # Bridge profiles store whichever spelling was resolved at creation (bare UUID or
    # nb-<uuid>); only the normalized comparison matches every spelling.
    normalized_tunneled_ids = _normalized_tunnel_notebook_ids(tunneled_ids)

    if tunneled:
        # Show only notebooks with tunnels
        all_items = [
            item
            for item in all_items
            if _normalize_notebook_id(item.get("notebook_id") or item.get("id", ""))
            in normalized_tunneled_ids
        ]
        # Sort by created_at descending (most recent first)
        all_items = sorted(
            all_items, key=lambda item: str(item.get("created_at") or ""), reverse=True
        )
        # Apply limit after filtering
        all_items = all_items[:limit]
    else:
        # Sort by tunnel priority (notebooks with tunnels first)
        all_items = _sort_notebook_items_by_tunnel_priority(all_items, tunneled_ids)
        all_items = all_items[:limit]

    if json_output and failed_workspaces:
        # Partial failure keeps rc=0, so the payload is the only place a caller can learn the listing
        # is incomplete. The presenter has no slot for it, so this one payload is assembled here with
        # the same items/total shape it emits.
        click.echo(
            json_formatter.format_json(
                {
                    "items": all_items,
                    "total": len(all_items),
                    "failed_workspaces": failed_workspaces,
                }
            )
        )
        return

    _print_notebook_list(all_items, json_output, columns=columns, tunnel_config=tunnel_config)


@click.command("ssh")
@click.argument("notebook")
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Wait for notebook to reach RUNNING status",
)
@click.option(
    "--pubkey",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help=(
        "SSH public key path to authorize (defaults to ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub)"
    ),
)
@click.option(
    "--save-as",
    help=(
        "Save this notebook tunnel as a named profile (usable with 'ssh <name>' after "
        "'inspire tunnel ssh-config --install')"
    ),
)
@click.option(
    "--port",
    default=31337,
    show_default=True,
    help="rtunnel server listen port inside notebook",
)
@click.option(
    "--ssh-port",
    default=22222,
    show_default=True,
    help="sshd port inside notebook",
)
@click.option(
    "--command",
    help="Optional remote command to run (if omitted, opens an interactive shell)",
)
@click.option(
    "--rtunnel-bin",
    help="Path to pre-cached rtunnel binary (e.g., /inspire/.../rtunnel)",
)
@click.option(
    "--rtunnel-upload-policy",
    type=click.Choice(["auto", "never", "always"], case_sensitive=False),
    default=None,
    help="Rtunnel upload fallback: auto (default), never, or always",
)
@click.option(
    "--debug-playwright",
    is_flag=True,
    help="Run browser automation with visible window for debugging",
)
@click.option(
    "--timeout",
    "setup_timeout",
    default=300,
    show_default=True,
    help="Timeout in seconds for rtunnel setup to complete",
)
@click.argument("ssh_command", nargs=-1, type=click.UNPROCESSED)
@pass_context
def ssh_notebook_cmd(
    ctx: Context,
    notebook: str,
    wait: bool,
    pubkey: Optional[str],
    save_as: Optional[str],
    port: int,
    ssh_port: int,
    command: Optional[str],
    rtunnel_bin: Optional[str],
    rtunnel_upload_policy: Optional[str],
    debug_playwright: bool,
    setup_timeout: int,
    ssh_command: tuple[str, ...],
) -> None:
    """SSH into a running notebook instance via rtunnel ProxyCommand.

    \b
    Examples:
        inspire notebook ssh abc123
        inspire notebook ssh abc123 --command "echo hello"
        inspire notebook ssh abc123 -- echo 'connected'
        inspire notebook ssh abc123 -- python train.py --epochs 100
    """
    if ssh_command and command:
        raise click.UsageError("Provide a remote command via --command or after '--', not both.")
    if ssh_command:
        command = shlex.join(ssh_command)
    run_notebook_ssh(
        ctx,
        notebook_id=notebook,
        wait=wait,
        pubkey=pubkey,
        save_as=save_as,
        port=port,
        ssh_port=ssh_port,
        command=command,
        rtunnel_bin=rtunnel_bin,
        rtunnel_upload_policy=rtunnel_upload_policy,
        debug_playwright=debug_playwright,
        setup_timeout=setup_timeout,
    )


#: What one `notebook exec` call is for, decided before anything is sent so that a combination with
#: no meaning is refused as itself instead of resolved by whichever branch came first. `run` starts
#: the command and watches it in one call, `detach` starts it and does not, `attach` watches one
#: that another call started.
EXEC_MODE_RUN = "run"
EXEC_MODE_DETACH = "detach"
EXEC_MODE_ATTACH = "attach"


def _refuse_usage(ctx: Context, message: str, hint: Optional[str] = None) -> None:
    """A usage-level rejection is a validation error (12), like every other refusal here."""
    _handle_error(ctx, "ValidationError", message, EXIT_VALIDATION_ERROR, hint=hint)


def _exec_watch_budget(ctx: Context, timeout: Optional[int]) -> Optional[int]:
    """How long this process is willing to watch: ``None`` is "until the exec is over".

    The answer comes from Click's parameter source rather than from the value, because the two
    questions are not the same question - "what number did the caller type" and "did the caller type
    one" - and only the second one decides whether this call leaves early. A ``default=60`` beside a
    call that now follows its command to the end would be a number this CLI never uses and a promise
    it no longer keeps, so the option carries no default and the source is where "not given" and
    "given" are told apart.
    """
    source = click.get_current_context().get_parameter_source("timeout")
    if source in (None, click.ParameterSource.DEFAULT):
        return None
    if timeout is None or int(timeout) < 1:
        _refuse_usage(
            ctx,
            f"--timeout {timeout} is not a span this process can watch for",
            hint=(
                "give a number of seconds, or leave --timeout out entirely to follow the exec until "
                "it ends. For work you would rather not wait for at all, --detach starts it and "
                "--attach <exec-id> reads it back."
            ),
        )
    return int(timeout) if timeout is not None else None


def _resolve_exec_usage(
    ctx: Context,
    *,
    command: tuple[str, ...],
    detach: bool,
    attach_id: Optional[str],
    after: int,
    follow: bool,
    completion_marker: Optional[str],
) -> Optional[tuple[str, Optional[str]]]:
    """The mode this call asked for, or None once the refusal has been reported.

    Each refusal names an option that would otherwise have been collected and silently ignored:
    ``--follow`` beside ``--detach`` has nothing to follow, and a command beside ``--attach`` would
    be dropped without a word, which is the failure mode this repo's surface ablation keeps finding.
    """
    if detach and attach_id is not None:
        _refuse_usage(
            ctx,
            "--detach and --attach are mutually exclusive: one starts an exec, the other reads one "
            "that already exists",
            hint="start it with --detach, then read the exec id it printed with --attach",
        )
        return None
    if attach_id is not None and not str(attach_id).strip():
        _refuse_usage(
            ctx,
            "--attach needs an exec id; an empty one names nothing",
            hint="the id is the one --detach printed, or `ls /tmp/inspire-exec` on the notebook",
        )
        return None
    if detach and follow:
        _refuse_usage(
            ctx,
            "--detach starts the exec and returns; --follow belongs to the --attach call that "
            "reads it",
            hint="drop --follow to detach it, or use --attach <exec-id> --follow to watch it",
        )
        return None
    if detach and after:
        _refuse_usage(
            ctx,
            f"--after {after} resumes a read of a record; --detach creates one and reads nothing",
            hint="use it with --attach <exec-id>",
        )
        return None
    if follow and attach_id is None:
        _refuse_usage(
            ctx,
            "--follow keeps reading a detached exec, so it needs --attach EXEC_ID",
            hint="start it with --detach, then follow it with --attach <exec-id> --follow",
        )
        return None
    if attach_id is None and after:
        _refuse_usage(
            ctx,
            f"--after {after} resumes a read of a detached exec's output, and nothing is being "
            "attached to",
            hint="use it with --attach <exec-id>",
        )
        return None
    if after < 0:
        _refuse_usage(ctx, "--after cannot be negative (it is a byte offset)")
        return None
    if completion_marker:
        _refuse_usage(
            ctx,
            "--completion-marker belongs to the terminal stream, and this verb runs the command off "
            "the terminal and watches its record files instead: there is no stream left to watch a "
            "marker appear in",
            hint=(
                "drop --completion-marker - the exec's own exit code is the completion signal, and "
                "this call already waits for it. For work you would rather not wait for, --detach "
                "starts it and --attach <exec-id> reads it back."
            ),
        )
        return None
    if attach_id is not None and command:
        _refuse_usage(
            ctx,
            "--attach reads a record and runs nothing, so the command after `--` would be dropped",
            hint="drop the command, or drop --attach to run it",
        )
        return None
    if not command:
        if detach:
            _refuse_usage(ctx, "--detach needs a command to start")
            return None
        if attach_id is None:
            _refuse_usage(
                ctx,
                "Provide a command after `--`, or read a detached exec with --attach EXEC_ID",
                hint="for work that must outlive the terminal, use --detach and then --attach",
            )
            return None
    if attach_id is not None:
        try:
            return EXEC_MODE_ATTACH, validate_exec_id(str(attach_id))
        except ValueError as exc:
            _refuse_usage(ctx, str(exc))
            return None
    return (EXEC_MODE_DETACH if detach else EXEC_MODE_RUN), None


# ignore_unknown_options=True prevents Click from intercepting --flag arguments
# that belong to the remote command (e.g. `python train.py --configs x=1`).
# Without this, Click raises "no such option: --configs" before the command
# is forwarded to the notebook. Same fix as bridge exec.
@click.command("exec", context_settings=dict(ignore_unknown_options=True))
@click.argument("command", nargs=-1, required=False, type=click.UNPROCESSED)
@click.option(
    "-b",
    "--bridge",
    "bridge_name",
    default=None,
    help="Bridge profile name (resolves to notebook ID from bridge config)",
)
@click.option(
    "--notebook",
    "-n",
    "notebook_id",
    default=None,
    help="Notebook ID or name (alternative to --bridge, uses WebSocket directly)",
)
@click.option(
    "--timeout",
    type=int,
    default=None,
    help="Seconds this process is willing to watch, in every mode that watches. Omit it (the "
    "default) and the call stays with the exec until it reaches a terminal state - a long command "
    "comes through this call without the caller needing to know any other flag exists. Give it and "
    "this process stops at that number, which leaves the exec running untouched and exits 124 with "
    "the exec id and a ready-made `--attach <id> --after N --follow` cursor. It never bounds the "
    "command, either way.",
)
@click.option(
    "--completion-marker",
    default=None,
    help="Refused (exit 12). It belonged to the terminal stream; the command now runs off that "
    "stream and its own status file is the completion signal, which this call already waits for.",
)
@click.option(
    "--detach",
    is_flag=True,
    help="Start the command detached from the terminal and return in seconds. It keeps running when "
    "this call ends or the terminal channel dies: its output goes to a record directory on the "
    "notebook (printed below), its exit status to <record>/status, and --attach reads both back.",
)
@click.option(
    "--attach",
    "attach_id",
    metavar="EXEC_ID",
    help="Read one detached exec's record from any process, however it died: new bytes of output go "
    "to stdout, the state (`running` or `exited <code>`) to stderr. Exits 124 while the exec is "
    "still running, carrying the exec id and the offset to resume from. Read-only.",
)
@click.option(
    "--after",
    type=int,
    default=0,
    show_default=True,
    metavar="N",
    help="With --attach: byte offset in the exec's output record to read from - the `offset` an "
    "earlier read reported.",
)
@click.option(
    "--follow",
    is_flag=True,
    help="With --attach: keep reading in a loop until the exec exits or --timeout expires. Each "
    "read is its own short call on its own terminal, so a channel that dies costs a retry.",
)
@pass_context
def exec_notebook_cmd(
    ctx: Context,
    command: tuple[str, ...],
    bridge_name: Optional[str],
    notebook_id: Optional[str],
    timeout: Optional[int],
    completion_marker: Optional[str],
    detach: bool,
    attach_id: Optional[str],
    after: int,
    follow: bool,
) -> None:
    """Execute a command in a notebook's Jupyter terminal via WebSocket.

    This command uses WebSocket to connect to the notebook's Jupyter terminal,
    so it does NOT require an SSH tunnel. Use --notebook (or -n) to target a
    notebook directly by ID or name, without needing a bridge profile.

    The terminal is a channel this client does not own: it closes on its own, measured at
    253-330s, and a command running in it is killed when the terminal is deleted. So no mode of
    this verb runs the command there. A plain exec starts it on the notebook's own filesystem and
    tails its output back from a byte offset, which makes a dead channel a reconnect instead of a
    lost command and follows the exec to its end unless --timeout says how long to watch; --detach
    starts the same work without watching it, and --attach reads it back from any later process.

    \b
    Examples:
        # Direct notebook targeting (no bridge needed):
        inspire notebook exec -n glm51-dl -- ls -la
        inspire notebook exec --notebook <notebook-id> -- "df -h /inspire"

    \b
        # Via bridge profile:
        inspire notebook exec -b my-bridge -- ls -la
        inspire notebook exec -b my-bridge -- "echo hello" --timeout 30

    \b
        # A long command: watched to the end by default, or bounded by this caller's patience:
        inspire notebook exec -n glm51-dl -- 'make -j8'
        inspire notebook exec -n glm51-dl --timeout 300 -- 'make -j8'  # 124 + cursor when it expires

    \b
        # Work that outlives this call:
        inspire notebook exec -n glm51-dl --detach -- 'python train.py --epochs 90'
        inspire notebook exec -n glm51-dl --attach nbx-0f2c1a9b7d3e4a51 --follow
        inspire notebook exec -n glm51-dl --attach nbx-0f2c1a9b7d3e4a51 --after 8192
    """
    # Resolved before anything else, because a caller who typed an unusable --timeout should hear
    # that rather than anything about the notebook it never reached.
    watch_budget = _exec_watch_budget(ctx, timeout)
    usage = _resolve_exec_usage(
        ctx,
        command=command,
        detach=detach,
        attach_id=attach_id,
        after=after,
        follow=follow,
        completion_marker=completion_marker,
    )
    if usage is None:
        return
    mode, exec_id = usage

    json_output = resolve_json_output(ctx, False)

    # Resolve notebook ID from either --notebook or --bridge. Name resolution
    # needs an authenticated session, so direct targets are finalized below.
    if notebook_id is not None:
        resolved_notebook_id = None
        via_bridge = None
    elif bridge_name is not None:
        # Resolve bridge name to notebook ID
        tunnel_config = load_tunnel_config()
        bridge_profile = tunnel_config.get_bridge(bridge_name)
        if bridge_profile is None:
            _handle_error(
                ctx,
                "ConfigError",
                f"Bridge profile '{bridge_name}' not found.",
                EXIT_CONFIG_ERROR,
                hint="Run 'inspire tunnel list' to see available bridges.",
            )
            return
        resolved_notebook_id = getattr(bridge_profile, "notebook_id", None)
        via_bridge = bridge_name
        if not resolved_notebook_id:
            _handle_error(
                ctx,
                "ConfigError",
                f"Bridge profile '{bridge_name}' has no notebook_id binding.",
                EXIT_CONFIG_ERROR,
                hint="Re-create the bridge with 'inspire notebook ssh <id> --save-as <name>'.",
            )
            return
    else:
        _handle_error(
            ctx,
            "ConfigError",
            "Must specify either --notebook/-n or --bridge/-b.",
            EXIT_CONFIG_ERROR,
            hint="Use --notebook <id-or-name> to target a notebook directly, "
            "or -b <bridge> if you have a bridge profile set up.",
        )
        return

    session = require_web_session(
        ctx,
        hint=(
            "Notebook exec requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    if notebook_id is not None:
        config = load_config(ctx)
        resolved_notebook_id, _ = _resolve_notebook_id(
            ctx,
            session=session,
            config=config,
            base_url=get_base_url(),
            identifier=notebook_id,
            json_output=json_output,
        )

    assert resolved_notebook_id is not None

    if mode == EXEC_MODE_ATTACH:
        # A report reads the notebook's files and runs nothing, so it loads no [remote_env] and no
        # resolved prefix: the prefix can hold secrets expanded from the local environment, and a
        # call that only reads has no reason to touch them. `job exec`'s report verbs draw the same
        # line, which is also what lets --attach work from a credential-only config.
        run_attach_exec(
            ctx,
            notebook_id=resolved_notebook_id,
            exec_id=str(exec_id),
            after=after,
            follow=follow,
            timeout=watch_budget,
            session=session,
            json_output=json_output,
            run_command_fn=run_command_in_notebook,
        )
        return

    # A single argument commonly represents an intentional shell expression,
    # e.g. ``-- "df -h /inspire"``.  Re-quoting it with ``shlex.join`` would
    # turn the whole expression into one executable name inside the notebook.
    full_command = command[0] if len(command) == 1 else shlex.join(command)

    # Direct execution must not be an exception to [remote_env]: the same command
    # resolving one way through ``bridge exec`` and another way here is a trap, because
    # the package index, PATH and every other override silently differ between the two.
    runtime_config = load_config(ctx)
    runtime = resolve_effective_remote_runtime(
        runtime_config,
        require_cwd=False,
        session_state=load_runtime_session_state_for_config(runtime_config),
    )
    export_commands = build_runtime_export_commands(runtime)
    # Only the command that runs carries the exports. What is echoed and what goes into the JSON
    # payload stays the user's own text: [remote_env] values may hold secrets (they expand "$VAR"
    # from the local environment), and those must not reach stdout, terminal scrollback or logs. The
    # two exec modes apply the same prefix the same way - in front of the script that launches the
    # work, so the work inherits it through its environment and no record file ever holds it.

    if mode == EXEC_MODE_DETACH:
        # The same [remote_env] as a plain exec, so a detached command sees the same package index
        # and PATH as the same command run inline. The prefix goes in front of the *launching* script
        # rather than into the record: the detached process inherits it through the environment, and
        # a file under /tmp would outlive this call holding a prefix that can carry secrets.
        if not json_output:
            target = f"(via bridge '{via_bridge}')" if via_bridge else "(direct)"
            click.echo(
                f"Detaching command in notebook '{resolved_notebook_id}' {target}...", err=True
            )
            click.echo(f"Command: {full_command}", err=True)
        run_detached_exec(
            ctx,
            notebook_id=resolved_notebook_id,
            command=full_command,
            export_commands=tuple(export_commands),
            session=session,
            timeout=watch_budget,
            json_output=json_output,
            run_command_fn=run_command_in_notebook,
        )
        return

    if not json_output:
        if via_bridge:
            click.echo(
                f"Executing command in notebook '{resolved_notebook_id}' (via bridge '{via_bridge}')...",
                err=True,
            )
        else:
            click.echo(
                f"Executing command in notebook '{resolved_notebook_id}' (direct)...", err=True
            )
        click.echo(f"Command: {full_command}", err=True)

    # The command never runs in the terminal that watches it. `run_streamed_exec` starts it on the
    # notebook's own filesystem - the same machinery `--detach` exposes - and tails the record from a
    # byte offset, so a channel that dies costs a reconnect and a `--timeout` that expires leaves the
    # exec running with the offset to resume from. Both used to be lies: the command came down with
    # the terminal either way (see notebook_detach's measurement). With no --timeout this call stays
    # until the exec ends, which is the whole point: a caller that never learned about --detach still
    # gets its long command through.
    run_streamed_exec(
        ctx,
        notebook_id=resolved_notebook_id,
        command=full_command,
        bridge=via_bridge,
        export_commands=tuple(export_commands),
        session=session,
        timeout=watch_budget,
        json_output=json_output,
        run_command_fn=run_command_in_notebook,
    )


__all__ = [
    "create_notebook_cmd",
    "exec_notebook_cmd",
    "list_notebooks",
    "notebook_status",
    "ssh_notebook_cmd",
    "start_notebook_cmd",
    "stop_notebook_cmd",
]
