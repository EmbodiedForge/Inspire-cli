"""Helpers for `inspire notebook create`."""

from __future__ import annotations

import re
from typing import Optional


def parse_resource_string(resource: str) -> tuple[int, str, Optional[int]]:
    """Parse a resource string like '1xH200' into (gpu_count, gpu_type, cpu_count).

    Supported formats:
    - "1xH200", "4xH200", "8xH100"
    - "H200", "H100" (defaults to 1 GPU)
    - "1 H200", "4 H100"
    - "4CPU", "4xCPU", "4 CPU" (CPU-only)
    - "CPU" (CPU-only, count resolved from quota)
    - "4x", "4X", "8x" (GPU count only, type auto-selected)
    - "4" (GPU count only, type auto-selected)

    Returns:
        Tuple of (gpu_count, gpu_type_pattern, cpu_count). cpu_count is None
        when the CPU count is unspecified (e.g., "CPU"). gpu_type_pattern is
        "GPU" when the type should be auto-selected.
    """
    resource = resource.strip().upper()

    cpu_aliases = {"CPU", "CPUONLY", "CPU_ONLY", "CPU-ONLY"}

    # Pattern: Nx or NX only (e.g., "4x", "8X") - auto-select GPU type
    match = re.match(r"^(\d+)\s*[xX]$", resource)
    if match:
        count = int(match.group(1))
        return count, "GPU", None  # "GPU" signals auto-select

    # Pattern: N only (e.g., "4", "8") - auto-select GPU type
    match = re.match(r"^(\d+)$", resource)
    if match:
        count = int(match.group(1))
        return count, "GPU", None  # "GPU" signals auto-select

    # Pattern: NxGPU (e.g., "1xH200", "4xH100")
    match = re.match(r"^(\d+)\s*[xX]\s*(\w+)$", resource)
    if match:
        count = int(match.group(1))
        pattern = match.group(2)
        if pattern in cpu_aliases:
            return 0, "CPU", count
        return count, pattern, None

    # Pattern: N GPU (e.g., "1 H200", "4 H100")
    match = re.match(r"^(\d+)\s+(\w+)$", resource)
    if match:
        count = int(match.group(1))
        pattern = match.group(2)
        if pattern in cpu_aliases:
            return 0, "CPU", count
        return count, pattern, None

    # Pattern: NGPU without delimiter (e.g., "4CPU", "4H200")
    match = re.match(r"^(\d+)([A-Z0-9_-]+)$", resource)
    if match:
        count = int(match.group(1))
        pattern = match.group(2)
        if pattern in cpu_aliases:
            return 0, "CPU", count
        return count, pattern, None

    # Pattern: GPU only (e.g., "H200") - defaults to 1
    match = re.match(r"^(\w+)$", resource)
    if match:
        pattern = match.group(1)
        if pattern in cpu_aliases:
            return 0, "CPU", None
        return 1, pattern, None

    raise ValueError(f"Invalid resource format: {resource}")


def format_resource_display(
    gpu_count: int,
    gpu_pattern: str,
    cpu_count: Optional[int],
) -> str:
    """Format a resource string for display."""
    if gpu_count == 0 and gpu_pattern.upper() == "CPU":
        if cpu_count:
            return f"{cpu_count}xCPU"
        return "CPU"
    return f"{gpu_count}x{gpu_pattern}"


def match_gpu_type(pattern: str, gpu_type_display: str) -> bool:
    """Check if a GPU type display string matches a pattern."""
    pattern = pattern.upper()
    gpu_type_display = gpu_type_display.upper()
    return pattern in gpu_type_display
