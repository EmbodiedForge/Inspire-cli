"""Model serving subcommands for deploying inference services."""

from __future__ import annotations

import json
import shlex
import sys
import time
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_TIMEOUT,
    EXIT_USAGE_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.logging_setup import redact_text
from inspire.cli.utils.confirm import confirmed_or_error
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import (
    load_config,
    require_web_session,
    resolve_json_output,
)
from inspire.cli.utils.redaction import redact_named_fields
from inspire.cli.utils.resource_metrics_cli import (
    metrics_payload,
    metrics_text_lines,
    parse_metric_types,
)
from inspire.cli.commands.model.model_deploy_flow import (
    run_model_delete,
    run_model_deploy,
    run_model_list,
    run_model_stop,
)
from inspire.platform.web.browser_api import inference_servings as serving_api
from inspire.platform.web.browser_api import resource_metrics as resource_metrics_api

_SERVING_STATUS_ALIASES = {
    "RUNNING": "running",
    "SERVING_RUNNING": "running",
    "PENDING": "pending",
    "CREATING": "pending",
    "STARTING": "starting",
    "STOPPING": "stopping",
    "STOPPED": "stopped",
    "FAILED": "failed",
    "ERROR": "failed",
}
_SERVING_TERMINAL_STATUSES = {"stopped", "failed"}


def _normalize_serving_status(status: object) -> str:
    raw = str(status or "").strip()
    if not raw:
        return "unknown"
    return _SERVING_STATUS_ALIASES.get(raw.upper(), raw.lower())


def _serving_wait_target(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"running", "stopped", "terminal"}:
        raise click.BadParameter("Use one of: running, stopped, terminal.")
    return normalized


def _serving_wait_matches(status: str, target: str) -> bool:
    if target == "terminal":
        return status in _SERVING_TERMINAL_STATUSES
    return status == target


def _emit_serving_wait_result(
    ctx: Context,
    *,
    serving_id: str,
    target: str,
    raw_status: str,
    status: str,
    elapsed_seconds: int,
) -> None:
    payload = {
        "serving_id": serving_id,
        "target": target,
        "status": status,
        "raw_status": raw_status,
        "elapsed_seconds": elapsed_seconds,
    }
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(
        f"Serving {serving_id} reached {status} "
        f"(raw: {raw_status or 'unknown'}) in {elapsed_seconds}s."
    )


def _emit_model_payload(ctx: Context, payload: dict, *, text: str) -> None:
    safe_payload = _redact_secret_fields(payload)
    if ctx.json_output:
        click.echo(json_formatter.format_json(safe_payload))
        return
    click.echo(redact_text(text))


def _redact_secret_fields(value):  # noqa: ANN001
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(
                marker in key_text for marker in ("secret", "token", "api_key", "apikey", "key")
            ):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_secret_fields(item)
        return redacted
    if isinstance(value, list):
        return [_redact_secret_fields(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _log_entry_text(entry: dict) -> str:
    for key in ("log", "message", "content", "text"):
        value = entry.get(key)
        if value not in (None, ""):
            return redact_text(str(value))
    return redact_text(str(entry))


def _normalize_exec_command(command_parts: tuple[str, ...]) -> str:
    if not command_parts:
        raise click.UsageError("Provide a command to execute.")
    if len(command_parts) == 1:
        return command_parts[0]
    return shlex.join(command_parts)


def _emit_exec_result(ctx: Context, payload: dict) -> None:
    exit_code = payload.get("exit_code")
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
    else:
        stdout = str(payload.get("stdout") or "")
        if stdout:
            click.echo(stdout, nl=not stdout.endswith("\n"))
        if payload.get("truncated"):
            # Same ceiling as `job exec` (both run through the platform PTY exec path): the reader saw the
            # tail, and stderr is where that belongs so a pipe keeps only the command's own output.
            click.secho(
                "[output truncated: the above is the tail; the pod sent "
                f"{payload.get('bytes_total')} bytes in total]",
                fg="yellow",
                err=True,
            )
    if exit_code is None:
        sys.exit(124)
    if int(exit_code) != 0:
        sys.exit(int(exit_code))


@click.command("deploy")
@click.option(
    "--name",
    "-n",
    required=True,
    help="Serving name (unique identifier)",
)
@click.option(
    "--preset",
    type=click.Choice(["glm51"], case_sensitive=False),
    help="Use a preset deployment configuration (glm51 for GLM-5.1-FP8)",
)
@click.option(
    "--image",
    "-i",
    help="Image name (e.g., sglang:v0.5.10-cu130). Required unless using --preset.",
)
@click.option(
    "--command",
    "-c",
    help="Command to run the model server. Required unless using --preset.",
)
@click.option(
    "--gpu-type",
    help="GPU type (e.g., NVIDIA_H200, NVIDIA_H100). Required unless using --preset.",
)
@click.option(
    "--gpu-count",
    type=int,
    help="Number of GPUs (e.g., 4, 8). Required unless using --preset.",
)
@click.option(
    "--port",
    "-p",
    type=int,
    default=8000,
    help="Port for the inference service (default: 8000)",
)
@click.option(
    "--replicas",
    "-r",
    type=int,
    default=1,
    help="Number of replicas (default: 1)",
)
@click.option(
    "--project",
    "-P",
    "project_name",
    type=str,
    default=None,
    help="Project name (e.g., 'CQ项目-科研驾驶舱'). Uses first project if not specified.",
)
@click.option(
    "--model-id",
    help="Explicit model metadata ID to send with the serving create payload.",
)
@click.option(
    "--model-version",
    help="Explicit model metadata version; defaults to the platform default when omitted.",
)
@click.option(
    "--reveal-token",
    is_flag=True,
    help="Print serving token fields without redaction.",
)
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Wait for serving to be ready (default: --wait)",
)
@click.option(
    "--timeout",
    type=int,
    default=600,
    help="Timeout in seconds when waiting (default: 600)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Resolve the launch and print the platform request (secrets redacted) without creating a serving",
)
@pass_context
def deploy_model_cmd(
    ctx: Context,
    name: str,
    preset: Optional[str],
    image: Optional[str],
    command: Optional[str],
    gpu_type: Optional[str],
    gpu_count: Optional[int],
    port: int,
    replicas: int,
    project_name: Optional[str],
    model_id: Optional[str],
    model_version: Optional[str],
    reveal_token: bool,
    wait: bool,
    timeout: int,
    dry_run: bool = False,
) -> None:
    """Deploy a model inference serving.

    Deploy a model serving on inspire platform with specified GPU resources.

    \b
    Examples:
        # Deploy GLM-5.1-FP8 with preset (recommended):
        inspire model deploy --name glm5-serve --preset glm51 --project "CQ项目-科研驾驶舱"

        # Manual deployment with full control:
        inspire model deploy --name glm5-serve \\
            --image sglang:v0.5.10-cu130 \\
            --command "python -m sglang.launch_server --model-path zai-org/GLM-5.1-FP8 --tp 8 --port 8000" \\
            --gpu-type NVIDIA_H200 --gpu-count 8 \\
            --project "CQ项目-科研驾驶舱"

        inspire model deploy --name my-llm \\
            --image vllm:v0.5.0 \\
            --command "python -m vllm.entrypoints.openai.api_server --model ..." \\
            --gpu-type NVIDIA_H100 --gpu-count 4 --port 8080
    """
    json_output = resolve_json_output(ctx, False)

    # Handle preset configurations
    if preset:
        if preset.lower() == "glm51":
            image = image or "sglang:v0.5.10-cu130"
            gpu_type = gpu_type or "NVIDIA_H200"
            gpu_count = gpu_count or 8
            command = command or (
                "SGLANG_ENABLE_SPEC_V2=1 python -m sglang.launch_server "
                "--model-path /inspire/qb-ilm/project/cq-scientific-cooperation-zone/public/models/glm-5.1-fp8 "
                "--tp 8 --reasoning-parser glm45 --tool-call-parser glm47 "
                "--speculative-algorithm EAGLE --speculative-num-steps 3 "
                "--speculative-eagle-topk 1 --speculative-num-draft-tokens 4 "
                "--mem-fraction-static 0.85 --host 0.0.0.0 --port 8000"
            )
            if not json_output:
                click.echo(f"Using GLM-5.1 preset: {gpu_count}x{gpu_type}", err=True)

    # Validate required parameters
    if not image:
        _handle_error(
            ctx, "MissingOption", "--image is required (or use --preset)", EXIT_USAGE_ERROR
        )
        return
    if not command:
        _handle_error(
            ctx, "MissingOption", "--command is required (or use --preset)", EXIT_USAGE_ERROR
        )
        return
    if not gpu_type:
        _handle_error(
            ctx, "MissingOption", "--gpu-type is required (or use --preset)", EXIT_USAGE_ERROR
        )
        return
    if not gpu_count:
        _handle_error(
            ctx, "MissingOption", "--gpu-count is required (or use --preset)", EXIT_USAGE_ERROR
        )
        return

    session = require_web_session(
        ctx,
        hint="Model deployment requires web authentication.",
    )

    config = load_config(ctx)

    run_model_deploy(
        ctx=ctx,
        name=name,
        image=image,
        command=command,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        port=port,
        replicas=replicas,
        project_name=project_name,
        model_id=model_id,
        model_version=model_version,
        wait=wait,
        timeout=timeout,
        json_output=json_output,
        session=session,
        config=config,
        reveal_token=reveal_token,
        dry_run=dry_run,
    )


@click.command("list")
@click.option(
    "--limit",
    "-l",
    type=int,
    default=50,
    help="Maximum number of servings to list (default: 50)",
)
@click.option(
    "--all",
    "-a",
    is_flag=True,
    help="List servings from all projects",
)
@pass_context
def list_models_cmd(
    ctx: Context,
    limit: int,
    all: bool,
) -> None:
    """List model inference servings.

    \b
    Examples:
        inspire model list
        inspire model list --limit 20
        inspire model list --all
    """
    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint="Listing model servings requires web authentication.",
    )

    run_model_list(
        ctx=ctx,
        limit=limit,
        all_projects=all,
        json_output=json_output,
        session=session,
    )


@click.command("stop")
@click.argument("serving_id")
@click.option(
    "--yes",
    "--force",
    "yes",
    is_flag=True,
    help="Stop without an interactive confirmation (alias: --force)",
)
@pass_context
def stop_model_cmd(
    ctx: Context,
    serving_id: str,
    yes: bool,
) -> None:
    """Stop a model inference serving.

    \b
    Examples:
        inspire model stop serving-abc-123
        inspire model stop serving-abc-123 --yes
    """
    if not confirmed_or_error(ctx, assume_yes=yes, subject=f"stop serving {serving_id}"):
        raise click.Abort()

    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint="Stopping model serving requires web authentication.",
    )

    run_model_stop(
        ctx=ctx,
        serving_id=serving_id,
        json_output=json_output,
        session=session,
    )


@click.command("start")
@click.argument("serving_id")
@pass_context
def start_model_cmd(ctx: Context, serving_id: str) -> None:
    """Start a stopped model inference serving."""
    session = require_web_session(ctx, hint="Starting model serving requires web authentication.")
    try:
        payload = serving_api.start_inference_serving(serving_id, session=session)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return
    _emit_model_payload(
        ctx,
        {"serving_id": serving_id, "result": payload},
        text=f"Started serving: {serving_id}",
    )


@click.command("scale")
@click.argument("serving_id")
@click.option("--replicas", "-r", type=int, required=True, help="Target replica count.")
@click.option(
    "--yes",
    "--force",
    "yes",
    is_flag=True,
    help="Scale to zero without an interactive confirmation (alias: --force)",
)
@pass_context
def scale_model_cmd(ctx: Context, serving_id: str, replicas: int, yes: bool) -> None:
    """Scale replicas for a running model inference serving."""
    if replicas < 0:
        _handle_error(ctx, "ValidationError", "--replicas must be >= 0", EXIT_VALIDATION_ERROR)
        return
    if replicas == 0 and not confirmed_or_error(
        ctx,
        assume_yes=yes,
        subject=f"scale serving {serving_id} to zero replicas",
    ):
        raise click.Abort()
    session = require_web_session(ctx, hint="Scaling model serving requires web authentication.")
    try:
        payload = serving_api.scale_inference_serving(serving_id, replicas, session=session)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return
    _emit_model_payload(
        ctx,
        {"serving_id": serving_id, "replicas": replicas, "result": payload},
        text=f"Scaled serving {serving_id} to {replicas} replica(s)",
    )


@click.command("status")
@click.argument("serving_id")
@click.option(
    "--test/--no-test",
    default=False,
    help="Send a test inference request to the serving endpoint",
)
@click.option(
    "--reveal-token",
    is_flag=True,
    help="Print serving token fields without redaction.",
)
@pass_context
def status_model_cmd(
    ctx: Context,
    serving_id: str,
    test: bool,
    reveal_token: bool,
) -> None:
    """Get status of a model inference serving.

    \b
    Examples:
        inspire model status serving-abc-123
        inspire model status serving-abc-123 --test
    """
    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint="Getting model serving status requires web authentication.",
    )

    from inspire.cli.commands.model.model_deploy_flow import run_model_status

    run_model_status(
        ctx=ctx,
        serving_id=serving_id,
        json_output=json_output,
        session=session,
        test_inference=test,
        reveal_token=reveal_token,
    )


@click.command("wait")
@click.argument("serving_id")
@click.option(
    "--until",
    callback=lambda _ctx, _param, value: _serving_wait_target(value),
    default="running",
    show_default=True,
    help="Target status: running, stopped, or terminal.",
)
@click.option("--timeout", type=int, default=600, show_default=True, help="Max wait seconds.")
@click.option("--interval", type=int, default=10, show_default=True, help="Poll interval seconds.")
@pass_context
def wait_model_cmd(
    ctx: Context,
    serving_id: str,
    until: str,
    timeout: int,
    interval: int,
) -> None:
    """Wait for a model inference serving to reach a target status."""
    if timeout < 0:
        _handle_error(ctx, "ValidationError", "--timeout must be >= 0", EXIT_VALIDATION_ERROR)
        return
    if interval <= 0:
        _handle_error(ctx, "ValidationError", "--interval must be positive", EXIT_VALIDATION_ERROR)
        return

    session = require_web_session(
        ctx, hint="Waiting for model serving requires web authentication."
    )
    start = time.time()
    while True:
        try:
            serving = serving_api.get_inference_serving_detail(serving_id, session=session)
        except Exception as exc:  # noqa: BLE001
            _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
            return
        raw_status = str(serving.status or "")
        status = _normalize_serving_status(raw_status)
        elapsed = int(time.time() - start)

        if _serving_wait_matches(status, until):
            _emit_serving_wait_result(
                ctx,
                serving_id=serving.serving_id or serving_id,
                target=until,
                raw_status=raw_status,
                status=status,
                elapsed_seconds=elapsed,
            )
            return

        if status in _SERVING_TERMINAL_STATUSES:
            _handle_error(
                ctx,
                "ServingTerminal",
                (
                    f"Serving '{serving_id}' reached terminal status {status} "
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
                    f"Serving '{serving_id}' did not reach {until} within {timeout}s "
                    f"(last status: {status}, raw: {raw_status or 'unknown'})."
                ),
                EXIT_TIMEOUT,
            )
            return
        time.sleep(interval)


@click.command("metrics")
@click.argument("serving_id")
@click.option(
    "--metric",
    "metric_types",
    multiple=True,
    help="Metric type to fetch. Repeatable or comma-separated.",
)
@click.option("--interval", type=int, default=300, show_default=True, help="Sample interval.")
@click.option(
    "--version-index",
    type=int,
    default=-1,
    show_default=False,
    help="Zero-based serving version index; -1 selects active/latest.",
)
@click.option("--raw", is_flag=True, help="Return platform-shaped metric payloads in JSON.")
@click.option("--series", is_flag=True, help="Include full time series in summarized JSON/text.")
@pass_context
def metrics_model_cmd(
    ctx: Context,
    serving_id: str,
    metric_types: tuple[str, ...],
    interval: int,
    version_index: int,
    raw: bool,
    series: bool,
) -> None:
    """Fetch platform historical resource metrics for a model serving."""
    json_output = resolve_json_output(ctx, False)
    if interval <= 0:
        _handle_error(ctx, "ValidationError", "--interval must be positive", EXIT_VALIDATION_ERROR)
        return
    if version_index < -1:
        _handle_error(
            ctx,
            "ValidationError",
            "--version-index must be >= -1",
            EXIT_VALIDATION_ERROR,
        )
        return

    session = require_web_session(ctx, hint="Model metrics requires web authentication.")
    try:
        result = resource_metrics_api.get_inference_serving_resource_metrics(
            serving_id,
            metric_types=parse_metric_types(metric_types),
            interval_seconds=interval,
            version_index=version_index,
            raw=raw,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return

    if json_output or raw:
        click.echo(
            json_formatter.format_json(
                metrics_payload(
                    result,
                    id_key="serving_id",
                    raw=raw,
                    include_series=series,
                )
            )
        )
        return
    for line in metrics_text_lines(result, label="Serving", include_series=series):
        click.echo(line)


@click.command("instances")
@click.argument("serving_id")
@click.option("--page-size", type=int, default=100, show_default=True)
@pass_context
def instances_model_cmd(ctx: Context, serving_id: str, page_size: int) -> None:
    """List platform instances for a model inference serving."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Listing model instances requires web authentication.")
    try:
        payload = serving_api.list_inference_serving_instances(
            serving_id,
            page_size=page_size,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return
    if json_output:
        click.echo(json_formatter.format_json(_redact_secret_fields(payload)))
        return
    items = payload.get("items", []) if isinstance(payload, dict) else []
    if not items:
        click.echo(f"No instances found for {serving_id}.")
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        click.echo(
            f"{redact_text(str(item.get('name', '-'))):<54} "
            f"{redact_text(str(item.get('instance_status') or item.get('status') or '-')):<18} "
            f"{redact_text(str(item.get('node') or item.get('node_name') or '-'))}"
        )


@click.command("events")
@click.argument("serving_id")
@click.option("--page-size", type=int, default=50, show_default=True)
@pass_context
def events_model_cmd(ctx: Context, serving_id: str, page_size: int) -> None:
    """List platform events for a model inference serving."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Listing model events requires web authentication.")
    try:
        payload = serving_api.list_inference_serving_events(
            serving_id,
            page_size=page_size,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return
    if json_output:
        click.echo(json_formatter.format_json(_redact_secret_fields(payload)))
        return
    events = payload.get("events") or payload.get("items") or []
    if not events:
        click.echo(f"No events found for {serving_id}.")
        return
    for event in events:
        if isinstance(event, dict):
            click.echo(
                f"{event.get('time') or event.get('event_time') or '-'}  "
                f"{redact_text(str(event.get('reason') or event.get('type') or '-'))}  "
                f"{redact_text(str(event.get('message') or event.get('msg') or ''))}"
            )


@click.command("logs")
@click.argument("serving_id")
@click.option("--tail", "-n", type=int, default=100, show_default=True)
@click.option("--instance", "instance_name", help="Exact platform instance/pod name.")
@pass_context
def logs_model_cmd(
    ctx: Context,
    serving_id: str,
    tail: int,
    instance_name: str | None,
) -> None:
    """Fetch platform logs for a model inference serving."""
    json_output = resolve_json_output(ctx, False)
    if tail <= 0:
        _handle_error(ctx, "ValidationError", "--tail must be positive", EXIT_VALIDATION_ERROR)
        return
    session = require_web_session(ctx, hint="Fetching model logs requires web authentication.")
    try:
        payload = serving_api.get_inference_serving_logs(
            serving_id,
            page_size=tail,
            instance_name=instance_name,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return
    if json_output:
        click.echo(json_formatter.format_json(_redact_secret_fields(payload)))
        return
    logs = payload.get("logs", []) if isinstance(payload, dict) else []
    if not isinstance(logs, list) or not logs:
        click.echo(f"No platform logs found for {serving_id}.")
        return
    for entry in reversed(logs):
        click.echo(_log_entry_text(entry) if isinstance(entry, dict) else redact_text(str(entry)))


@click.command("exec", context_settings=dict(ignore_unknown_options=True))
@click.argument("serving_id")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@click.option("--instance", "instance_name", help="Exact platform instance/pod name.")
@click.option("--timeout", type=float, default=60.0, show_default=True, help="Timeout seconds.")
@pass_context
def exec_model_cmd(
    ctx: Context,
    serving_id: str,
    command: tuple[str, ...],
    instance_name: str | None,
    timeout: float,
) -> None:
    """Execute a non-interactive command in a running model serving container."""
    if timeout <= 0:
        _handle_error(ctx, "ValidationError", "--timeout must be positive", EXIT_VALIDATION_ERROR)
        return

    session = require_web_session(ctx, hint="Executing model command requires web authentication.")
    try:
        full_command = _normalize_exec_command(command)
        payload = serving_api.execute_inference_serving_command(
            serving_id,
            full_command,
            instance_name=instance_name,
            timeout=timeout,
            session=session,
        )
    except click.UsageError:
        raise
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "ExecError", str(exc), EXIT_API_ERROR)
        return

    payload.setdefault("command", full_command)
    _emit_exec_result(ctx, payload)


@click.command("versions")
@click.argument("serving_id")
@pass_context
def versions_model_cmd(ctx: Context, serving_id: str) -> None:
    """List versions for a model inference serving."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Listing model versions requires web authentication.")
    try:
        payload = serving_api.get_inference_serving_versions(serving_id, session=session)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return
    if json_output:
        click.echo(json_formatter.format_json(_redact_secret_fields(payload)))
        return
    click.echo(json.dumps(_redact_secret_fields(payload), ensure_ascii=False, indent=2))


@click.command("models")
@click.option("--workspace-id", default=None, help="Workspace ID. Defaults to current session.")
@click.option("--page-size", type=int, default=20, show_default=True)
@pass_context
def serving_models_cmd(ctx: Context, workspace_id: str | None, page_size: int) -> None:
    """List models available to the serving surface."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Listing serving models requires web authentication.")
    resolved_workspace_id = workspace_id or getattr(session, "workspace_id", None)
    if not resolved_workspace_id:
        _handle_error(ctx, "ValidationError", "--workspace-id is required", EXIT_VALIDATION_ERROR)
        return
    try:
        payload = serving_api.list_serving_models(
            workspace_id=resolved_workspace_id,
            page_size=page_size,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return
    if json_output:
        click.echo(json_formatter.format_json(_redact_secret_fields(payload)))
        return
    items = payload.get("items") or payload.get("models") or []
    if not items:
        click.echo("No serving models found.")
        return
    for item in items:
        if isinstance(item, dict):
            click.echo(
                f"{redact_text(str(item.get('model_id') or item.get('id') or '-'))}  "
                f"{redact_text(str(item.get('name') or '-'))}"
            )


@click.command("update")
@click.argument("serving_id")
@click.option(
    "--payload-json", required=True, help="JSON object payload for /inference_servings/update."
)
@pass_context
def update_model_cmd(ctx: Context, serving_id: str, payload_json: str) -> None:
    """Update a model inference serving with a platform-shaped JSON payload."""
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        _handle_error(ctx, "ValidationError", f"Invalid JSON: {exc}", EXIT_VALIDATION_ERROR)
        return
    if not isinstance(payload, dict):
        _handle_error(
            ctx, "ValidationError", "--payload-json must be a JSON object", EXIT_VALIDATION_ERROR
        )
        return
    payload.setdefault("inference_serving_id", serving_id)
    session = require_web_session(ctx, hint="Updating model serving requires web authentication.")
    try:
        result = serving_api.update_inference_serving(payload, session=session)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return
    _emit_model_payload(
        ctx,
        {"serving_id": serving_id, "result": result},
        text=f"Updated serving: {serving_id}",
    )


@click.command("validate-domain")
@click.argument("domain")
@click.option("--serving-id", default="new_inference_serving", show_default=True)
@pass_context
def validate_domain_model_cmd(ctx: Context, domain: str, serving_id: str) -> None:
    """Validate a custom-domain prefix for a serving."""
    session = require_web_session(
        ctx, hint="Validating serving domain requires web authentication."
    )
    try:
        payload = serving_api.validate_inference_serving_custom_domain(
            domain,
            serving_id=serving_id,
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return
    _emit_model_payload(ctx, payload, text=f"Domain available: {domain}")


@click.group("api-keys")
def api_keys_cmd() -> None:
    """List or inspect serving API keys."""


@api_keys_cmd.command("list")
@pass_context
def api_keys_list_cmd(ctx: Context) -> None:
    """List serving API keys with secret material redacted."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Listing API keys requires web authentication.")
    try:
        # `redact_named_fields`, not `_redact_secret_fields`: the rows carry `key_id` (the
        # argument `api-keys show` needs) beside `value` (the key material), and a
        # substring rule redacts the first while letting the second through.
        payload = redact_named_fields(serving_api.list_user_api_keys(session=session))
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return
    if json_output:
        click.echo(json_formatter.format_json(payload))
        return
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@api_keys_cmd.command("show")
@click.argument("key_id")
@click.option("--reveal", is_flag=True, help="Print backend API key fields without redaction.")
@pass_context
def api_keys_show_cmd(ctx: Context, key_id: str, reveal: bool) -> None:
    """Show one serving API key entry."""
    json_output = resolve_json_output(ctx, False)
    session = require_web_session(ctx, hint="Showing API key requires web authentication.")
    try:
        payload = serving_api.show_user_api_key(key_id, session=session)
    except Exception as exc:  # noqa: BLE001
        _handle_error(ctx, "APIError", str(exc), EXIT_API_ERROR)
        return
    safe_payload = payload if reveal else redact_named_fields(payload)
    if json_output:
        click.echo(json_formatter.format_json(safe_payload))
        return
    click.echo(json.dumps(safe_payload, ensure_ascii=False, indent=2))


@click.command("delete")
@click.argument("serving_id")
@click.option(
    "--yes",
    "--force/--no-force",
    "force",
    default=False,
    help="Force delete without confirmation (default: --no-force) (alias: --yes)",
)
@pass_context
def delete_model_cmd(
    ctx: Context,
    serving_id: str,
    force: bool,
) -> None:
    """Delete a model inference serving.

    \b
    Examples:
        inspire model delete serving-abc-123
        inspire model delete serving-abc-123 --force
    """
    if not confirmed_or_error(ctx, assume_yes=force, subject=f"delete serving {serving_id}"):
        raise click.Abort()

    json_output = resolve_json_output(ctx, False)

    session = require_web_session(
        ctx,
        hint="Deleting model serving requires web authentication.",
    )

    run_model_delete(
        ctx=ctx,
        serving_id=serving_id,
        json_output=json_output,
        session=session,
    )
