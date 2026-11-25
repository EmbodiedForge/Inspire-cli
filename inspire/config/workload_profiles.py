"""Layered workload profiles containing portable platform conditions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from inspire.config.models import ConfigError

WORKLOAD_KINDS = ("notebook", "job", "hpc", "ray", "model")
_FORBIDDEN_EXACT = {
    "name",
    "command",
    "entrypoint",
    "password",
    "token",
    "api_key",
    "secret",
}
_COMMON_FIELDS = {
    "workspace",
    "project",
    "group",
    "quota",
    "image",
    "image_type",
    "priority",
}
PROFILE_FIELDS: dict[str, set[str]] = {
    "notebook": _COMMON_FIELDS
    | {"resource", "shm_size", "post_start", "post_start_mode", "internet"},
    "job": _COMMON_FIELDS
    | {
        "resource",
        "framework",
        "nodes",
        "max_time",
        "fault_tolerant",
        "fault_tolerance_max_retry",
        "keep_alive_on_success",
        "keep_alive_on_fail",
    },
    "hpc": _COMMON_FIELDS
    | {
        "instance_count",
        "number_of_tasks",
        "cpus_per_task",
        "memory_per_cpu",
        "memory_per_node",
        "job_max_time",
        "enable_hyper_threading",
    },
    "ray": _COMMON_FIELDS | {"head", "workers", "description"},
    "model": _COMMON_FIELDS
    | {
        "model",
        "replicas",
        "min_replicas",
        "max_replicas",
        "auto_stop",
        "custom_domain",
        "description",
    },
}


def validate_profile(kind: str, profile: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one profile and reject live bindings, execution fields, and secrets."""
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in PROFILE_FIELDS:
        raise ConfigError(f"Unsupported workload profile kind: {kind!r}")
    if not isinstance(profile, Mapping):
        raise ConfigError("Workload profile must be an object")

    allowed = PROFILE_FIELDS[normalized_kind]
    result: dict[str, Any] = {}
    for raw_key, value in profile.items():
        key = str(raw_key or "").strip()
        lowered = key.lower()
        if not key:
            raise ConfigError("Workload profile fields cannot be empty")
        if lowered in _FORBIDDEN_EXACT or lowered.endswith("_id"):
            raise ConfigError(
                f"Workload profile field {key!r} is not portable; "
                "profiles cannot store execution content, secrets, or platform IDs"
            )
        if key not in allowed:
            available = ", ".join(sorted(allowed))
            raise ConfigError(
                f"Unsupported {normalized_kind} profile field {key!r}. Allowed: {available}"
            )
        result[key] = deepcopy(value)
    return result


def normalize_profiles(raw: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """Normalize `[profiles.<kind>.<name>]` tables."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, Mapping):
        raise ConfigError("profiles must be an object")
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for raw_kind, raw_profiles in raw.items():
        kind = str(raw_kind or "").strip().lower()
        if kind not in PROFILE_FIELDS:
            raise ConfigError(f"Unsupported workload profile kind: {kind!r}")
        if not isinstance(raw_profiles, Mapping):
            raise ConfigError(f"profiles.{kind} must be an object")
        result[kind] = {}
        for raw_name, raw_profile in raw_profiles.items():
            name = str(raw_name or "").strip()
            if not name:
                raise ConfigError(f"profiles.{kind} contains an empty profile name")
            result[kind][name] = validate_profile(kind, raw_profile)
    return result


def merge_profiles(
    global_profiles: Mapping[str, Any], project_profiles: Mapping[str, Any]
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, str]]]:
    """Merge profile layers and report the winning source for every profile."""
    global_normalized = normalize_profiles(global_profiles)
    project_normalized = normalize_profiles(project_profiles)
    merged = deepcopy(global_normalized)
    sources: dict[str, dict[str, str]] = {
        kind: {name: "global" for name in profiles} for kind, profiles in global_normalized.items()
    }
    for kind, profiles in project_normalized.items():
        merged.setdefault(kind, {}).update(deepcopy(profiles))
        sources.setdefault(kind, {}).update({name: "project" for name in profiles})
    return merged, sources


def apply_profile(
    *,
    profiles: Mapping[str, Any],
    kind: str,
    name: str | None,
    explicit: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge explicit values over one selected workload profile."""
    selected: dict[str, Any] = {}
    sources: dict[str, str] = {}
    if name:
        kind_profiles = profiles.get(kind, {}) if isinstance(profiles, Mapping) else {}
        if not isinstance(kind_profiles, Mapping) or name not in kind_profiles:
            available = ", ".join(sorted(str(item) for item in kind_profiles)) or "<none>"
            raise ConfigError(
                f"Unknown {kind} profile {name!r}. Available {kind} profiles: {available}"
            )
        selected.update(validate_profile(kind, kind_profiles[name]))
        sources.update({key: f"profile:{name}" for key in selected})

    for key, value in explicit.items():
        if value is None:
            continue
        selected[key] = deepcopy(value)
        sources[key] = "cli"
    return selected, sources


__all__ = [
    "PROFILE_FIELDS",
    "WORKLOAD_KINDS",
    "apply_profile",
    "merge_profiles",
    "normalize_profiles",
    "validate_profile",
]
