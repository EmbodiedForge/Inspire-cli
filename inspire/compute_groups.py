"""Shared compute group definitions used across CLI and API helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComputeGroupDefinition:
    name: str
    compute_group_id: str
    gpu_type: str
    location: str = ""


COMPUTE_GROUPS: tuple[ComputeGroupDefinition, ...] = (
    ComputeGroupDefinition(
        name="H100 (CUDA 12.8)",
        compute_group_id="lcg-79b2ad0e-a375-43f3-a0b1-b4ce79710fd7",
        gpu_type="H100",
        location="CUDA 12.8版本",
    ),
    ComputeGroupDefinition(
        name="H200 机房1",
        compute_group_id="lcg-df089db8-817a-4aa8-a164-eb1a32948564",
        gpu_type="H200",
        location="1号机房",
    ),
    ComputeGroupDefinition(
        name="H200 机房2",
        compute_group_id="lcg-303ac8c6-aa19-4284-af03-2296592326e5",
        gpu_type="H200",
        location="2号机房",
    ),
    ComputeGroupDefinition(
        name="H200 机房3",
        compute_group_id="lcg-a91ad10b-415d-4abd-8170-828a2feae5d2",
        gpu_type="H200",
        location="3号机房",
    ),
    ComputeGroupDefinition(
        name="H200 3号-2",
        compute_group_id="lcg-95e38be4-4842-4155-af13-4325aa744bca",
        gpu_type="H200",
        location="3号-2",
    ),
)


def compute_group_name_map() -> dict[str, str]:
    return {group.compute_group_id: group.name for group in COMPUTE_GROUPS}
