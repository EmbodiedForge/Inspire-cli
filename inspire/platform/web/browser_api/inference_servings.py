"""Browser (web-session) inference serving APIs (deploy, list, stop, delete)."""

from __future__ import annotations

import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _request_json,
)
from inspire.platform.web.browser_api.errors import ensure_browser_api_success
from inspire.platform.web.browser_api.ws_exec import execute_platform_ws_command
from inspire.platform.web.browser_api.notebooks import (
    _get_session_and_workspace_id,
)
from inspire.platform.web.session import WebSession, get_web_session

DEFAULT_STREAM_IDLE_TIMEOUT = 1800
DEFAULT_MODEL_CACHE_TTL_SECONDS = 24 * 3600
DEFAULT_MODEL_CACHE_PATH = Path.home() / ".cache" / "inspire-cli" / "serving_model_defaults.json"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class InferenceServingInfo:
    """Inference serving information."""

    serving_id: str
    name: str
    status: str  # PENDING, RUNNING, STOPPED, FAILED, etc.
    inference_serving_type: str  # CUSTOM, MODEL_PLAZA, etc.
    workspace_id: str
    project_id: str
    mirror_id: str
    command: str
    port: int
    gpu_count: int
    gpu_type: str
    replicas: int
    created_at: str
    endpoint_url: str = ""
    token: str = ""
    available_replicas: int = 0
    node_name: str = ""
    logic_compute_group_id: str = ""


def _serving_from_api(item: dict[str, Any]) -> InferenceServingInfo:
    """Convert an API serving dict to an InferenceServingInfo."""
    return InferenceServingInfo(
        serving_id=item.get("inference_serving_id", item.get("id", "")),
        name=item.get("name", ""),
        status=item.get("status", ""),
        inference_serving_type=item.get("inference_serving_type", ""),
        workspace_id=item.get("workspace_id", ""),
        project_id=item.get("project_id", ""),
        mirror_id=item.get("mirror_id", ""),
        command=item.get("command", ""),
        port=item.get("port", 0),
        gpu_count=item.get("gpu_count", 0),
        gpu_type=item.get("gpu_type", ""),
        replicas=item.get("replicas", 1),
        created_at=item.get("created_at", ""),
        endpoint_url=(
            item.get("extra_info", {}).get("service", "") or item.get("endpoint_url", "")
        ),
        token=item.get("token", ""),
        available_replicas=item.get("extra_info", {}).get("available_replicas", 0),
        node_name=", ".join(item.get("extra_info", {}).get("node_names", [])),
        logic_compute_group_id=item.get("logic_compute_group_id", ""),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inference_referer() -> str:
    """Build referer for inference serving requests."""
    return f"{_get_base_url()}/inference/serving"


def _request_inference_data(
    session: WebSession,
    method: str,
    endpoint_path: str,
    *,
    body: Optional[dict] = None,
    timeout: int = 30,
    default_data: Any = None,
    action: str = "Inference serving request",
) -> Any:
    """Make a request to the inference serving API."""
    data = _request_json(
        session,
        method,
        _browser_api_path(endpoint_path),
        referer=_inference_referer(),
        body=body,
        timeout=timeout,
    )

    ensure_browser_api_success(data, action=action)

    return data.get("data", default_data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_fallback_model_id(session: WebSession) -> str:
    """Get a valid model plaza model_id for use as placeholder metadata.

    The inference serving API requires model_id even for CUSTOM servings.
    We look up any available model from the model plaza and use its ID —
    the actual model served is controlled by the command, not model_id.
    Returns an empty string if no models are available (API may reject).
    """
    try:
        data = _request_json(
            session,
            "POST",
            _browser_api_path("/model_plaza/list"),
            referer=_inference_referer(),
            body={"page": 0, "page_size": 1},
            timeout=10,
        )
        items = data.get("data", {}).get("items", [])
        if items:
            return items[0].get("model_id", "")
    except Exception:
        pass
    return ""


def _default_model_cache_load() -> dict[str, Any]:
    try:
        data = json.loads(DEFAULT_MODEL_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _default_model_cache_store(cache: dict[str, Any]) -> None:
    try:
        DEFAULT_MODEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_MODEL_CACHE_PATH.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _default_model_cache_key(workspace_id: str, project_id: str) -> str:
    return f"{workspace_id or '_default_workspace'}:{project_id or '_default_project'}"


def _serving_model_item_model(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    model = item.get("model", item)
    return model if isinstance(model, dict) else None


def _serving_model_list_items(payload: dict[str, Any]) -> list[Any]:
    for key in ("list", "items", "models"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _serving_model_total(payload: dict[str, Any]) -> int | None:
    value = payload.get("total") or payload.get("total_count")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _infer_model_source(model_id: str) -> str:
    """Infer serving metadata source from the platform model identifier shape."""
    return "MODEL_PLAZA" if str(model_id or "").startswith("mp-") else "MODEL_HUB"


def resolve_default_serving_model(
    *,
    workspace_id: str,
    project_id: str = "",
    refresh: bool = False,
    session: Optional[WebSession] = None,
    max_pages: int = 25,
    page_size: int = 100,
) -> dict[str, Any] | None:
    """Resolve the first serving model owned by the selected project.

    The platform's model list endpoint is workspace-scoped on the request but
    can return records for other projects/workspaces. Filter client-side so a
    default model from one project is never silently reused for another.
    """
    if session is None:
        session = get_web_session()

    cache_key = _default_model_cache_key(workspace_id, project_id)
    cache = _default_model_cache_load()
    if not refresh:
        cached = cache.get(cache_key)
        if (
            isinstance(cached, dict)
            and cached.get("model_id")
            and time.time() - float(cached.get("fetched_at") or 0) < DEFAULT_MODEL_CACHE_TTL_SECONDS
        ):
            return {
                "model_id": str(cached["model_id"]),
                "model_version": cached.get("model_version", 0),
                "model_source": str(
                    cached.get("model_source") or _infer_model_source(str(cached["model_id"]))
                ),
                "name": str(cached.get("name") or ""),
                "project_id": str(cached.get("project_id") or project_id),
                "workspace_id": str(cached.get("workspace_id") or workspace_id),
            }

    picked: dict[str, Any] | None = None
    seen = 0
    for page in range(1, max_pages + 1):
        payload = list_serving_models(
            workspace_id=workspace_id,
            page=page,
            page_size=page_size,
            session=session,
        )
        items = _serving_model_list_items(payload)
        if not items:
            break

        for item in items:
            model = _serving_model_item_model(item)
            if model is None:
                continue
            model_project_id = str(model.get("project_id") or "")
            if not project_id or model_project_id == project_id:
                picked = model
                break

        seen += len(items)
        total = _serving_model_total(payload)
        if picked is not None or (total is not None and seen >= total):
            break

    if picked is None or not picked.get("model_id"):
        return None

    resolved = {
        "model_id": str(picked.get("model_id") or ""),
        "model_version": picked.get("version") or picked.get("model_version") or 0,
        "model_source": "MODEL_HUB",
        "name": str(picked.get("name") or ""),
        "project_id": str(picked.get("project_id") or ""),
        "workspace_id": str(picked.get("workspace_id") or workspace_id),
    }
    cache[cache_key] = {"fetched_at": int(time.time()), **resolved}
    _default_model_cache_store(cache)
    return resolved


# ---------------------------------------------------------------------------
# CRUD Operations
# ---------------------------------------------------------------------------


def create_inference_serving(
    name: str,
    mirror_id: str,
    command: str,
    port: int,
    gpu_type: str,
    gpu_count: int,
    workspace_id: str,
    project_id: str,
    logic_compute_group_id: str,
    quota_id: str,
    inference_serving_type: str = "CUSTOM",
    replicas: int = 1,
    node_num_per_replica: int = 1,
    model_source: str = "",
    model_id: str = "",
    model_version: str = "",
    cpu_count: int = 1,
    memory_size_gib: int = 1,
    description: str = "",
    instance_spec_id: str = "",
    resource_spec_price: Optional[dict[str, Any]] = None,
    stream_idle_timeout: int = DEFAULT_STREAM_IDLE_TIMEOUT,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Create a new inference serving.

    Args:
        name: Serving name.
        mirror_id: Docker image mirror ID.
        command: Launch command (e.g., sglang.launch_server ...).
        port: Exposed port.
        gpu_type: GPU type (e.g., "NVIDIA_H200", "NVIDIA_H100").
        gpu_count: Number of GPUs.
        workspace_id: Workspace ID.
        project_id: Project ID.
        logic_compute_group_id: Logic compute group ID.
        quota_id: Quota ID (from SCHEDULE_CONFIG_TYPE_SERVE resource prices).
        inference_serving_type: "CUSTOM" or "MODEL_PLAZA".
        replicas: Number of replicas.
        node_num_per_replica: Nodes per replica.
        model_source: Model source (omit if not using model plaza).
        model_id: Model ID (if using model plaza).
        model_version: Model version.
        cpu_count: CPU count for resource spec.
        memory_size_gib: Memory size in GiB for resource spec.
        description: Optional description.
        instance_spec_id: Instance specification ID (required top-level field).
        resource_spec_price: Pre-built resource spec price dict. If provided,
            its values override the individually-passed gpu/cpu/memory fields.
        stream_idle_timeout: Gateway idle timeout in seconds.
        session: Existing web session.

    Returns:
        API response data containing serving_id.
    """
    if session is None:
        session = get_web_session()

    # Pull values from resource_spec_price dict if provided, else from individual params
    rs = resource_spec_price if resource_spec_price else {}
    rs_gpu_type = rs.get("gpu_type", gpu_type)
    rs_gpu_count = rs.get("gpu_count", gpu_count)
    rs_memory = rs.get("memory_size_gib", memory_size_gib)
    rs_cpu = rs.get("cpu_count", cpu_count)
    rs_quota = rs.get("quota_id", quota_id)

    body: dict[str, Any] = {
        "name": name,
        "mirror_id": mirror_id,
        "command": command,
        "port": port,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "logic_compute_group_id": logic_compute_group_id,
        "inference_serving_type": inference_serving_type,
        "replicas": replicas,
        "node_num_per_replica": node_num_per_replica,
        "resource_spec_price": {
            # gpu_type/gpu_count/etc. nested per LcgResourceSpecPriceConfirmationReq
            "gpu_type": rs_gpu_type,
            "gpu_count": rs_gpu_count,
            "memory_size_gib": rs_memory,
            "cpu_count": rs_cpu,
            "logic_compute_group_id": logic_compute_group_id,
            "quota_id": rs_quota,
        },
        "traffic_config": {"stream_idle_timeout": stream_idle_timeout},
    }

    # The backend requires model_source + model_id + model_version for ALL serving types,
    # including CUSTOM. Use MODEL_PLAZA with a valid model_id (the actual served model
    # is determined by the command, not by model_id — model_id is metadata).
    if model_id:
        body["model_source"] = model_source or _infer_model_source(model_id)
        body["model_id"] = model_id
        body["model_version"] = model_version or "1"
    else:
        # Look up any available model plaza model to use as placeholder metadata.
        fallback = _get_fallback_model_id(session=session)
        body["model_source"] = "MODEL_PLAZA"
        body["model_id"] = fallback
        body["model_version"] = "1"

    if description:
        body["description"] = description

    return _request_inference_data(
        session,
        "POST",
        "/inference_servings/create",
        body=body,
        timeout=60,
        default_data={},
        action="Create inference serving",
    )


def list_inference_servings(
    workspace_id: Optional[str] = None,
    limit: int = 20,
    session: Optional[WebSession] = None,
) -> list[InferenceServingInfo]:
    """List inference servings.

    Args:
        workspace_id: Workspace ID for filtering.
        limit: Max number of servings to return.
        session: Existing web session.

    Returns:
        List of InferenceServingInfo objects.
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    body: dict[str, Any] = {
        "page": 0,
        "page_size": limit,
        "workspace_id": workspace_id,
        "filter_by": {},
    }

    data = _request_inference_data(
        session,
        "POST",
        "/inference_servings/list",
        body=body,
        timeout=30,
        default_data={},
        action="List inference servings",
    )
    items = data.get("inference_servings", [])
    return [_serving_from_api(item) for item in items]


def get_inference_serving_detail(
    serving_id: str,
    session: Optional[WebSession] = None,
) -> InferenceServingInfo:
    """Get detailed inference serving information.

    Args:
        serving_id: The serving ID to look up.
        session: Existing web session.

    Returns:
        InferenceServingInfo with full details including endpoint URL.
    """
    if session is None:
        session = get_web_session()

    data = _request_inference_data(
        session,
        "GET",
        f"/inference_servings/{serving_id}",
        timeout=30,
        default_data={},
        action="Get inference serving detail",
    )
    return _serving_from_api(data)


def stop_inference_serving(
    serving_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Stop an inference serving.

    Args:
        serving_id: ID of the serving to stop.
        session: Existing web session.

    Returns:
        API response data.
    """
    if session is None:
        session = get_web_session()

    return _request_inference_data(
        session,
        "POST",
        f"/inference_servings/{serving_id}/stop",
        timeout=30,
        default_data={},
        action="Stop inference serving",
    )


def start_inference_serving(
    serving_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Start (resume) a stopped inference serving.

    Args:
        serving_id: ID of the serving to start.
        session: Existing web session.

    Returns:
        API response data.
    """
    if session is None:
        session = get_web_session()

    return _request_inference_data(
        session,
        "POST",
        f"/inference_servings/{serving_id}/start",
        timeout=30,
        default_data={},
        action="Start inference serving",
    )


def scale_inference_serving(
    serving_id: str,
    replicas: int,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Scale a running inference serving."""
    if session is None:
        session = get_web_session()

    return _request_inference_data(
        session,
        "POST",
        "/inference_servings/scale",
        body={"inference_serving_id": serving_id, "replica": replicas},
        timeout=30,
        default_data={},
        action="Scale inference serving",
    )


def update_inference_serving(
    payload: dict[str, Any],
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Update an inference serving using a platform-shaped payload."""
    if session is None:
        session = get_web_session()

    return _request_inference_data(
        session,
        "POST",
        "/inference_servings/update",
        body=payload,
        timeout=60,
        default_data={},
        action="Update inference serving",
    )


def validate_inference_serving_custom_domain(
    custom_domain: str,
    serving_id: str = "new_inference_serving",
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Validate a serving custom-domain prefix."""
    if session is None:
        session = get_web_session()

    return _request_inference_data(
        session,
        "POST",
        f"/inference_servings/{serving_id}/validate_custom_domain",
        body={"custom_domain": custom_domain},
        timeout=30,
        default_data={},
        action="Validate inference serving custom domain",
    )


def delete_inference_serving(
    serving_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Delete an inference serving.

    Args:
        serving_id: ID of the serving to delete.
        session: Existing web session.

    Returns:
        API response data.
    """
    if session is None:
        session = get_web_session()

    return _request_inference_data(
        session,
        "DELETE",
        f"/inference_servings/{serving_id}",
        timeout=30,
        default_data={},
        action="Delete inference serving",
    )


def list_inference_serving_instances(
    serving_id: str,
    *,
    page: int = 1,
    page_size: int = 100,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """List instances for an inference serving."""
    if session is None:
        session = get_web_session()

    data = _request_inference_data(
        session,
        "POST",
        "/inference_servings/instances/list",
        body={"inference_serving_id": serving_id, "page": page, "page_size": page_size},
        timeout=30,
        default_data={},
        action="List inference serving instances",
    )
    return data if isinstance(data, dict) else {}


def list_inference_serving_events(
    serving_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """List events for an inference serving."""
    if session is None:
        session = get_web_session()

    data = _request_inference_data(
        session,
        "POST",
        "/inference_servings/events/list",
        body={
            "page": page,
            "page_size": page_size,
            "filter": {"object_type": 0, "object_ids": [serving_id]},
        },
        timeout=30,
        default_data={},
        action="List inference serving events",
    )
    return data if isinstance(data, dict) else {}


def _select_serving_instance(
    instances: list[dict[str, Any]],
    *,
    instance_name: str | None = None,
) -> dict[str, Any]:
    if instance_name:
        for item in instances:
            if item.get("name") == instance_name:
                return item
        available = ", ".join(str(item.get("name") or "") for item in instances)
        raise ValueError(f"Instance {instance_name!r} not found. Available instances: {available}")
    if not instances:
        raise ValueError("No inference serving instances found")
    return sorted(instances, key=lambda item: str(item.get("name") or ""))[0]


def _serving_instance_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("instance_id") or item.get("id") or "")


def _flatten_serving_instances(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]

    flattened: list[dict[str, Any]] = []
    groups = payload.get("groups") or payload.get("instance_groups") or []
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_items = group.get("items") or group.get("instances") or []
            if isinstance(group_items, list):
                flattened.extend(item for item in group_items if isinstance(item, dict))
    return flattened


def _is_running_serving_instance(item: dict[str, Any]) -> bool:
    status = str(item.get("instance_status") or item.get("status") or "").strip().lower()
    return status in {"running", "instance_running"}


def _select_running_serving_instance(
    instances: list[dict[str, Any]],
    *,
    instance_name: str | None,
) -> dict[str, Any]:
    if instance_name:
        for item in instances:
            if _serving_instance_name(item) == instance_name:
                return item
        available = ", ".join(_serving_instance_name(item) for item in instances)
        raise ValueError(f"Instance {instance_name!r} not found. Available instances: {available}")

    running = [item for item in instances if _is_running_serving_instance(item)]
    if not running:
        statuses = ", ".join(
            f"{_serving_instance_name(item) or '-'}:"
            f"{item.get('instance_status') or item.get('status') or '-'}"
            for item in instances
        )
        raise ValueError(
            "No RUNNING inference serving instances found"
            + (f". Available instances: {statuses}" if statuses else "")
        )
    return sorted(running, key=_serving_instance_name)[0]


def execute_inference_serving_command(
    serving_id: str,
    command: str,
    *,
    instance_name: str | None = None,
    timeout: float | None = 60.0,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Execute one command in a running inference serving instance via PTY WebSocket."""
    if session is None:
        session = get_web_session()

    instances_payload = list_inference_serving_instances(
        serving_id,
        page_size=100,
        session=session,
    )
    instances = _flatten_serving_instances(instances_payload)
    if not instances:
        raise ValueError(f"No instances found for serving {serving_id}")
    selected = _select_running_serving_instance(
        instances,
        instance_name=instance_name,
    )
    selected_name = _serving_instance_name(selected)
    if not selected_name:
        raise ValueError(f"Selected instance for serving {serving_id} has no name")

    result = execute_platform_ws_command(
        endpoint_path="/inference_servings/instances/exec",
        query={"inference_serving_id": serving_id, "instance_id": selected_name},
        instance_name=selected_name,
        command=command,
        timeout=timeout,
        session=session,
        # Kept bound to the command timeout on purpose: #50 decoupled the two for `job exec` only, so this
        # caller still gets its previous behaviour of one budget covering open + run.
        handshake_timeout=timeout,
    )
    result["serving_id"] = serving_id
    result["instance"] = selected
    return result


def get_inference_serving_logs(
    serving_id: str,
    *,
    page_size: int = 100,
    instance_name: str | None = None,
    sort: str = "descend",
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Fetch platform logs for one inference serving instance."""
    if session is None:
        session = get_web_session()

    instances_payload = list_inference_serving_instances(
        serving_id,
        page_size=100,
        session=session,
    )
    selected = _select_serving_instance(
        _flatten_serving_instances(instances_payload),
        instance_name=instance_name,
    )
    selected_name = str(selected.get("name") or "")
    if not selected_name:
        raise ValueError(f"Selected instance for serving {serving_id} has no name")

    data = _request_inference_data(
        session,
        "POST",
        "/logs/inference_serving",
        body={
            "inference_serving_id": serving_id,
            "page_size": page_size,
            "filter": {"podNames": [selected_name]},
            "sorter": [
                {"field": "time", "sort": sort},
                {"field": "log-id.keyword", "sort": sort},
            ],
        },
        timeout=60,
        default_data={},
        action="Get inference serving logs",
    )
    payload = data if isinstance(data, dict) else {}
    payload.setdefault("serving_id", serving_id)
    payload.setdefault("instance", selected)
    payload.setdefault("instance_name", selected_name)
    return payload


def get_inference_serving_versions(
    serving_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """List serving versions."""
    if session is None:
        session = get_web_session()

    data = _request_inference_data(
        session,
        "GET",
        f"/inference_servings/{serving_id}/versions",
        timeout=30,
        default_data={},
        action="List inference serving versions",
    )
    return data if isinstance(data, dict) else {}


def list_serving_models(
    *,
    workspace_id: str,
    page: int = 1,
    page_size: int = 20,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """List models available to the serving surface."""
    if session is None:
        session = get_web_session()

    data = _request_inference_data(
        session,
        "POST",
        "/model/list",
        body={"page": page, "page_size": page_size, "filter_by": {}, "workspace_id": workspace_id},
        timeout=30,
        default_data={},
        action="List serving models",
    )
    return data if isinstance(data, dict) else {}


def list_user_api_keys(session: Optional[WebSession] = None) -> dict[str, Any]:
    """List user API keys."""
    if session is None:
        session = get_web_session()

    data = _request_inference_data(
        session,
        "GET",
        "/user/my-api-key/list",
        timeout=30,
        default_data={},
        action="List user API keys",
    )
    return data if isinstance(data, dict) else {}


def show_user_api_key(
    key_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Fetch one user API key entry."""
    if session is None:
        session = get_web_session()

    data = _request_inference_data(
        session,
        "GET",
        f"/user/my-api-key/show/{key_id}",
        timeout=30,
        default_data={},
        action="Show user API key",
    )
    return data if isinstance(data, dict) else {}


def wait_for_inference_serving_running(
    serving_id: str,
    session: Optional[WebSession] = None,
    timeout: int = 600,
    poll_interval: int = 10,
) -> InferenceServingInfo:
    """Wait for an inference serving to reach RUNNING status.

    Args:
        serving_id: The serving ID to poll.
        session: Existing web session.
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between polls.

    Returns:
        InferenceServingInfo when running.

    Raises:
        TimeoutError: If the serving does not become running in time.
        ValueError: If the serving fails.
    """
    if session is None:
        session = get_web_session()

    start = time.time()
    last_status = None

    while True:
        serving = get_inference_serving_detail(serving_id=serving_id, session=session)
        status = (serving.status or "").upper()
        if status:
            last_status = status

        if status == "RUNNING":
            return serving

        if status == "FAILED":
            raise ValueError(f"Serving '{serving_id}' failed (status: {status})")

        if time.time() - start >= timeout:
            raise TimeoutError(
                f"Serving '{serving_id}' did not reach RUNNING within {timeout}s "
                f"(last status: {last_status or 'unknown'})"
            )

        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Helper: Get compute resources
# ---------------------------------------------------------------------------


def list_logic_compute_groups(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict[str, Any]]:
    """List available logic compute groups for inference.

    Uses the availability API to list groups, then fetches per-group
    GPU types and resource specs (quota IDs) for each GPU configuration.

    Args:
        workspace_id: Workspace ID.
        session: Existing web session.

    Returns:
        List of compute group dicts with id, name, gpu_types, instance_specs.
        Each instance_spec has gpu_count, gpu_type, quota_id, etc.
    """
    from inspire.platform.web.browser_api.availability.api import (
        list_compute_groups as _list_groups,
    )

    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id

    # Get base group list
    groups = _list_groups(workspace_id=workspace_id, session=session)

    results: list[dict[str, Any]] = []
    for group in groups:
        group_id = group.get("logic_compute_group_id", "")
        if not group_id:
            continue

        gpu_types: list[str] = []
        instance_specs: list[dict[str, Any]] = []

        for stat in group.get("gpu_type_stats") or []:
            gpu_info = stat.get("gpu_info") or {}
            gpu_type = str(gpu_info.get("gpu_type") or "").strip()
            if gpu_type:
                gpu_types.append(gpu_type)

        try:
            # Get resource spec prices (quota IDs per GPU count)
            prices_data = _request_json(
                session,
                "POST",
                _browser_api_path("/resource_prices/logic_compute_groups/"),
                referer=_inference_referer(),
                body={
                    "workspace_id": workspace_id,
                    "schedule_config_type": "SCHEDULE_CONFIG_TYPE_SERVE",
                    "logic_compute_group_id": group_id,
                },
                timeout=30,
            )
            spec_prices = prices_data.get("data", {}).get("lcg_resource_spec_prices", [])
            for spec in spec_prices:
                instance_specs.append(
                    {
                        "gpu_count": spec.get("gpu_count", 0),
                        "gpu_type": spec.get("gpu_info", {}).get("gpu_type", ""),
                        "quota_id": spec.get("quota_id", ""),
                        "instance_spec_id": spec.get("instance_spec_id", ""),
                        "memory_size_gib": spec.get("memory_size_gib", 0),
                        "price": spec.get("price", 0),
                        "price_info": spec.get("price_info", {}),
                        "cpu_count": spec.get("cpu_count", 1),
                    }
                )
        except Exception:
            pass

        enriched = dict(group)
        enriched["gpu_types"] = gpu_types
        enriched["instance_specs"] = instance_specs
        results.append(enriched)

    return results


def get_mirror_id_by_name(
    image_name: str,
    source: str = "public",
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> Optional[str]:
    """Look up mirror ID by image name.

    Performs exact match only. No fallback to other versions.

    Args:
        image_name: Image name to search for (e.g., "sglang:v0.5.10-cu130").
        source: "official", "public", or "all".
        workspace_id: Workspace ID.
        session: Existing web session.

    Returns:
        Mirror ID (image_id) if found, None otherwise.
    """
    from inspire.platform.web.browser_api.images import list_images_by_source

    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    sources = ["official", "public"] if source == "all" else [source]

    for src in sources:
        try:
            images = list_images_by_source(
                source=src,
                workspace_id=workspace_id,
                session=session,
            )
            for img in images:
                if img.name.lower() == image_name.lower():
                    return img.image_id
        except Exception:
            continue

    return None


__all__ = [
    "InferenceServingInfo",
    "create_inference_serving",
    "list_inference_servings",
    "get_inference_serving_detail",
    "get_inference_serving_logs",
    "get_inference_serving_versions",
    "stop_inference_serving",
    "start_inference_serving",
    "scale_inference_serving",
    "update_inference_serving",
    "validate_inference_serving_custom_domain",
    "delete_inference_serving",
    "execute_inference_serving_command",
    "list_inference_serving_events",
    "list_inference_serving_instances",
    "list_serving_models",
    "resolve_default_serving_model",
    "list_user_api_keys",
    "show_user_api_key",
    "wait_for_inference_serving_running",
    "list_logic_compute_groups",
    "get_mirror_id_by_name",
]
