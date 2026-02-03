"""Resource request parsing for the Inspire OpenAPI client."""

from __future__ import annotations

import re

from inspire.api.openapi_models import GPUType


def parse_resource_request(resource_str: str) -> tuple[GPUType, int]:
    """Parse natural language resource request into a (GPU type, count) tuple."""
    if not resource_str:
        raise ValueError("Resource description cannot be empty")

    resource_str = resource_str.upper().strip()

    patterns = [
        r"^(\d+)[xX]?(H100|H200)$",
        r"^(H100|H200)[xX]?(\d+)?$",
        r"^(\d+)\s+(H100|H200)$",
    ]

    gpu_count = 1
    gpu_type_str = None

    for pattern in patterns:
        match = re.match(pattern, resource_str.replace(" ", ""))
        if match:
            groups = match.groups()
            if len(groups) == 2:
                if groups[0].isdigit():
                    gpu_count = int(groups[0])
                    gpu_type_str = groups[1]
                elif groups[1] and groups[1].isdigit():
                    gpu_type_str = groups[0]
                    gpu_count = int(groups[1])
                else:
                    gpu_type_str = groups[0] if not groups[0].isdigit() else groups[1]
            break

    if not gpu_type_str:
        if "H200" in resource_str:
            gpu_type_str = "H200"
        elif "H100" in resource_str:
            gpu_type_str = "H100"

    if not gpu_type_str:
        raise ValueError(f"Unrecognized GPU type: {resource_str}")

    try:
        gpu_type = GPUType(gpu_type_str)
    except ValueError as e:
        raise ValueError(
            f"Unsupported GPU type: {gpu_type_str}, supported types: H100, H200"
        ) from e

    if gpu_count <= 0:
        raise ValueError(f"GPU count must be positive: {gpu_count}")

    return gpu_type, gpu_count


__all__ = ["parse_resource_request"]
