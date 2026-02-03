"""Resource spec and compute group matching helpers."""

from __future__ import annotations

import re
from typing import Optional

from inspire.api.openapi_models import ComputeGroup, GPUType, ResourceSpec


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
    compute_groups: list[ComputeGroup], *, gpu_type: GPUType
) -> list[ComputeGroup]:
    return [group for group in compute_groups if group.gpu_type == gpu_type]


def select_compute_group(
    matching_groups: list[ComputeGroup],
    *,
    prefer_location: Optional[str] = None,
) -> ComputeGroup:
    selected_group = matching_groups[0]

    if not prefer_location:
        return selected_group

    matched = False

    for group in matching_groups:
        if prefer_location.lower() in group.location.lower():
            selected_group = group
            matched = True
            break

    if not matched:
        numbers = re.findall(r"\d+", prefer_location)
        if numbers:
            for num in numbers:
                for group in matching_groups:
                    if num in group.location:
                        selected_group = group
                        matched = True
                        break
                if matched:
                    break

    if not matched:
        available_locations = [g.location for g in matching_groups]
        raise ValueError(
            f"Location '{prefer_location}' not found for {selected_group.gpu_type.value}. "
            f"Available locations: {', '.join(available_locations)}"
        )

    return selected_group


__all__ = ["find_matching_specs", "find_compute_groups", "select_compute_group"]
