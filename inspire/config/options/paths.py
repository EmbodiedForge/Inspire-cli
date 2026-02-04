"""Config options: Paths."""

from __future__ import annotations

from inspire.config.schema_models import ConfigOption

PATHS_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_TARGET_DIR",
        toml_key="paths.target_dir",
        field_name="target_dir",
        description="Target directory on Bridge shared filesystem",
        default=None,
        category="Paths",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_LOG_PATTERN",
        toml_key="paths.log_pattern",
        field_name="log_pattern",
        description="Log file glob pattern",
        default="training_master_*.log",
        category="Paths",
        scope="project",
    ),
    ConfigOption(
        env_var="INSPIRE_JOB_CACHE",
        toml_key="paths.job_cache",
        field_name="job_cache_path",
        description="Local job cache file path",
        default="~/.inspire/jobs.json",
        category="Paths",
        scope="global",
    ),
    ConfigOption(
        env_var="INSP_LOG_CACHE_DIR",
        toml_key="paths.log_cache_dir",
        field_name="log_cache_dir",
        description="Cache directory for remote logs",
        default="~/.inspire/logs",
        category="Paths",
        scope="global",
    ),
]
