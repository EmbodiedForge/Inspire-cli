"""Job-related human formatter helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

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
        if minutes > 0:
            return f"{minutes}m {seconds % 60}s"
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

    header_line = (
        f"{'Job ID':<{job_id_width}} {'Name':<{name_width}} {'Status':<{status_width}} "
        f"{'Created':<{created_width}}"
    )
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
            f"{job_id:<{job_id_width}} {name:<{name_width}} {status_str:<{status_width}} "
            f"{created:<{created_width}}"
        )

    lines.append(separator)
    lines.append(f"Total: {len(jobs)} job(s)")

    return "\n".join(lines)
