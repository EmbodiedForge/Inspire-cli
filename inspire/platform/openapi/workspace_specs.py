"""Workspace-aware resource spec fetching for the Inspire OpenAPI client.

Specs are discovered from the browser API per workspace and stored in config.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from inspire.platform.openapi.models import GPUType, ResourceSpec

if TYPE_CHECKING:
    from inspire.config import Config
    from inspire.platform.web.session import WebSession


def fetch_workspace_specs(
    workspace_id: str,
    *,
    session: WebSession | None = None,
) -> list[ResourceSpec]:
    """Fetch resource specs from browser API for a workspace.

    Args:
        workspace_id: Target workspace ID

    Returns:
        List of ResourceSpec for the workspace

    Raises:
        RuntimeError: If fetch fails
    """
    from inspire.platform.web.browser_api import list_compute_groups
    from inspire.platform.web.browser_api.notebooks import (
        _get_session_and_workspace_id,
        _request_notebooks_data,
    )

    try:
        resolved_session, resolved_workspace_id = _get_session_and_workspace_id(
            workspace_id=workspace_id,
            session=session,
        )

        groups = list_compute_groups(
            workspace_id=resolved_workspace_id,
            session=resolved_session,
        )
    except Exception as e:
        raise RuntimeError(
            f"Failed to probe resource specs for workspace {workspace_id}: {e}"
        ) from e
    specs_by_identity: dict[tuple[str, str, int, int, int], ResourceSpec] = {}

    for group in groups:
        group_id = str(group.get("logic_compute_group_id", "")).strip()
        if not group_id:
            continue

        body = {
            "workspace_id": resolved_workspace_id,
            "schedule_config_type": "SCHEDULE_CONFIG_TYPE_TRAIN",
            "logic_compute_group_id": group_id,
        }
        data = _request_notebooks_data(
            resolved_session,
            "POST",
            "/resource_prices/logic_compute_groups/",
            body=body,
            timeout=30,
            default_data=[],
        )
        prices = (
            data
            if isinstance(data, list)
            else data.get(
                "lcg_resource_spec_prices",
                data.get("resource_spec_prices", data.get("list", [])),
            )
        )

        for price in prices:
            spec_id = str(price.get("quota_id", "")).strip()
            gpu_count = int(price.get("gpu_count", 0) or 0)
            gpu_info = price.get("gpu_info", {}) or {}
            gpu_type_raw = _normalize_gpu_type(gpu_info.get("gpu_type", ""))
            if not spec_id or gpu_count <= 0 or not gpu_type_raw:
                continue

            try:
                gpu_type = GPUType(gpu_type_raw)
            except ValueError:
                continue

            gpu_memory = (
                int(gpu_info.get("gpu_memory", 0) or 0)
                or int(gpu_info.get("gpu_memory_gb", 0) or 0)
                or 0
            )
            spec = ResourceSpec(
                gpu_type=gpu_type,
                gpu_count=gpu_count,
                cpu_cores=int(price.get("cpu_count", 0) or 0),
                memory_gb=int(price.get("memory_size_gib", 0) or 0),
                gpu_memory_gb=gpu_memory,
                spec_id=spec_id,
                description=(
                    str(price.get("name", "") or "").strip() or f"{gpu_count} × {gpu_type.value}"
                ),
            )
            specs_by_identity.setdefault(_spec_identity(spec), spec)

    if specs_by_identity:
        return sorted(specs_by_identity.values(), key=lambda s: (s.gpu_count, s.spec_id))

    return []


def _spec_identity(spec: ResourceSpec | dict) -> tuple[str, str, int, int, int]:
    if isinstance(spec, dict):
        return (
            str(spec["spec_id"]),
            str(spec["gpu_type"]),
            int(spec["gpu_count"]),
            int(spec["cpu_cores"]),
            int(spec["memory_gb"]),
        )
    return (
        spec.spec_id,
        spec.gpu_type.value,
        spec.gpu_count,
        spec.cpu_cores,
        spec.memory_gb,
    )


def _normalize_gpu_type(gpu_type_raw: str) -> str:
    """Normalize raw GPU labels to OpenAPI enum values."""
    normalized = (gpu_type_raw or "").strip().upper()
    if not normalized:
        return ""
    if "H200" in normalized:
        return GPUType.H200.value
    if "H100" in normalized:
        return GPUType.H100.value
    if "4090" in normalized:
        return GPUType.RTX4090.value
    return normalized


def load_specs_from_config(config: Config, workspace_id: str) -> list[ResourceSpec] | None:
    """Load specs from config cache if available.

    Args:
        config: Config object
        workspace_id: Workspace to load specs for

    Returns:
        List of ResourceSpec if cached, None otherwise
    """
    specs_data = config.workspace_specs.get(workspace_id)
    if not specs_data:
        return None

    specs = []
    for spec_dict in specs_data:
        try:
            spec = ResourceSpec(
                gpu_type=GPUType(spec_dict["gpu_type"]),
                gpu_count=spec_dict["gpu_count"],
                cpu_cores=spec_dict["cpu_cores"],
                memory_gb=spec_dict["memory_gb"],
                gpu_memory_gb=spec_dict.get("gpu_memory_gb", 0),
                spec_id=spec_dict["spec_id"],
                description=spec_dict.get("description", ""),
            )
            specs.append(spec)
        except (KeyError, ValueError):
            continue

    return specs if specs else None


def save_specs_to_config(config: Config, workspace_id: str, specs: list[ResourceSpec]) -> None:
    """Replace one workspace's specs in the config cache.

    Args:
        config: Config object
        workspace_id: Workspace to save specs for
        specs: Complete list of ResourceSpec for the workspace
    """
    specs_by_identity = {}
    for spec in specs:
        spec_dict = {
            "spec_id": spec.spec_id,
            "gpu_type": spec.gpu_type.value,
            "gpu_count": spec.gpu_count,
            "cpu_cores": spec.cpu_cores,
            "memory_gb": spec.memory_gb,
            "gpu_memory_gb": spec.gpu_memory_gb,
            "description": spec.description,
        }
        specs_by_identity[_spec_identity(spec_dict)] = spec_dict

    config.workspace_specs[workspace_id] = list(specs_by_identity.values())

    from inspire.config.models import Config

    if isinstance(config, Config):
        from inspire.config.toml import save_config

        try:
            account_alias = str(config.account_alias or "").strip()
            if account_alias:
                from inspire.config.account_state import (
                    AccountDiscoveryState,
                    Observation,
                    get_account_discovery_state_path,
                    load_account_discovery_state,
                    save_account_discovery_state,
                )

                state = load_account_discovery_state(account_alias)
                if state is None:
                    if get_account_discovery_state_path(account_alias).exists():
                        raise RuntimeError(
                            "Account discovery state is unreadable; run 'inspire init --discover' "
                            "before refreshing resource specs"
                        )
                    state = AccountDiscoveryState(
                        account_alias=account_alias,
                        platform_username=config.username,
                        base_url=config.base_url,
                    )
                state.workspace_specs[workspace_id] = Observation.success(
                    config.workspace_specs[workspace_id]
                )
                save_account_discovery_state(state)
            save_config(config)
        except Exception as e:
            raise RuntimeError(
                f"Failed to save resource specs for workspace {workspace_id}: {e}"
            ) from e


__all__ = ["fetch_workspace_specs", "load_specs_from_config", "save_specs_to_config"]
