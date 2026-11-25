"""Business logic for model deployment commands."""

from __future__ import annotations

from typing import Optional

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_TIMEOUT,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.logging_setup import redact_text
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.launch_preview import redact_structure
from inspire.config import Config
from inspire.platform.web.browser_api.inference_servings import (
    create_inference_serving,
    delete_inference_serving,
    get_inference_serving_detail,
    get_mirror_id_by_name,
    list_inference_servings,
    list_logic_compute_groups,
    resolve_default_serving_model,
    stop_inference_serving,
    wait_for_inference_serving_running,
)
import click

from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession


def _gpu_type_matches(requested: str, candidate: object) -> bool:
    requested_text = str(requested or "").strip()
    candidate_text = str(candidate or "").strip()
    return bool(
        requested_text
        and candidate_text
        and (requested_text in candidate_text or candidate_text in requested_text)
    )


def _with_canonical_gpu_type(
    spec: dict,
    *,
    requested_gpu_type: str,
    group_gpu_types: list,
) -> dict:
    if spec.get("gpu_type"):
        return dict(spec)
    enriched = dict(spec)
    for gpu_type in group_gpu_types:
        if _gpu_type_matches(requested_gpu_type, gpu_type):
            enriched["gpu_type"] = str(gpu_type)
            break
    return enriched


def _select_instance_spec(specs: list, gpu_count: int) -> Optional[dict]:
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        if spec.get("gpu_count", 0) == gpu_count:
            return spec

    for spec in specs:
        if not isinstance(spec, dict):
            continue
        if spec.get("gpu_count", 0) >= gpu_count:
            return spec

    return None


def _get_project_id_for_workspace(
    session: WebSession,
    workspace_id: str,
    project_name: Optional[str] = None,
) -> Optional[str]:
    """Get project ID for a workspace, optionally filtering by name.

    Args:
        session: Web session.
        workspace_id: Workspace ID.
        project_name: Optional project name to match (e.g., 'CQ项目-科研驾驶舱').

    Returns:
        Project ID if found, None otherwise.
    """
    from inspire.platform.web.browser_api.projects import list_projects

    try:
        projects = list_projects(workspace_id=workspace_id, session=session)
        if not projects:
            return None
        if project_name:
            for p in projects:
                if p.name == project_name:
                    return p.project_id
            # Partial match (e.g. "科研驾驶舱" matches "CQ项目-科研驾驶舱")
            for p in projects:
                if project_name in p.name:
                    return p.project_id
            return None
        # Return first project as default
        return projects[0].project_id
    except Exception:
        pass
    return None


def _find_compute_resources(
    session: WebSession,
    workspace_id: str,
    gpu_type: str,
    gpu_count: int,
) -> tuple[Optional[str], Optional[dict], int]:
    """Find logic compute group and instance spec for GPU requirements.

    Args:
        session: Web session.
        workspace_id: Workspace ID.
        gpu_type: GPU type (e.g., "NVIDIA_H200").
        gpu_count: Number of GPUs.

    Returns:
        Tuple of (logic_compute_group_id, spec_dict, available_gpus) or (None, None, 0).
        spec_dict contains: gpu_count, gpu_type, quota_id, memory_size_gib.
        available_gpus is the number of available GPUs in the group (may be negative if oversubscribed).
    """
    from inspire.platform.web.browser_api.availability.api import get_accurate_gpu_availability

    try:
        # Get GPU availability info
        availability_info = get_accurate_gpu_availability(
            workspace_id=workspace_id,
            session=session,
        )

        groups = list_logic_compute_groups(
            workspace_id=workspace_id,
            session=session,
        )

        candidates: list[tuple[int, str, dict]] = []
        for group in groups:
            # Check if this group supports the requested GPU
            # Partial match: "NVIDIA_H200" matches "NVIDIA_H200_SXM_141G"
            gpu_types = group.get("gpu_types", [])
            if not any(_gpu_type_matches(gpu_type, gt) for gt in gpu_types):
                continue

            logic_group_id = group.get("logic_compute_group_id", "")

            # Find available GPUs for this group and type
            available_gpus = 0
            for avail in availability_info:
                if avail.group_id == logic_group_id:
                    if _gpu_type_matches(gpu_type, avail.gpu_type) or len(gpu_types) == 1:
                        available_gpus = avail.available_gpus
                        break

            specs = group.get("instance_specs", [])
            spec = _select_instance_spec(specs, gpu_count)
            if spec is None:
                continue
            candidates.append(
                (
                    available_gpus,
                    logic_group_id,
                    _with_canonical_gpu_type(
                        spec,
                        requested_gpu_type=gpu_type,
                        group_gpu_types=gpu_types,
                    ),
                )
            )

        if candidates:
            candidates.sort(
                key=lambda item: (item[0] >= gpu_count, item[0]),
                reverse=True,
            )
            available_gpus, logic_group_id, spec = candidates[0]
            return logic_group_id, spec, available_gpus

    except Exception:
        pass

    return None, None, 0


def _resolve_serving_workspace_id(session: WebSession, config: Config) -> str:
    session_workspace_id = str(getattr(session, "workspace_id", "") or "").strip()
    if session_workspace_id and session_workspace_id != DEFAULT_WORKSPACE_ID:
        return session_workspace_id
    return str(config.default_workspace_id or "").strip()


def _build_serving_resource_spec_price(
    *,
    requested_gpu_type: str,
    requested_gpu_count: int,
    logic_compute_group_id: str,
    quota_id: str,
    spec: dict,
) -> dict:
    return {
        "gpu_type": spec.get("gpu_type") or requested_gpu_type,
        "gpu_count": spec.get("gpu_count") or requested_gpu_count,
        "memory_size_gib": spec.get("memory_size_gib", 1),
        "cpu_count": spec.get("cpu_count", 1),
        "logic_compute_group_id": logic_compute_group_id,
        "quota_id": quota_id,
    }


def _safe_serving_token(token: str, *, reveal_token: bool) -> str:
    if not token:
        return ""
    return token if reveal_token else "<redacted>"


def _safe_serving_endpoint(endpoint_url: str, *, reveal_token: bool) -> str:
    if not endpoint_url:
        return ""
    return endpoint_url if reveal_token else redact_text(endpoint_url)


def _infer_model_source(model_id: str) -> str:
    return "MODEL_PLAZA" if str(model_id or "").startswith("mp-") else "MODEL_HUB"


def _serving_status_payload(serving, *, reveal_token: bool) -> dict:  # noqa: ANN001
    return {
        "serving_id": serving.serving_id,
        "name": serving.name,
        "status": serving.status,
        "endpoint_url": _safe_serving_endpoint(
            serving.endpoint_url,
            reveal_token=reveal_token,
        ),
        "token": _safe_serving_token(serving.token, reveal_token=reveal_token),
        "gpu_type": serving.gpu_type,
        "gpu_count": serving.gpu_count,
        "created_at": serving.created_at,
        "available_replicas": serving.available_replicas,
        "node_name": serving.node_name,
    }


def _build_deploy_preview(
    *,
    name: str,
    image: str,
    command: str,
    port: int,
    gpu_type: str,
    gpu_count: int,
    replicas: int,
    project_name: Optional[str],
    available_gpus: int,
    workspace_id: str,
    project_id: str,
    mirror_id: str,
    logic_group_id: str,
    quota_id: str,
    model_id: str,
    model_version: object,
    model_source: str,
    resource_spec_price: dict,
) -> dict:
    """The would-be serving create, resolved and redacted, with no mutation performed."""
    warnings: list[str] = []
    if available_gpus < gpu_count:
        warnings.append(
            f"Only {available_gpus}x{gpu_type} available, but {gpu_count} requested. "
            "The serving may fail to schedule."
        )
    platform_request = {
        "name": name,
        "mirror_id": mirror_id,
        "command": command,
        "port": port,
        "gpu_type": gpu_type,
        "gpu_count": gpu_count,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "logic_compute_group_id": logic_group_id,
        "quota_id": quota_id,
        "replicas": replicas,
        "resource_spec_price": resource_spec_price,
        "model_source": model_source,
        "model_id": model_id,
        "model_version": model_version,
    }
    return {
        "kind": "model_serving",
        "mutation": "skipped",
        "intent": {
            "name": name,
            "image": image,
            "gpu_type": gpu_type,
            "gpu_count": gpu_count,
            "port": port,
            "replicas": replicas,
            "project": project_name,
        },
        "resolved": {
            "workspace_id": workspace_id,
            "project_id": project_id,
            "mirror_id": mirror_id,
            "logic_compute_group_id": logic_group_id,
            "quota_id": quota_id,
            "model_id": model_id,
            "model_version": model_version,
        },
        "platform_request": redact_structure(platform_request),
        "warnings": warnings,
    }


def _render_deploy_preview(preview: dict) -> None:
    intent = preview["intent"]
    resolved = preview["resolved"]
    click.echo("Model Serving Create Preview\n")
    click.echo(f"Name:       {intent['name']}")
    click.echo(f"Workspace:  {resolved['workspace_id']}")
    click.echo(f"Project:    {resolved['project_id']}")
    click.echo(f"Group:      {resolved['logic_compute_group_id']}")
    click.echo(f"Quota:      {resolved['quota_id']}")
    click.echo(f"Image:      {intent['image']}")
    click.echo(f"Request:    {intent['gpu_count']}x{intent['gpu_type']} port={intent['port']}")
    for warning in preview.get("warnings", []):
        click.echo(f"Warning:    {warning}")
    click.echo("Mutation:   skipped")
    click.echo("No serving was created.")


def run_model_deploy(
    ctx: Context,
    name: str,
    image: str,
    command: str,
    gpu_type: str,
    gpu_count: int,
    port: int,
    replicas: int,
    project_name: Optional[str],
    model_id: Optional[str],
    model_version: Optional[str],
    wait: bool,
    timeout: int,
    json_output: bool,
    session: WebSession,
    config: Config,
    reveal_token: bool = False,
    dry_run: bool = False,
) -> None:
    """Run the model deployment flow.

    Args:
        ctx: CLI context.
        name: Serving name.
        image: Image name.
        command: Launch command.
        gpu_type: GPU type.
        gpu_count: Number of GPUs.
        port: Service port.
        replicas: Number of replicas.
        project_name: Project name to deploy under.
        model_id: Explicit model metadata ID to send with the serving payload.
        model_version: Explicit model metadata version.
        wait: Whether to wait for serving to be ready.
        timeout: Timeout in seconds.
        json_output: Output as JSON.
        session: Web session.
        config: Configuration.
        reveal_token: Whether to print serving token fields without redaction.
    """
    # Get workspace ID
    workspace_id = _resolve_serving_workspace_id(session, config)
    if not workspace_id:
        _handle_error(
            ctx,
            "ConfigError",
            "No workspace configured. Set [defaults].workspace_id in config.",
            EXIT_CONFIG_ERROR,
            hint="Pass an explicit workspace or configure [defaults].workspace_id.",
        )
        return

    # Get project ID
    project_id = _get_project_id_for_workspace(session, workspace_id, project_name)
    if not project_id:
        _handle_error(
            ctx,
            "ConfigError",
            f"No project found for workspace {workspace_id}.",
            EXIT_CONFIG_ERROR,
            hint="Create a project in the web UI or specify --project.",
        )
        return

    # Lookup mirror ID from image name
    if not json_output:
        click.echo(f"Looking up image '{image}'...", err=True)

    mirror_id = get_mirror_id_by_name(
        image_name=image,
        source="all",
        workspace_id=workspace_id,
        session=session,
    )

    if not mirror_id:
        _handle_error(
            ctx,
            "ImageNotFound",
            f"Image '{image}' not found. Check the image name.",
            1,
            hint="Run 'inspire image list --source all' to see available images.",
        )
        return

    if not json_output:
        click.echo(f"Found mirror ID: {mirror_id}", err=True)

    # Find compute resources
    if not json_output:
        click.echo(f"Finding compute resources for {gpu_count}x{gpu_type}...", err=True)

    logic_group_id, spec, available_gpus = _find_compute_resources(
        session=session,
        workspace_id=workspace_id,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
    )

    if not logic_group_id or not spec:
        _handle_error(
            ctx,
            "ResourceNotFound",
            f"No compute resources found for {gpu_count}x{gpu_type}.",
            1,
            hint="Check available resources with 'inspire resources list'.",
        )
        return

    # Check GPU availability - warn if quota is exhausted
    if available_gpus < gpu_count:
        if not json_output:
            click.echo(
                f"Warning: Insufficient GPU quota. Requested {gpu_count}x{gpu_type}, "
                f"but only {available_gpus} GPUs available in project. "
                f"Deployment may fail or get gpu_count=0.",
                err=True,
            )

    quota_id = spec.get("quota_id", "")

    if not json_output:
        click.echo(
            f"Found compute group: {logic_group_id}, quota: {quota_id}",
            err=True,
        )
        # Warn about quota availability
        if available_gpus < gpu_count:
            click.echo(
                f"Warning: Only {available_gpus}x{gpu_type} available, "
                f"but {gpu_count}x requested. Deployment may fail!",
                err=True,
            )
        click.echo(f"Creating inference serving '{name}'...", err=True)

    # Build resource_spec_price with the exact spec fields the backend accepts.
    resource_spec_price = _build_serving_resource_spec_price(
        requested_gpu_type=gpu_type,
        requested_gpu_count=gpu_count,
        logic_compute_group_id=logic_group_id,
        quota_id=quota_id,
        spec=spec,
    )

    resolved_model_id = model_id or ""
    resolved_model_version: str = model_version or ""
    resolved_model_source = _infer_model_source(resolved_model_id) if resolved_model_id else ""
    if not resolved_model_id:
        model = resolve_default_serving_model(
            workspace_id=workspace_id,
            project_id=project_id,
            session=session,
        )
        if not model or not model.get("model_id"):
            _handle_error(
                ctx,
                "ModelNotFound",
                f"No serving models found for project {project_id!r}.",
                1,
                hint="Pass --model-id, or create/select a model for this project first.",
            )
            return
        resolved_model_id = str(model["model_id"])
        resolved_model_version = model.get("model_version") or ""
        resolved_model_source = str(
            model.get("model_source") or _infer_model_source(resolved_model_id)
        )

    # Create the serving
    if dry_run:
        preview = _build_deploy_preview(
            name=name,
            image=image,
            command=command,
            port=port,
            gpu_type=gpu_type,
            gpu_count=gpu_count,
            replicas=replicas,
            project_name=project_name,
            available_gpus=available_gpus,
            workspace_id=workspace_id,
            project_id=project_id,
            mirror_id=mirror_id,
            logic_group_id=logic_group_id,
            quota_id=quota_id,
            model_id=resolved_model_id,
            model_version=resolved_model_version,
            model_source=resolved_model_source,
            resource_spec_price=resource_spec_price,
        )
        if json_output:
            click.echo(json_formatter.format_json(preview))
        else:
            _render_deploy_preview(preview)
        return

    try:
        result = create_inference_serving(
            session=session,
            name=name,
            mirror_id=mirror_id,
            command=command,
            port=port,
            gpu_type=gpu_type,
            gpu_count=gpu_count,
            workspace_id=workspace_id,
            project_id=project_id,
            logic_compute_group_id=logic_group_id,
            quota_id=quota_id,
            replicas=replicas,
            resource_spec_price=resource_spec_price,
            model_source=resolved_model_source,
            model_id=resolved_model_id,
            model_version=resolved_model_version,
        )

        serving_id = result.get("inference_serving_id", "")

        if not serving_id:
            _handle_error(
                ctx,
                "APIError",
                "Failed to create serving: no serving ID returned.",
                EXIT_API_ERROR,
            )
            return

        if not json_output:
            click.echo(f"Created serving: {serving_id}", err=True)

        if wait:
            if not json_output:
                click.echo(f"Waiting for serving to be ready (timeout: {timeout}s)...", err=True)

            try:
                serving = wait_for_inference_serving_running(
                    serving_id=serving_id,
                    session=session,
                    timeout=timeout,
                )

                if json_output:
                    click.echo(
                        json_formatter.format_json(
                            _serving_status_payload(
                                serving,
                                reveal_token=reveal_token,
                            )
                        )
                    )
                else:
                    click.echo("Serving is ready!")
                    safe_endpoint = _safe_serving_endpoint(
                        serving.endpoint_url,
                        reveal_token=reveal_token,
                    )
                    safe_token = _safe_serving_token(
                        serving.token,
                        reveal_token=reveal_token,
                    )
                    if safe_endpoint:
                        click.echo(f"Endpoint: {safe_endpoint}")
                    if safe_token:
                        click.echo(f"Token: {safe_token}")

            except TimeoutError:
                _handle_error(
                    ctx,
                    "Timeout",
                    f"Serving did not become ready within {timeout}s.",
                    EXIT_TIMEOUT,
                    hint=f"Check status with 'inspire model list' or 'inspire model status {serving_id}'.",
                )
            except ValueError as e:
                _handle_error(
                    ctx,
                    "ServingFailed",
                    str(e),
                    EXIT_API_ERROR,
                )
        else:
            if json_output:
                click.echo(
                    json_formatter.format_json(
                        {
                            "serving_id": serving_id,
                            "status": "creating",
                        }
                    )
                )
            else:
                click.echo("Serving is being created.")
                click.echo("Check status with 'inspire model list'.")

    except ValueError as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to create serving: {e}",
            EXIT_API_ERROR,
        )


def run_model_list(
    ctx: Context,
    limit: int,
    all_projects: bool,
    json_output: bool,
    session: WebSession,
) -> None:
    """Run the model list flow.

    Args:
        ctx: CLI context.
        limit: Maximum number of servings to list.
        all_projects: List from all projects.
        json_output: Output as JSON.
        session: Web session.
    """
    try:
        servings = list_inference_servings(
            session=session,
            limit=limit,
        )

        if json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "items": [
                            {
                                "serving_id": s.serving_id,
                                "name": s.name,
                                "status": s.status,
                                "gpu_type": s.gpu_type,
                                "gpu_count": s.gpu_count,
                                "created_at": s.created_at,
                            }
                            for s in servings
                        ],
                        "total": len(servings),
                    }
                )
            )
        else:
            if not servings:
                click.echo("No model servings found.")
                return

            click.echo(f"{'ID':<20} {'Name':<20} {'Status':<12} {'Replicas':<10} {'Node'}")
            click.echo("-" * 80)
            for s in servings:
                repl_info = f"{s.available_replicas}/{s.replicas}" if s.replicas else "—"
                node_info = s.node_name[:20] if s.node_name else "—"
                click.echo(
                    f"{s.serving_id:<20} {s.name:<20} {s.status:<12} {repl_info:<10} {node_info}"
                )

    except ValueError as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to list servings: {e}",
            EXIT_API_ERROR,
        )
    except Exception as e:
        # A transport failure that escapes this handler leaves --json with nothing on stdout or stderr.
        _handle_error(
            ctx,
            "APIError",
            f"Failed to list servings: {e}",
            EXIT_API_ERROR,
        )


def run_model_stop(
    ctx: Context,
    serving_id: str,
    json_output: bool,
    session: WebSession,
) -> None:
    """Run the model stop flow.

    Args:
        ctx: CLI context.
        serving_id: Serving ID to stop.
        json_output: Output as JSON.
        session: Web session.
    """
    try:
        stop_inference_serving(serving_id=serving_id, session=session)

        if json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "serving_id": serving_id,
                        "status": "stopped",
                    }
                )
            )
        else:
            click.echo(f"Serving {serving_id} stopped.")

    except ValueError as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to stop serving: {e}",
            EXIT_API_ERROR,
        )


def _first_flag_value(tokens: list[str], flags: tuple[str, ...]) -> Optional[str]:
    """The value of the first token in *tokens* that carries one of *flags*, or None."""
    for index, token in enumerate(tokens):
        name, sep, inline_value = token.partition("=")
        if name not in flags:
            continue
        value = inline_value if sep else (tokens[index + 1] if index + 1 < len(tokens) else "")
        value = str(value or "").strip().strip("'\"")
        if value:
            return value
    return None


def _served_model_name(serving) -> str:  # noqa: ANN001
    """The model name a serving actually answers to on its OpenAI-compatible endpoint.

    The platform's create API takes `model_id` as metadata only; the served name is whatever the
    launch command declares, so read it from there and fall back to the serving's own name.
    """
    command = str(getattr(serving, "command", "") or "")
    tokens = command.split()
    # An explicit served name wins over a model path wherever the command puts them: a command can
    # carry both, and the path is only what the engine loads, not what the endpoint answers to.
    explicit = _first_flag_value(tokens, ("--served-model-name", "--served_model_name"))
    if explicit:
        # A served name is an opaque string clients ask for, not a path: 'zai-org/GLM-5.1-FP8' is
        # legitimate and must survive whole.
        return explicit
    derived = _first_flag_value(tokens, ("--model-path", "--model_path", "--model"))
    if derived:
        # A model path serves under its basename (`--model-path .../glm-5.1-fp8` -> glm-5.1-fp8).
        return derived.rstrip("/").rsplit("/", 1)[-1]
    return str(getattr(serving, "name", "") or "")


def _test_serving_endpoint(
    endpoint_url: str,
    api_key: str,
    model: str,
    prompt: str = "Hello, are you working?",
) -> dict:
    """Send a test inference request to the serving endpoint.

    Args:
        endpoint_url: The serving endpoint URL.
        api_key: Bearer API key.
        model: Model name to ask for; must be the one the serving serves.
        prompt: Test prompt.

    Returns:
        Response dict with keys: success, content, usage, error.
    """
    import urllib.request
    import urllib.error
    import json as _json

    url = f"{endpoint_url}/v1/chat/completions"
    req = urllib.request.Request(
        url,
        data=_json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = _json.loads(resp.read().decode("utf-8"))
            return {
                "success": True,
                "content": body.get("choices", [{}])[0].get("message", {}).get("content", ""),
                "usage": body.get("usage", {}),
            }
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8")
            return {"success": False, "error": redact_text(f"HTTP {e.code}: {error_body}")}
        except Exception:
            return {"success": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"success": False, "error": redact_text(str(e))}


def _run_serving_inference_test(
    ctx: Context,
    serving,  # noqa: ANN001
    *,
    json_output: bool,
) -> dict:
    """Ask the serving a question and report what came back.

    Returns an ``inference_test`` shaped dict. A test that could not be sent records why instead of
    vanishing: ``--test`` that silently sends nothing is the bug this closes (issue #66).
    """
    model = _served_model_name(serving)

    def record(result: dict) -> dict:
        return {"model": model, **result}

    if str(getattr(serving, "status", "") or "") != "RUNNING" or not serving.endpoint_url:
        return record(
            {
                "success": False,
                "attempted": False,
                "error": (
                    f"serving status is {getattr(serving, 'status', '') or 'unknown'} and "
                    "endpoint_url is "
                    f"{'set' if serving.endpoint_url else 'missing'}; a test needs a RUNNING "
                    "serving with an endpoint"
                ),
            }
        )

    from inspire.cli.utils.notebook_cli import load_config

    cfg = load_config(ctx)
    api_key = cfg.api_key if cfg else None
    if not api_key:
        message = "No API key available. Set auth.api_key in config or INSPIRE_API_KEY env var."
        if not json_output:
            click.echo(message, err=True)
        return record({"success": False, "attempted": False, "error": message})

    if not json_output:
        click.echo("\nRunning inference test...", err=True)
    result = record(_test_serving_endpoint(serving.endpoint_url, api_key, model))
    if not json_output:
        if result.get("success"):
            content = result.get("content", "")
            usage = result.get("usage", {})
            click.echo("\nInference test PASSED")
            click.echo(f"Response ({usage.get('completion_tokens', '?')} tokens):")
            click.echo(f"  {content[:300]}{'...' if len(content) > 300 else ''}")
        else:
            click.echo(f"\nInference test FAILED: {result.get('error')}", err=True)
    return result


def run_model_status(
    ctx: Context,
    serving_id: str,
    json_output: bool,
    session: WebSession,
    test_inference: bool = False,
    reveal_token: bool = False,
) -> None:
    """Run the model status flow.

    Args:
        ctx: CLI context.
        serving_id: Serving ID to check.
        json_output: Output as JSON.
        session: Web session.
        test_inference: If True, send a test inference request.
        reveal_token: Whether to print serving token fields without redaction.
    """
    try:
        serving = get_inference_serving_detail(serving_id=serving_id, session=session)

        if json_output:
            payload = _serving_status_payload(
                serving,
                reveal_token=reveal_token,
            )
            if test_inference:
                payload["inference_test"] = _run_serving_inference_test(
                    ctx, serving, json_output=True
                )
            click.echo(json_formatter.format_json(payload))
            return

        click.echo(f"Serving: {serving.name} ({serving.serving_id})")
        click.echo(f"Status: {serving.status}")
        if serving.gpu_count or serving.gpu_type:
            click.echo(f"GPU: {serving.gpu_count}x{serving.gpu_type}")
        if serving.node_name:
            click.echo(f"Node: {serving.node_name}")
        if serving.available_replicas:
            click.echo(f"Replicas: {serving.available_replicas}/{serving.replicas}")
        click.echo(f"Created: {serving.created_at}")
        if serving.endpoint_url:
            click.echo(
                "Endpoint: "
                + _safe_serving_endpoint(serving.endpoint_url, reveal_token=reveal_token)
            )
        if serving.token:
            click.echo("Token: " + _safe_serving_token(serving.token, reveal_token=reveal_token))

        if test_inference:
            _run_serving_inference_test(ctx, serving, json_output=False)

    except ValueError as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to get serving status: {e}",
            EXIT_API_ERROR,
        )


def run_model_delete(
    ctx: Context,
    serving_id: str,
    json_output: bool,
    session: WebSession,
) -> None:
    """Run the confirmed model delete flow.

    Args:
        ctx: CLI context.
        serving_id: Serving ID to delete.
        json_output: Output as JSON.
        session: Web session.
    """
    try:
        delete_inference_serving(serving_id=serving_id, session=session)

        if json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "serving_id": serving_id,
                        "status": "deleted",
                    }
                )
            )
        else:
            click.echo(f"Serving {serving_id} deleted.")

    except ValueError as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to delete serving: {e}",
            EXIT_API_ERROR,
        )
