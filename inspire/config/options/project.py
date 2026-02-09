"""Config options: Job, Notebook, Sync, Workspaces, and Mirrors."""

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

NOTEBOOK_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_NOTEBOOK_RESOURCE",
        toml_key="notebook.resource",
        field_name="notebook_resource",
        description="Default resource for notebooks",
        default="1xH200",
        category="Notebook",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_NOTEBOOK_IMAGE",
        toml_key="notebook.image",
        field_name="notebook_image",
        description="Default Docker image for notebooks",
        default=None,
        category="Notebook",
        scope="project",
    ),
]

SYNC_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_DEFAULT_REMOTE",
        toml_key="sync.default_remote",
        field_name="default_remote",
        description="Default git remote name",
        default="origin",
        category="Sync",
        scope="project",
    ),
]

WORKSPACES_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_WORKSPACE_CPU_ID",
        toml_key="workspaces.cpu",
        field_name="workspace_cpu_id",
        description="Workspace ID for CPU workloads (default workspace)",
        default=None,
        category="Workspaces",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_WORKSPACE_GPU_ID",
        toml_key="workspaces.gpu",
        field_name="workspace_gpu_id",
        description="Workspace ID for GPU workloads (H100/H200)",
        default=None,
        category="Workspaces",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_WORKSPACE_INTERNET_ID",
        toml_key="workspaces.internet",
        field_name="workspace_internet_id",
        description="Workspace ID for internet-enabled workloads (e.g. RTX 4090)",
        default=None,
        category="Workspaces",
        scope="project",
    ),
]

MIRRORS_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_APT_MIRROR_URL",
        toml_key="mirrors.apt_mirror_url",
        field_name="apt_mirror_url",
        description="APT mirror URL for package installation",
        default=None,
        category="Mirrors",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_PIP_INDEX_URL",
        toml_key="mirrors.pip_index_url",
        field_name="pip_index_url",
        description="PyPI mirror URL for Python packages",
        default=None,
        category="Mirrors",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_PIP_TRUSTED_HOST",
        toml_key="mirrors.pip_trusted_host",
        field_name="pip_trusted_host",
        description="Trusted host for pip (when using self-signed certs)",
        default=None,
        category="Mirrors",
        scope="global",
    ),
]
