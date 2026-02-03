"""Resource parsing and matching for the Inspire OpenAPI client."""

from __future__ import annotations

from typing import Optional

from inspire.api.openapi_models import ComputeGroup, GPUType, ResourceSpec
from inspire.compute_groups import load_compute_groups_from_config

from .display import display_available_resources
from .match import find_compute_groups, find_matching_specs, select_compute_group
from .parse import parse_resource_request
from .specs import build_default_resource_specs


class ResourceManager:
    """Resource manager - handles resource spec and compute group matching."""

    def __init__(self, compute_groups_raw: Optional[list[dict]] = None):
        self.resource_specs = build_default_resource_specs()

        compute_groups_tuples = load_compute_groups_from_config(compute_groups_raw or [])
        self.compute_groups = [
            ComputeGroup(
                name=group.name,
                compute_group_id=group.compute_group_id,
                gpu_type=GPUType(group.gpu_type),
                location=group.location,
            )
            for group in compute_groups_tuples
        ]

    def parse_resource_request(self, resource_str: str) -> tuple[GPUType, int]:
        return parse_resource_request(resource_str)

    def find_matching_specs(self, gpu_type: GPUType, gpu_count: int) -> list[ResourceSpec]:
        return find_matching_specs(self.resource_specs, gpu_type=gpu_type, gpu_count=gpu_count)

    def find_compute_groups(self, gpu_type: GPUType) -> list[ComputeGroup]:
        return find_compute_groups(self.compute_groups, gpu_type=gpu_type)

    def get_recommended_config(
        self, resource_str: str, prefer_location: Optional[str] = None
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

        matching_groups = self.find_compute_groups(gpu_type)
        if not matching_groups:
            raise ValueError(f"No compute group found supporting {gpu_type.value}")

        selected_group = select_compute_group(matching_groups, prefer_location=prefer_location)
        return selected_spec.spec_id, selected_group.compute_group_id

    def display_available_resources(self) -> None:
        display_available_resources(
            resource_specs=self.resource_specs,
            compute_groups=self.compute_groups,
        )


__all__ = ["ResourceManager"]
