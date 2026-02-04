"""Shared job creation pipeline used by `inspire job create` and `inspire run`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from inspire.cli.utils import job_submit
from inspire.config import Config


@dataclass(frozen=True)
class JobSubmission:
    job_id: Optional[str]
    data: dict
    result: Any
    log_path: Optional[str]
    wrapped_command: str
    max_time_ms: str


def submit_training_job(
    api,  # noqa: ANN001
    *,
    config: Config,
    name: str,
    command: str,
    resource: str,
    framework: str,
    location: Optional[str],
    project_id: str,
    workspace_id: str,
    image: Optional[str],
    priority: int,
    nodes: int,
    max_time_hours: float,
) -> JobSubmission:
    wrapped_command = job_submit.wrap_in_bash(command)
    final_command, log_path = job_submit.build_remote_logged_command(
        config, command=wrapped_command
    )

    max_time_ms = str(int(max_time_hours * 3600 * 1000))

    create_kwargs = dict(
        name=name,
        command=final_command,
        resource=resource,
        framework=framework,
        prefer_location=location,
        project_id=project_id,
        workspace_id=workspace_id,
        image=image,
        task_priority=priority,
        instance_count=nodes,
        max_running_time_ms=max_time_ms,
    )

    if config.shm_size is not None:
        shm_size = int(config.shm_size)
        if shm_size < 1:
            raise ValueError(
                "Shared memory size must be >= 1 (set INSPIRE_SHM_SIZE or job.shm_size)."
            )
        create_kwargs["shm_gi"] = shm_size

    result = api.create_training_job_smart(**create_kwargs)
    data = result.get("data", {}) if isinstance(result, dict) else {}
    job_id = data.get("job_id")

    if job_id:
        job_submit.cache_created_job(
            config,
            job_id=job_id,
            name=name,
            resource=resource,
            command=wrapped_command,
            log_path=log_path,
        )

    return JobSubmission(
        job_id=job_id,
        data=data,
        result=result,
        log_path=log_path,
        wrapped_command=wrapped_command,
        max_time_ms=max_time_ms,
    )
