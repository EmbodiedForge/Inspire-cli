"""Config options: Job."""

from __future__ import annotations

from inspire.config.schema_models import ConfigOption, _parse_int

JOB_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSP_PRIORITY",
        toml_key="job.priority",
        field_name="job_priority",
        description="Default job priority (1-10)",
        default=6,
        category="Job",
        parser=_parse_int,
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_IMAGE",
        toml_key="job.image",
        field_name="job_image",
        description="Default Docker image for jobs",
        default=None,
        category="Job",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_PROJECT_ID",
        toml_key="job.project_id",
        field_name="job_project_id",
        description="Default project ID for jobs",
        default=None,
        category="Job",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_WORKSPACE_ID",
        toml_key="job.workspace_id",
        field_name="job_workspace_id",
        description="Default workspace ID for jobs",
        default=None,
        category="Job",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_SHM_SIZE",
        toml_key="job.shm_size",
        field_name="shm_size",
        description="Default shared memory size in GB (jobs + notebooks)",
        default=None,
        category="Job",
        parser=_parse_int,
        scope="project",
    ),
]
