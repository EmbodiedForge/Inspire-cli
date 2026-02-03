"""Resource-related human formatter helpers."""

from __future__ import annotations

from typing import Any, Dict, List


def format_resources(specs: List[Dict[str, Any]], groups: List[Dict[str, Any]]) -> str:
    """Format available resources as a table.

    Args:
        specs: List of resource specifications
        groups: List of compute groups

    Returns:
        Formatted string with resources
    """
    lines = [
        "",
        "\U0001f4ca Available Resources",
        "",
        "\U0001f5a5\ufe0f  GPU Configurations:",
        "\u2500" * 60,
    ]

    for spec in specs:
        desc = spec.get("description", f"{spec.get('gpu_count', '?')}x GPU")
        lines.append(f"  \u2022 {desc}")

    lines.extend(
        [
            "",
            "\U0001f3e2 Compute Groups:",
            "\u2500" * 60,
        ]
    )

    for group in groups:
        name = group.get("name", "Unknown")
        location = group.get("location", "")
        lines.append(f"  \u2022 {name}" + (f" ({location})" if location else ""))

    lines.extend(
        [
            "",
            "\U0001f4a1 Usage Examples:",
            "  \u2022 --resource 'H200'     -> 1x H200 GPU",
            "  \u2022 --resource '4xH200'   -> 4x H200 GPU",
            "  \u2022 --resource '8 H200'   -> 8x H200 GPU",
        ]
    )

    return "\n".join(lines)


def format_nodes(nodes: List[Dict[str, Any]], total: int = 0) -> str:
    """Format cluster nodes as a table.

    Args:
        nodes: List of node data
        total: Total number of nodes (for pagination)

    Returns:
        Formatted table string
    """
    if not nodes:
        return "\nNo nodes found.\n"

    lines = [
        "",
        "\U0001f5a5\ufe0f  Cluster Nodes",
        "\u2500" * 80,
        f"{'Node ID':<40} {'Pool':<12} {'Status':<12} {'GPUs':<8}",
        "\u2500" * 80,
    ]

    for node in nodes:
        node_id = str(node.get("node_id", "N/A"))[:38]
        pool = node.get("resource_pool", "unknown")
        status = node.get("status", "unknown")
        gpus = str(node.get("gpu_count", "?"))

        lines.append(f"{node_id:<40} {pool:<12} {status:<12} {gpus:<8}")

    lines.append("\u2500" * 80)
    if total:
        lines.append(f"Showing {len(nodes)} of {total} nodes")
    else:
        lines.append(f"Total: {len(nodes)} node(s)")

    return "\n".join(lines)


def format_groups(groups: List[Any]) -> str:
    """Format compute groups as a table.

    Args:
        groups: List of ComputeGroupAvailability objects or dicts

    Returns:
        Formatted table string
    """
    if not groups:
        return "\nNo compute groups found.\n"

    lines = [
        "",
        "\U0001f3e2 Compute Groups",
        "\u2500" * 100,
        f"{'Group ID':<40} {'Name':<18} {'GPU':<8} {'Online':<8} {'Fault':<8} {'Free GPUs':<10}",
        "\u2500" * 100,
    ]

    for group in groups:
        # Handle both dataclass and dict
        if hasattr(group, "group_id"):
            group_id = group.group_id[:38] if len(group.group_id) > 38 else group.group_id
            name = group.group_name[:16] if len(group.group_name) > 16 else group.group_name
            gpu_type = group.gpu_type
            online = str(group.online_nodes)
            fault = str(group.fault_nodes)
            free_gpus = str(group.free_gpus)
        else:
            group_id = str(group.get("group_id", ""))[:38]
            name = str(group.get("group_name", "Unknown"))[:16]
            gpu_type = str(group.get("gpu_type", "?"))
            online = str(group.get("online_nodes", 0))
            fault = str(group.get("fault_nodes", 0))
            free_gpus = str(group.get("free_gpus", 0))

        lines.append(
            f"{group_id:<40} {name:<18} {gpu_type:<8} {online:<8} {fault:<8} {free_gpus:<10}"
        )

    lines.append("\u2500" * 100)
    lines.append(f"Total: {len(groups)} group(s)")

    return "\n".join(lines)
