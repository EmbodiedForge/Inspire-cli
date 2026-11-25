"""Job-related helpers for the Inspire OpenAPI client."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from inspire.platform.openapi.errors import (
    JobCreationError,
    JobNotFoundError,
    InspireAPIError,
    ValidationError,
    _translate_api_error,
    _validate_job_id_format,
)

logger = logging.getLogger(__name__)


def create_training_job_smart(
    api,  # noqa: ANN001
    *,
    name: str,
    command: str,
    resource: str,
    framework: str = "pytorch",
    prefer_location: Optional[str] = None,
    project_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    image: Optional[str] = None,
    task_priority: Optional[int] = None,
    instance_count: Optional[int] = None,
    max_running_time_ms: Optional[str] = None,
    shm_gi: Optional[int] = None,
    auto_fault_tolerance: bool = False,
    keep_alive_on_success: Optional[int] = None,
    keep_alive_on_fail: Optional[int] = None,
) -> Dict[str, Any]:
    """Create training job with smart resource matching."""
    api._check_authentication()

    # Validate required parameters
    api._validate_required_params(name=name, command=command, resource=resource)

    # Resolve workspace before resource matching so direct API callers do not
    # need to manually warm the workspace-scoped spec cache first.
    workspace_id = workspace_id or api.DEFAULT_WORKSPACE_ID

    try:
        api.resource_manager.ensure_specs_for_workspace(workspace_id)
    except RuntimeError as e:
        raise JobCreationError(
            f"Failed to load resource specs for workspace {workspace_id}: {e}"
        ) from e

    # Get recommended configuration
    try:
        spec_id, compute_group_id = api.resource_manager.get_recommended_config(
            resource, prefer_location, workspace_id=workspace_id
        )
    except ValueError as e:
        raise ValidationError(f"Resource configuration error: {str(e)}") from e

    # Use defaults for optional parameters
    project_id = project_id or api.DEFAULT_PROJECT_ID
    if task_priority is None:
        task_priority = api.DEFAULT_TASK_PRIORITY
    if instance_count is None:
        instance_count = api.DEFAULT_INSTANCE_COUNT
    max_running_time_ms = max_running_time_ms or api.DEFAULT_MAX_RUNNING_TIME

    # Set default shared memory size
    if shm_gi is None:
        shm_gi = api.DEFAULT_SHM_SIZE

    # Image configuration
    final_image = image or api._get_default_image()

    framework_item = {
        "image_type": api.DEFAULT_IMAGE_TYPE,
        "image": final_image,
        "instance_count": instance_count,
        "spec_id": spec_id,
    }
    if shm_gi is not None:
        framework_item["shm_gi"] = shm_gi

    payload = {
        "name": name,
        "command": command,
        "framework": framework,
        "logic_compute_group_id": compute_group_id,
        "project_id": project_id,
        "workspace_id": workspace_id,
        "task_priority": task_priority,
        "max_running_time_ms": max_running_time_ms,
        "framework_config": [framework_item],
    }

    if auto_fault_tolerance:
        payload["auto_fault_tolerance"] = True
    if keep_alive_on_success is not None:
        payload["reserve_on_success_ms"] = str(keep_alive_on_success * 1000)
    if keep_alive_on_fail is not None:
        payload["reserve_on_fail_ms"] = str(keep_alive_on_fail * 1000)

    try:
        result = api._make_request("POST", api.endpoints.TRAIN_JOB_CREATE, payload)

        if result.get("code") == 0:
            job_id = result["data"].get("job_id")
            logger.info("🚀 Training job created successfully! Job ID: %s", job_id)
            return result

        error_code = result.get("code")
        error_msg = result.get("message", "Unknown error")

        # Check for invalid spec error and retry with refreshed specs
        if _is_invalid_spec_error(error_code, error_msg):
            logger.warning("Invalid spec_id detected, refreshing workspace specs...")
            try:
                api.resource_manager.refresh_workspace_specs(workspace_id)
                # Get new recommended config with refreshed specs
                spec_id, compute_group_id = api.resource_manager.get_recommended_config(
                    resource, prefer_location, workspace_id=workspace_id
                )
                # Update payload with new spec_id
                framework_item["spec_id"] = spec_id
                payload["framework_config"] = [framework_item]
                payload["logic_compute_group_id"] = compute_group_id

                # Retry the request
                result = api._make_request("POST", api.endpoints.TRAIN_JOB_CREATE, payload)

                if result.get("code") == 0:
                    job_id = result["data"].get("job_id")
                    logger.info("🚀 Training job created successfully! Job ID: %s", job_id)
                    return result

                # If retry still fails, raise error
                error_code = result.get("code")
                error_msg = result.get("message", "Unknown error")
                friendly_msg = _translate_api_error(error_code, error_msg)
                raise JobCreationError(f"Failed to create training job: {friendly_msg}")

            except RuntimeError as e:
                raise JobCreationError(
                    f"Failed to refresh resource specs for workspace {workspace_id}: {e}"
                ) from e

        friendly_msg = _translate_api_error(error_code, error_msg)
        raise JobCreationError(f"Failed to create training job: {friendly_msg}")

    except requests.exceptions.RequestException as e:
        raise JobCreationError(f"Training job creation request failed: {str(e)}") from e


def _is_invalid_spec_error(error_code: int, error_msg: str) -> bool:
    """Check if API error indicates invalid spec_id."""
    # Common patterns for invalid spec/quota errors
    invalid_patterns = [
        "quota",
        "spec",
        "resource",
        "invalid",
        "not found",
        "not_exist",
        "not exist",
    ]
    msg_lower = error_msg.lower()
    return any(pattern in msg_lower for pattern in invalid_patterns)


def get_job_detail(api, job_id: str) -> Dict[str, Any]:  # noqa: ANN001
    """Get training job details."""
    api._check_authentication()
    api._validate_required_params(job_id=job_id)

    # Validate job ID format before making API call
    format_error = _validate_job_id_format(job_id)
    if format_error:
        raise JobNotFoundError(f"Invalid job ID '{job_id}': {format_error}")

    payload = {"job_id": job_id}
    result = api._make_request("POST", api.endpoints.TRAIN_JOB_DETAIL, payload)

    if result.get("code") == 0:
        logger.info("📋 Retrieved details for job %s", job_id)
        return result

    error_code = result.get("code")
    error_msg = result.get("message", "Unknown error")
    friendly_msg = _translate_api_error(error_code, error_msg)
    # Use specific exception for parameter errors (likely invalid job ID)
    if error_code == 100002:
        raise JobNotFoundError(f"Failed to get job details for '{job_id}': {friendly_msg}")
    raise InspireAPIError(f"Failed to get job details: {friendly_msg}")


def stop_training_job(api, job_id: str) -> bool:  # noqa: ANN001
    """Stop training job."""
    api._check_authentication()
    api._validate_required_params(job_id=job_id)

    # Validate job ID format before making API call
    format_error = _validate_job_id_format(job_id)
    if format_error:
        raise JobNotFoundError(f"Invalid job ID '{job_id}': {format_error}")

    payload = {"job_id": job_id}
    result = api._make_request("POST", api.endpoints.TRAIN_JOB_STOP, payload)

    if result.get("code") == 0:
        logger.info("🛑 Training job %s stopped successfully.", job_id)
        return True

    error_code = result.get("code")
    error_msg = result.get("message", "Unknown error")
    friendly_msg = _translate_api_error(error_code, error_msg)
    if error_code == 100002:
        raise JobNotFoundError(f"Failed to stop job '{job_id}': {friendly_msg}")
    raise InspireAPIError(f"Failed to stop training job: {friendly_msg}")


__all__ = ["create_training_job_smart", "get_job_detail", "stop_training_job"]
