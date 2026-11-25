"""Resource parsing, matching, and display for the Inspire OpenAPI client.

Specs are workspace-scoped. ResourceManager lazy-loads specs from config cache
or browser API, and caches them in memory for the current CLI invocation.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from inspire.compute_groups import load_compute_groups_from_config
from inspire.platform.openapi.models import ComputeGroup, GPUType, ResourceSpec
from inspire.platform.openapi.workspace_specs import (
    fetch_workspace_specs,
    load_specs_from_config,
    save_specs_to_config,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def parse_resource_request(resource_str: str) -> tuple[GPUType, int]:
    """Parse natural language resource request into a (GPU type, count) tuple."""
    if not resource_str:
        raise ValueError("Resource description cannot be empty")

    resource_str = resource_str.upper().strip()
    compact_resource = resource_str.replace(" ", "").replace("_", "").replace("-", "")

    patterns = [
        r"^(?P<count>\d+)[xX]?(?P<gpu>H100|H200|RTX4090|4090)$",
        r"^(?P<gpu>H100|H200|RTX4090|4090)[xX]?(?P<count>\d+)?$",
    ]

    gpu_count = 1
    gpu_type_str = None

    for pattern in patterns:
        match = re.match(pattern, compact_resource)
        if match:
            gpu_type_str = match.group("gpu")
            if match.group("count"):
                gpu_count = int(match.group("count"))
            break

    if not gpu_type_str:
        if "H200" in resource_str:
            gpu_type_str = "H200"
        elif "H100" in resource_str:
            gpu_type_str = "H100"
        elif "4090" in resource_str:
            gpu_type_str = "4090"

    if not gpu_type_str:
        raise ValueError(f"Unrecognized GPU type: {resource_str}")

    if "4090" in gpu_type_str:
        gpu_type_str = "4090"

    try:
        gpu_type = GPUType(gpu_type_str)
    except ValueError as e:
        raise ValueError(
            f"Unsupported GPU type: {gpu_type_str}, supported types: H100, H200, 4090"
        ) from e

    if gpu_count <= 0:
        raise ValueError(f"GPU count must be positive: {gpu_count}")

    return gpu_type, gpu_count


def normalize_gpu_type(gpu_type_raw: str) -> str:
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


# ---------------------------------------------------------------------------
# Match
# ---------------------------------------------------------------------------


def find_matching_specs(
    resource_specs: list[ResourceSpec],
    *,
    gpu_type: GPUType,
    gpu_count: int,
) -> list[ResourceSpec]:
    matching_specs = []

    for spec in resource_specs:
        if spec.gpu_type == gpu_type or (
            gpu_type == GPUType.H100 and spec.gpu_type == GPUType.H200
        ):
            if spec.gpu_count >= gpu_count:
                matching_specs.append(spec)

    matching_specs.sort(key=lambda x: x.gpu_count)
    return matching_specs


def find_compute_groups(
    compute_groups: list[ComputeGroup], *, gpu_type: GPUType, workspace_id: Optional[str] = None
) -> list[ComputeGroup]:
    result = [group for group in compute_groups if group.gpu_type == gpu_type]
    if workspace_id:
        result = [g for g in result if workspace_id in g.workspace_ids]
    return result


def select_compute_group(
    matching_groups: list[ComputeGroup],
    *,
    prefer_location: Optional[str] = None,
) -> ComputeGroup:
    selected_group = matching_groups[0]

    if not prefer_location:
        return selected_group

    matched = False
    prefer_location = prefer_location.strip()
    prefer_location_lower = prefer_location.lower()

    for group in matching_groups:
        for candidate in _group_match_candidates(group):
            if prefer_location_lower in candidate.lower():
                selected_group = group
                matched = True
                break
        if matched:
            break

    if not matched:
        numbers = re.findall(r"\d+", prefer_location)
        if numbers:
            for num in numbers:
                for group in matching_groups:
                    for candidate in _group_match_candidates(group):
                        if num in candidate:
                            selected_group = group
                            matched = True
                            break
                    if matched:
                        break
                if matched:
                    break

    if not matched:
        available_locations = []
        seen = set()
        for group in matching_groups:
            label = _group_display_label(group)
            key = label.casefold()
            if key in seen:
                continue
            seen.add(key)
            available_locations.append(label)
        raise ValueError(
            f"Location '{prefer_location}' not found for {selected_group.gpu_type.value}. "
            f"Available locations: {', '.join(available_locations)}"
        )

    return selected_group


def _group_match_candidates(group: ComputeGroup) -> list[str]:
    candidates = []
    for value in (group.location, group.name):
        text = (value or "").strip()
        if text:
            candidates.append(text)
    return candidates


def _group_display_label(group: ComputeGroup) -> str:
    for value in (group.location, group.name, group.compute_group_id):
        text = (value or "").strip()
        if text:
            return text
    return "unknown"


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def display_available_resources(
    *,
    resource_specs: list[ResourceSpec],
    compute_groups: list[ComputeGroup],
) -> None:
    """Print all available resource configurations."""
    logger.info("\n📊 Available Resource Configurations:")
    logger.info("=" * 60)

    logger.info("\n🖥️  GPU Spec Configurations:")
    for spec in resource_specs:
        logger.info("  • %s", spec.description)
        logger.info("    Spec ID: %s", spec.spec_id)

    logger.info("\n🏢 Compute Groups:")
    for group in compute_groups:
        logger.info("  • %s (%s)", group.name, group.location)
        logger.info("    Compute Group ID: %s", group.compute_group_id)

    logger.info("\n💡 Usage Examples:")
    logger.info("  • --resource 'H200'     -> 1x H200 GPU")
    logger.info("  • --resource '4xH200'   -> 4x H200 GPU")
    logger.info("  • --resource '8 H200'   -> 8x H200 GPU")
    logger.info("  • --resource 'H100'     -> 1x H100 GPU")
    logger.info("  • --resource '4090'     -> 1x RTX 4090 GPU")
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ResourceManager:
    """Resource manager - handles resource spec and compute group matching.

    Specs are lazy-loaded from config cache or browser API and cached in memory.
    """

    def __init__(
        self,
        config,
        compute_groups_raw: Optional[list[dict]] = None,
        *,
        skip_live_probe: bool = False,
    ):
        self.config = config
        # In-memory cache per workspace: {workspace_id: [ResourceSpec, ...]}
        self._specs_cache: dict[str, list[ResourceSpec]] = {}
        self._current_workspace_id: Optional[str] = None
        self._skip_live_probe = skip_live_probe

        compute_groups_tuples = load_compute_groups_from_config(compute_groups_raw or [])
        self.compute_groups = []
        for group in compute_groups_tuples:
            if not group.compute_group_id:
                continue

            gpu_type_raw = normalize_gpu_type(group.gpu_type or "")
            if not gpu_type_raw:
                continue

            try:
                gpu_type = GPUType(gpu_type_raw)
            except ValueError:
                continue

            self.compute_groups.append(
                ComputeGroup(
                    name=group.name,
                    compute_group_id=group.compute_group_id,
                    gpu_type=gpu_type,
                    location=group.location,
                    workspace_ids=group.workspace_ids,
                )
            )

    def ensure_specs_for_workspace(self, workspace_id: str) -> None:
        """Ensure specs are loaded for workspace (from cache or API).

        Args:
            workspace_id: Target workspace ID

        Raises:
            RuntimeError: If probe fails and no cache available
        """
        if workspace_id in self._specs_cache:
            self._current_workspace_id = workspace_id
            return

        if self._skip_live_probe:
            raise RuntimeError("Live resource spec probing is disabled.")

        # Try config cache first
        specs = load_specs_from_config(self.config, workspace_id)

        if specs is None:
            # Fetch from API and save to config
            specs = fetch_workspace_specs(workspace_id)
            save_specs_to_config(self.config, workspace_id, specs)
            logger.info("Discovered and cached %d specs for workspace %s", len(specs), workspace_id)

        self._specs_cache[workspace_id] = specs
        self._current_workspace_id = workspace_id

    def refresh_workspace_specs(self, workspace_id: str) -> list[ResourceSpec]:
        """Refresh specs for workspace (force re-fetch from API).

        Args:
            workspace_id: Workspace to refresh

        Returns:
            Updated list of ResourceSpec
        """
        if self._skip_live_probe:
            raise RuntimeError("Live resource spec probing is disabled.")

        specs = fetch_workspace_specs(workspace_id)
        save_specs_to_config(self.config, workspace_id, specs)
        self._specs_cache[workspace_id] = specs
        logger.info("Refreshed %d specs for workspace %s", len(specs), workspace_id)
        return specs

    def _set_test_specs(self, workspace_id: str, specs: list[ResourceSpec]) -> None:
        """Set test specs (testing only)."""
        self._specs_cache[workspace_id] = specs
        self._current_workspace_id = workspace_id

    @property
    def resource_specs(self) -> list[ResourceSpec]:
        """Get specs for current workspace."""
        if self._current_workspace_id is None:
            raise RuntimeError("No workspace loaded. Call ensure_specs_for_workspace() first.")
        return self._specs_cache.get(self._current_workspace_id, [])

    def parse_resource_request(self, resource_str: str) -> tuple[GPUType, int]:
        return parse_resource_request(resource_str)

    def find_matching_specs(self, gpu_type: GPUType, gpu_count: int) -> list[ResourceSpec]:
        return find_matching_specs(self.resource_specs, gpu_type=gpu_type, gpu_count=gpu_count)

    def find_compute_groups(
        self, gpu_type: GPUType, workspace_id: Optional[str] = None
    ) -> list[ComputeGroup]:
        return find_compute_groups(
            self.compute_groups, gpu_type=gpu_type, workspace_id=workspace_id
        )

    def get_recommended_config(
        self,
        resource_str: str,
        prefer_location: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> tuple[str, str]:
        gpu_type, gpu_count = self.parse_resource_request(resource_str)

        matching_specs = self.find_matching_specs(gpu_type, gpu_count)
        if not matching_specs:
            available_configs = [
                f"{spec.gpu_count}x{spec.gpu_type.value}" for spec in self.resource_specs
            ]
            raise ValueError(
                f"No configuration found matching {gpu_count}x{gpu_type.value}. "
                f"Available configurations: {', '.join(available_configs)}"
            )

        selected_spec = matching_specs[0]

        matching_groups = self.find_compute_groups(
            selected_spec.gpu_type, workspace_id=workspace_id
        )
        if not matching_groups:
            raise ValueError(f"No compute group found supporting {selected_spec.gpu_type.value}")

        selected_group = select_compute_group(matching_groups, prefer_location=prefer_location)
        return selected_spec.spec_id, selected_group.compute_group_id

    def display_available_resources(self) -> None:
        display_available_resources(
            resource_specs=self.resource_specs,
            compute_groups=self.compute_groups,
        )


__all__ = ["ResourceManager", "parse_resource_request", "normalize_gpu_type"]
