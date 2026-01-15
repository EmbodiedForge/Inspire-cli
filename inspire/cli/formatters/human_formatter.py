"""Human-readable output formatter for CLI commands.

Provides pretty-printed output with colors and tables.
"""

import sys
from typing import Any, Dict, List, Optional
from datetime import datetime


# Status emoji mapping
STATUS_EMOJI = {
    "PENDING": "\u23f3",  # hourglass
    "RUNNING": "\U0001f3c3",  # runner
    "SUCCEEDED": "\u2705",  # check mark
    "FAILED": "\u274c",  # cross mark
    "CANCELLED": "\U0001f6d1",  # stop sign
    "UNKNOWN": "\u2753",  # question mark
    # API snake_case variants
    "job_succeeded": "\u2705",  # check mark
    "job_failed": "\u274c",  # cross mark
    "job_cancelled": "\U0001f6d1",  # stop sign
}

DEFAULT_STATUS_EMOJI = "\U0001f4ca"  # bar chart


def _format_duration(ms: str) -> str:
    """Format milliseconds as human-readable duration."""
    try:
        milliseconds = int(ms)
        seconds = milliseconds // 1000
        minutes = seconds // 60
        hours = minutes // 60

        if hours > 0:
            return f"{hours}h {minutes % 60}m {seconds % 60}s"
        elif minutes > 0:
            return f"{minutes}m {seconds % 60}s"
        else:
            return f"{seconds}s"
    except (ValueError, TypeError):
        return "Unknown"


def _format_timestamp(timestamp_ms: str) -> str:
    """Format millisecond timestamp as human-readable datetime."""
    try:
        timestamp = int(timestamp_ms) / 1000
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "Unknown"


def format_job_status(job_data: Dict[str, Any]) -> str:
    """Format job status as a pretty box.

    Args:
        job_data: Job data from API response

    Returns:
        Formatted string with job status
    """
    status = job_data.get("status", "UNKNOWN")
    emoji = STATUS_EMOJI.get(status, DEFAULT_STATUS_EMOJI)

    lines = [
        "",
        "\u256d" + "\u2500" * 50 + "\u256e",
        "\u2502" + " Job Status".ljust(50) + "\u2502",
        "\u251c" + "\u2500" * 50 + "\u2524",
    ]

    # Core fields
    fields = [
        ("Job ID", job_data.get("job_id", "N/A")),
        ("Name", job_data.get("name", "N/A")),
        ("Status", f"{emoji} {status}"),
        ("Running Time", _format_duration(job_data.get("running_time_ms", "0"))),
    ]

    # Optional fields
    if job_data.get("node_count"):
        fields.append(("Nodes", str(job_data["node_count"])))
    if job_data.get("priority"):
        fields.append(("Priority", str(job_data["priority"])))
    if job_data.get("sub_msg"):
        fields.append(("Message", job_data["sub_msg"][:40]))

    # Timeline
    if job_data.get("created_at"):
        fields.append(("Created", _format_timestamp(job_data["created_at"])))
    if job_data.get("finished_at"):
        fields.append(("Finished", _format_timestamp(job_data["finished_at"])))

    for label, value in fields:
        line = f" {label}:".ljust(15) + str(value)
        lines.append("\u2502" + line.ljust(50) + "\u2502")

    lines.append("\u2570" + "\u2500" * 50 + "\u256f")

    return "\n".join(lines)


def format_job_list(jobs: List[Dict[str, Any]]) -> str:
    """Format job list as a table.

    Args:
        jobs: List of job data dictionaries

    Returns:
        Formatted table string
    """
    if not jobs:
        return "\nNo jobs found in local cache.\n"

    # Determine dynamic column widths to avoid truncation while keeping the table aligned.
    job_id_width = max(len("Job ID"), *(len(str(job.get("job_id", "N/A"))) for job in jobs))
    name_width = max(len("Name"), *(len(str(job.get("name", "N/A"))) for job in jobs))
    status_strings = [
        f"{STATUS_EMOJI.get(job.get('status', 'UNKNOWN'), DEFAULT_STATUS_EMOJI)} {job.get('status', 'UNKNOWN')}"
        for job in jobs
    ]
    status_width = (
        max(len("Status"), *(len(s) for s in status_strings)) if status_strings else len("Status")
    )
    created_width = max(len("Created"), *(len(str(job.get("created_at", "N/A"))) for job in jobs))

    header_line = f"{'Job ID':<{job_id_width}} {'Name':<{name_width}} {'Status':<{status_width}} {'Created':<{created_width}}"
    separator = "\u2500" * len(header_line)

    lines = [
        "",
        "\U0001f4cb Recent Jobs",
        separator,
        header_line,
        separator,
    ]

    for job, status_str in zip(jobs, status_strings):
        job_id = str(job.get("job_id", "N/A"))
        name = str(job.get("name", "N/A"))
        created = str(job.get("created_at", "N/A"))

        lines.append(
            f"{job_id:<{job_id_width}} {name:<{name_width}} {status_str:<{status_width}} {created:<{created_width}}"
        )

    lines.append(separator)
    lines.append(f"Total: {len(jobs)} job(s)")

    return "\n".join(lines)


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
        if hasattr(group, 'group_id'):
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

        lines.append(f"{group_id:<40} {name:<18} {gpu_type:<8} {online:<8} {fault:<8} {free_gpus:<10}")

    lines.append("\u2500" * 100)
    lines.append(f"Total: {len(groups)} group(s)")

    return "\n".join(lines)


def format_error(message: str, hint: Optional[str] = None) -> str:
    """Format an error message.

    Args:
        message: Error message
        hint: Optional hint for fixing

    Returns:
        Formatted error string
    """
    lines = [f"\n\u274c Error: {message}"]
    if hint:
        lines.append(f"\U0001f4a1 Hint: {hint}")
    return "\n".join(lines)


def format_success(message: str) -> str:
    """Format a success message.

    Args:
        message: Success message

    Returns:
        Formatted success string
    """
    return f"\u2705 {message}"


def format_warning(message: str) -> str:
    """Format a warning message.

    Args:
        message: Warning message

    Returns:
        Formatted warning string
    """
    return f"\u26a0\ufe0f {message}"


def print_error(message: str, hint: Optional[str] = None) -> None:
    """Print an error message to stderr."""
    print(format_error(message, hint), file=sys.stderr)


def print_success(message: str) -> None:
    """Print a success message to stdout."""
    print(format_success(message))
