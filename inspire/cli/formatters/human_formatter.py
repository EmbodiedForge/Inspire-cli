"""Human-readable output formatter for CLI commands.

Provides compact plain-text output for terminal and agent use.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from inspire.cli.utils.timestamps import format_timestamp_utc

# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def format_error(message: str, hint: Optional[str] = None) -> str:
    """Format an error message.

    Args:
        message: Error message
        hint: Optional hint for fixing

    Returns:
        Formatted error string
    """
    lines = [f"Error: {message}"]
    if hint:
        lines.append(f"Hint: {hint}")
    return "\n".join(lines)


def format_success(message: str) -> str:
    """Format a success message.

    Args:
        message: Success message

    Returns:
        Formatted success string
    """
    return f"OK {message}"


def format_warning(message: str) -> str:
    """Format a warning message.

    Args:
        message: Warning message

    Returns:
        Formatted warning string
    """
    return f"Warning: {message}"


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def _format_running_with_limit(running_ms: object, max_ms: object) -> str:
    """Render elapsed wall clock together with the ceiling the platform stored for it.

    `Running Time: 3h 4m` on its own invited exactly the wrong reading during the field investigation: two jobs
    died a little over three hours in while their stored ceilings were 8h and 10h, so the elapsed number looked
    like a limit that had been reached. Showing the ceiling in the same line makes "still had 5h of it" obvious
    and keeps a reader from concluding that the wall-clock cap killed the job. Elapsed is measured from
    scheduling, so it is time since scheduling rather than time spent running.
    """
    elapsed = _format_duration(str(running_ms or "0"))
    try:
        ceiling = int(str(max_ms))
    except (TypeError, ValueError):
        return elapsed
    if ceiling <= 0:
        return elapsed
    return f"{elapsed} / limit {_format_duration(str(ceiling))}"


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


def _format_timestamp(timestamp: object) -> str:
    """Format a platform timestamp as UTC ISO 8601.

    The platform sends epoch seconds, epoch milliseconds (as strings) and ISO dates for the same
    field depending on the endpoint; the shared helper normalises them all. "Unknown" stays the
    answer for a value that is not a moment, so a table never prints a raw epoch or a sentinel.
    """
    formatted = format_timestamp_utc(timestamp)
    return formatted if formatted is not None else "Unknown"


def format_attempt_summary(summary: Dict[str, Any]) -> str:
    """One line saying what fault tolerance has actually done (issue #31).

    ``job status`` used to print "Fault Tolerance: enabled (max retry: 1000)" and nothing about the
    rounds already burned, so a job on its 25th attempt looked identical to one on its first.

    The classes stay separated because their remedies are opposites: a round that never received a node
    is a scheduling/resource-request problem, a round that died on a node is a workload-or-node problem.
    Adding them into one "failed: 25" sends the reader to retry a request that cannot be scheduled.

    Every attempt is accounted for: the displayed classes are fixed, and whatever is left is shown as
    ``other`` so the numbers add up to the total instead of quietly dropping rounds.
    """
    counts = summary.get("counts") or {}
    total = summary.get("total", 0)
    repeated = summary.get("repeated_nodes") or []
    repeated_text = ""
    if repeated:
        # Name the repeats instead of making a reader compare node strings: the names differ only in
        # trailing digits, and that comparison has already produced one wrong conclusion.
        repeated_text = ", " + "; ".join(f"{node} twice" for node in repeated)

    nodes = summary.get("distinct_nodes", 0)
    parts = [
        f"attempts: {total}",
        "failed-on-node: "
        f"{counts.get('failed_on_node', 0)} ({nodes} distinct node{'s' if nodes != 1 else ''}{repeated_text})",
        f"never placed: {counts.get('never_placed', 0)}",
    ]
    shown = counts.get("failed_on_node", 0) + counts.get("never_placed", 0)
    for key in ("running", "creating", "reserving", "succeeded"):
        count = counts.get(key, 0)
        if count:
            parts.append(f"{key}: {count}")
            shown += count
    remaining = total - shown
    if remaining:
        parts.append(f"other: {remaining}")
    return "  ".join(parts)


def _format_timeline(timeline: Any) -> str:
    """Render the platform's phase timeline in the order the timestamps say, not the order the names imply.

    Measured on our own jobs (issue #42): `run` is recorded *before* `resource_prepared` (…043 then …048),
    so the names do not imply a chronological order and sorting by name invents one. The key set is also
    sparse - only some jobs report `resource_prepared` - so a phase that is absent is named as missing and
    never becomes a zero-length step.
    """
    if not isinstance(timeline, dict) or not timeline:
        return ""
    stamp_re = re.compile(r"^\d{10,13}$")
    known = [
        (str(key), str(timeline.get(key)).strip())
        for key in timeline
        if stamp_re.match(str(timeline.get(key) or "").strip())
    ]
    if not known:
        return ""
    known.sort(key=lambda item: int(item[1]))
    parts = []
    previous: int | None = None
    for key, raw in known:
        stamp = int(raw)
        step = ""
        if previous is not None:
            step = f" +{(stamp - previous) // 1000}s"
        parts.append(f"{key}={_format_timestamp(raw)}{step}")
        previous = stamp
    line = " -> ".join(parts)
    missing = [key for key in ("resource_prepared", "run") if not timeline.get(key)]
    if missing:
        line += f"  (not reported: {', '.join(missing)})"
    return line


def format_job_status(job_data: Dict[str, Any]) -> str:
    """Format job status as compact key-value lines.

    Args:
        job_data: Job data from API response

    Returns:
        Formatted string with job status
    """
    status = str(job_data.get("status", "UNKNOWN"))
    lines = ["Job Status"]

    # Core fields
    fields = [
        ("Job ID", job_data.get("job_id", "N/A")),
        ("Name", job_data.get("name", "N/A")),
        ("Status", status),
        (
            "Running Time",
            _format_running_with_limit(
                job_data.get("running_time_ms", "0"), job_data.get("max_running_time_ms")
            ),
        ),
    ]

    # Optional fields
    if job_data.get("node_count"):
        fields.append(("Nodes", str(job_data["node_count"])))
    priority_display = _format_priority_display(job_data)
    if priority_display != "-":
        fields.append(("Priority", priority_display))
    framework_config = job_data.get("framework_config")
    if isinstance(framework_config, list) and framework_config:
        first_framework = framework_config[0]
        if isinstance(first_framework, dict) and first_framework.get("shm_gi") is not None:
            fields.append(("SHM", f"{first_framework['shm_gi']} GiB"))
    if "auto_fault_tolerance" in job_data:
        fault_tolerance = "enabled" if job_data.get("auto_fault_tolerance") else "disabled"
        max_retry = job_data.get("fault_tolerance_max_retry")
        if job_data.get("auto_fault_tolerance") and max_retry is not None:
            # `fault_tolerance_max_retry` counts ATTEMPTS, not retries. Measured on our own jobs:
            # cap=100 produced 25 instances (rounds 1..25); cap=1 produced exactly one instance for a
            # container that started and failed after 3s - no retry. "max retry: 1" therefore reads as
            # "one retry available" and means the opposite, which is how a deterministic crash loop
            # stayed plausible for 14 hours (issue #43). Spell both numbers.
            try:
                attempts = int(max_retry)
                retries = max(0, attempts - 1)
            except (TypeError, ValueError):  # pragma: no cover - defensive against odd payloads
                attempts, retries = None, None
            if attempts is None:
                fault_tolerance = f"{fault_tolerance} (max retry: {max_retry})"
            else:
                fault_tolerance = (
                    f"{fault_tolerance} ({attempts} attempt{'s' if attempts != 1 else ''}, "
                    f"{retries} retr{'y' if retries == 1 else 'ies'})"
                )
                current_round = job_data.get("current_running_round")
                if current_round is not None:
                    # The round counter is in the payload the status call already makes; hiding it is why
                    # "25 rounds of the same bug" and "first attempt" looked identical.
                    fault_tolerance = f"{fault_tolerance}, on round {current_round}"
        fields.append(("Fault Tolerance", fault_tolerance))
    if job_data.get("sub_msg"):
        fields.append(("Message", job_data["sub_msg"][:40]))

    # Timeline
    if job_data.get("created_at"):
        fields.append(("Created", _format_timestamp(job_data["created_at"])))
    if job_data.get("finished_at"):
        fields.append(("Finished", _format_timestamp(job_data["finished_at"])))
    phase_line = _format_timeline(job_data.get("timeline"))
    if phase_line:
        fields.append(("Phases", phase_line))

    for label, value in fields:
        lines.append(f"{label}: {value}")

    return "\n".join(lines)


def _format_priority_display(job_data: Dict[str, Any]) -> str:
    """Render priority with an explicit LOW/preemptible marker for the 1-3 band."""
    priority = (
        job_data.get("priority_name") or job_data.get("task_priority") or job_data.get("priority")
    )
    if not priority:
        return "-"
    priority_level = str(job_data.get("priority_level") or "").strip()
    display = str(priority)
    if priority_level:
        display = f"{display} ({priority_level})"
    else:
        try:
            if int(priority) <= 3:
                display = f"{display} (LOW, preemptible)"
        except (TypeError, ValueError):
            pass
    return display


def format_job_list(jobs: List[Dict[str, Any]]) -> str:
    """Format job list as a table.

    Args:
        jobs: List of job data dictionaries

    Returns:
        Formatted table string
    """
    if not jobs:
        return "No jobs found."

    # Determine dynamic column widths to avoid truncation while keeping the table aligned.
    job_id_width = max(len("Job ID"), *(len(str(job.get("job_id", "N/A"))) for job in jobs))
    name_width = max(len("Name"), *(len(str(job.get("name", "N/A"))) for job in jobs))
    status_strings = [str(job.get("status", "UNKNOWN")) for job in jobs]
    status_width = (
        max(len("Status"), *(len(s) for s in status_strings)) if status_strings else len("Status")
    )
    priority_strings = [_format_priority_display(job) for job in jobs]
    priority_width = max(len("Priority"), *(len(s) for s in priority_strings))
    # The Created column is formatted, never the raw epoch the platform sends: a 13-digit number
    # is not a date a human can read, and every other timestamp in the CLI is ISO 8601.
    created_strings = [_format_timestamp(job.get("created_at")) for job in jobs]
    created_width = max(len("Created"), *(len(created) for created in created_strings))

    header_line = (
        f"{'Job ID':<{job_id_width}} {'Name':<{name_width}} {'Status':<{status_width}} "
        f"{'Priority':<{priority_width}} {'Created':<{created_width}}"
    )
    separator = "-" * len(header_line)
    lines = ["Jobs", header_line, separator]

    for job, status_str, priority_str, created in zip(
        jobs, status_strings, priority_strings, created_strings, strict=True
    ):
        job_id = str(job.get("job_id", "N/A"))
        name = str(job.get("name", "N/A"))

        lines.append(
            f"{job_id:<{job_id_width}} {name:<{name_width}} {status_str:<{status_width}} "
            f"{priority_str:<{priority_width}} {created:<{created_width}}"
        )

    lines.append(separator)
    lines.append(f"Total: {len(jobs)} job(s)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


def format_resources(specs: List[Dict[str, Any]], groups: List[Dict[str, Any]]) -> str:
    """Format available resources as a table.

    Args:
        specs: List of resource specifications
        groups: List of compute groups

    Returns:
        Formatted string with resources
    """
    lines = ["Available resources", "GPU configurations:"]

    for spec in specs:
        desc = spec.get("description", f"{spec.get('gpu_count', '?')}x GPU")
        lines.append(f"- {desc}")

    lines.extend(
        [
            "",
            "Compute groups:",
        ]
    )

    for group in groups:
        name = group.get("name", "Unknown")
        location = group.get("location", "")
        lines.append(f"- {name}" + (f" ({location})" if location else ""))

    lines.extend(
        [
            "",
            "Usage:",
            "- --resource 'H200' -> 1x H200 GPU",
            "- --resource '4xH200' -> 4x H200 GPU",
            "- --resource '8 H200' -> 8x H200 GPU",
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
        return "No nodes found."

    lines = [
        "Cluster nodes",
        f"{'Node ID':<40} {'Pool':<12} {'Status':<12} {'GPUs':<8}",
        "-" * 80,
    ]

    for node in nodes:
        node_id = str(node.get("node_id", "N/A"))[:38]
        pool = node.get("resource_pool", "unknown")
        status = node.get("status", "unknown")
        gpus = str(node.get("gpu_count", "?"))

        lines.append(f"{node_id:<40} {pool:<12} {status:<12} {gpus:<8}")

    lines.append("-" * 80)
    if total:
        lines.append(f"Showing {len(nodes)} of {total} nodes")
    else:
        lines.append(f"Total: {len(nodes)} node(s)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def format_image_list(images: List[Dict[str, Any]]) -> str:
    """Format image list as a table.

    Args:
        images: List of image data dictionaries

    Returns:
        Formatted table string
    """
    if not images:
        return "No images found."

    # The column used to be labelled "Source", and readers (including us, in a written incident report)
    # took its values - "public", "private" - for visibility. They are not: visibility is whichever
    # `--source` view was queried, and a `public` origin is compatible with PRIVATE visibility. The header
    # now says Origin; the legend below the table states what visibility does come from.
    source_labels = {
        "SOURCE_OFFICIAL": "official",
        "SOURCE_PUBLIC": "public",
        "SOURCE_PRIVATE": "private",
    }

    lines = [
        f"{'Name':<30} {'Version':<12} {'Origin':<10} {'Status':<10} {'Framework':<14}",
        "-" * 80,
    ]

    for img in images:
        name = str(img.get("name", "N/A"))[:30]
        version = str(img.get("version", ""))[:12]
        raw_source = str(img.get("source", ""))
        source = source_labels.get(raw_source, raw_source)[:10]
        status = str(img.get("status", ""))[:10]
        framework = str(img.get("framework", ""))[:14]

        lines.append(f"{name:<30} {version:<12} {source:<10} {status:<10} {framework:<14}")
    lines.append("")
    lines.append(
        "Origin is image provenance, not visibility. Visibility is the view you queried with --source: "
        "an image is institution-readable iff it also appears under `--source public`."
    )

    lines.append("-" * 80)
    lines.append(f"Total: {len(images)} image(s)")

    return "\n".join(lines)


def format_project_list(projects: List[Dict[str, Any]]) -> str:
    """Format project list as a table.

    Args:
        projects: List of project data dictionaries

    Returns:
        Formatted table string
    """
    if not projects:
        return "No projects found."

    lines = [
        f"{'Name':<24} {'Priority':<10} {'Budget remain':<16}",
        "-" * 52,
    ]

    for proj in projects:
        name = str(proj.get("name", "N/A"))[:24]
        priority = str(proj.get("priority_level", ""))[:10] or "-"
        budget = proj.get("member_remain_budget", 0.0)
        budget_str = f"{budget:,.0f}"

        lines.append(f"{name:<24} {priority:<10} {budget_str:<16}")

    lines.append("-" * 52)
    lines.append(f"Total: {len(projects)} project(s)")

    return "\n".join(lines)


def format_image_detail(image_data: Dict[str, Any]) -> str:
    """Format image detail as compact key-value lines.

    Args:
        image_data: Image data dictionary

    Returns:
        Formatted string with image details
    """
    lines = ["Image Detail"]

    # The column used to be labelled "Source", and readers (including us, in a written incident report)
    # took its values - "public", "private" - for visibility. They are not: visibility is whichever
    # `--source` view was queried, and a `public` origin is compatible with PRIVATE visibility. The header
    # now says Origin; the legend below the table states what visibility does come from.
    source_labels = {
        "SOURCE_OFFICIAL": "official",
        "SOURCE_PUBLIC": "public",
        "SOURCE_PRIVATE": "private",
    }

    raw_source = str(image_data.get("source", ""))
    source = source_labels.get(raw_source, raw_source)

    fields = [
        ("Image ID", image_data.get("image_id", "N/A")),
        ("Name", image_data.get("name", "N/A")),
        ("Version", image_data.get("version", "")),
        ("Framework", image_data.get("framework", "")),
        ("Source", source),
        ("Status", image_data.get("status", "")),
        ("URL", image_data.get("url", "")),
        ("Description", image_data.get("description", "")),
        ("Created", image_data.get("created_at", "")),
    ]

    for label, value in fields:
        if value:
            lines.append(f"{label}: {value}")

    return "\n".join(lines)
