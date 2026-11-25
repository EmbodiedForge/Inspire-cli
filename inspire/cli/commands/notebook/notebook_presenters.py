"""Presentation helpers for notebook CLI output."""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from inspire.cli.formatters import json_formatter
from inspire.cli.utils.timestamps import format_timestamp_utc
from .notebook_lookup import (
    _GPU_TYPE_BY_RAM_GB,
    _NOTEBOOK_UUID_RE,
    _format_notebook_resource,
    _notebook_id_from_item,
    _normalize_notebook_id,
)

if TYPE_CHECKING:
    pass


# Column formatters for notebook list
COLUMN_SPECS = {
    "name": ("Name", 25),
    "status": ("Status", 12),
    "resource": ("Resource", 12),
    "id": ("ID", 38),
    "created": ("Created", 20),
    "gpu": ("GPU", 8),
    "cpu": ("CPU", 8),
    "memory": ("Memory", 10),
    "image": ("Image", 25),
    "project": ("Project", 20),
    "priority": ("Priority", 20),
    "workspace": ("Workspace", 15),
    "node": ("Node", 15),
    "uptime": ("Uptime", 10),
    "tunnel": ("Tunnel", 15),
}

#: The one default column set for the human `notebook list` table. The click option in
#: ``notebook_commands.list_notebooks`` and the presenter's own fallback both read this
#: constant, so the two defaults cannot drift apart again.
DEFAULT_NOTEBOOK_LIST_COLUMNS = "name,status,resource,id"


def _format_uptime(item: dict) -> str:
    """Format uptime from live_time seconds."""
    live_seconds = int(item.get("live_time") or 0)
    if live_seconds <= 0:
        return "N/A"
    hours, rem = divmod(live_seconds, 3600)
    minutes = rem // 60
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "< 1m"


def _format_memory(item: dict) -> str:
    """Format memory size."""
    quota = item.get("quota") or {}
    memory = quota.get("memory_size", 0)
    return f"{memory}GiB" if memory else "N/A"


def _format_priority(item: dict) -> str:
    """Render notebook priority with an explicit LOW/preemptible marker."""
    project = item.get("project") or {}
    priority = project.get("priority_name") or item.get("task_priority")
    if not priority:
        return "-"
    display = str(priority)
    priority_level = str(project.get("priority_level") or "").strip()
    if priority_level:
        display = f"{display} ({priority_level})"
    else:
        try:
            if int(priority) <= 3:
                display = f"{display} (LOW, preemptible)"
        except (TypeError, ValueError):
            pass
    return display


def _get_tunnel_name(item: dict, tunnel_config) -> str:
    """Get tunnel name for notebook, or '-'."""
    if not tunnel_config:
        return "-"
    notebook_id = item.get("notebook_id") or item.get("id", "")
    normalized_id = _normalize_notebook_id(notebook_id)
    for bridge in tunnel_config.list_bridges():
        if bridge.notebook_id and _normalize_notebook_id(bridge.notebook_id) == normalized_id:
            return bridge.name
    return "-"


def _public_notebook_list_item(item: dict) -> dict:
    """Shape one platform list row for CLI output.

    The platform row carries a numeric primary key in ``id`` and the usable reference in
    ``notebook_id``. Output keeps the platform's ``notebook_id`` untouched, moves the
    primary key to ``internal_id``, and makes ``id`` the bare notebook UUID so a caller
    can feed it straight back into ``notebook status``/``stop``/... Rows whose reference
    is not a UUID (test doubles, unknown shapes) pass through unchanged.
    """
    result = dict(item)
    reference = _notebook_id_from_item(item)
    normalized = _normalize_notebook_id(reference) if reference else ""
    if normalized and _NOTEBOOK_UUID_RE.match(normalized):
        raw_id = item.get("id")
        if raw_id is not None and str(raw_id) != normalized:
            result["internal_id"] = raw_id
        result["id"] = normalized
    if "created_at" in result:
        # One UTC ISO 8601 spelling; the .NET empty-date sentinel and non-positive epochs
        # are "never set" and become null.
        result["created_at"] = format_timestamp_utc(result.get("created_at"))
    return result


def _public_notebook_list_items(items: list) -> list:
    return [_public_notebook_list_item(item) for item in items]


def _format_column(item: dict, col: str, tunnel_config=None) -> str:
    """Format a single column value."""
    if col == "name":
        return item.get("name", "N/A")
    elif col == "status":
        return item.get("status", "Unknown")
    elif col == "resource":
        return _format_notebook_resource(item)
    elif col == "id":
        return item.get("notebook_id") or item.get("id", "N/A")
    elif col == "created":
        # Unset sentinels (.NET empty date, non-positive epoch) render as Unknown, not
        # as 0001-01-01 or 1970-01-01.
        return format_timestamp_utc(item.get("created_at")) or "Unknown"
    elif col == "gpu":
        quota = item.get("quota") or {}
        return str(quota.get("gpu_count", 0))
    elif col == "cpu":
        quota = item.get("quota") or {}
        return str(quota.get("cpu_count", 0))
    elif col == "memory":
        return _format_memory(item)
    elif col == "image":
        image = item.get("image") or {}
        name = image.get("name", "")
        version = image.get("version", "")
        return f"{name}:{version}" if name and version else name or "N/A"
    elif col == "project":
        return item.get("project", {}).get("name") or item.get("project_name", "N/A")
    elif col == "priority":
        return _format_priority(item)
    elif col == "workspace":
        return item.get("workspace", {}).get("name", "N/A")
    elif col == "node":
        return item.get("extra_info", {}).get("NodeName", "N/A")
    elif col == "uptime":
        return _format_uptime(item)
    elif col == "tunnel":
        return _get_tunnel_name(item, tunnel_config)
    return "N/A"


def _print_notebook_detail(notebook: dict) -> None:
    """Print detailed notebook information."""
    click.echo(f"\n{'=' * 60}")
    click.echo(f"Notebook: {notebook.get('name', 'N/A')}")
    click.echo(f"{'=' * 60}")

    project = notebook.get("project") or {}
    quota = notebook.get("quota") or {}
    compute_group = notebook.get("logic_compute_group") or {}
    extra = notebook.get("extra_info") or {}
    image = notebook.get("image") or {}
    start_cfg = notebook.get("start_config") or {}
    workspace = notebook.get("workspace") or {}
    node = notebook.get("node") or {}

    gpu_type = ""
    node_gpu_info = node.get("gpu_info")
    if isinstance(node_gpu_info, dict):
        gpu_type = node_gpu_info.get("gpu_product_simple", "")
    if not gpu_type:
        spec = notebook.get("resource_spec_price") or {}
        gpu_info = spec.get("gpu_info") or {}
        gpu_type = gpu_info.get("gpu_product_simple", "")
    if not gpu_type:
        gpu_type = quota.get("gpu_type") or _GPU_TYPE_BY_RAM_GB.get(
            int(quota.get("gpu_ram") or 0), ""
        )

    gpu_count = quota.get("gpu_count", 0)
    gpu_str = f"{gpu_count}x {gpu_type}" if gpu_type and gpu_count else str(gpu_count or "N/A")

    img_name = image.get("name", "")
    img_ver = image.get("version", "")
    img_str = f"{img_name}:{img_ver}" if img_name and img_ver else img_name or "N/A"

    live_seconds = int(notebook.get("live_time") or 0)
    uptime = ""
    if live_seconds > 0:
        hours, rem = divmod(live_seconds, 3600)
        minutes = rem // 60
        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        uptime = " ".join(parts) or "< 1m"

    shm = start_cfg.get("shared_memory_size", 0) or 0

    fields = [
        ("ID", notebook.get("notebook_id") or notebook.get("id")),
        ("Status", notebook.get("status")),
        ("Project", project.get("name") or notebook.get("project_name")),
        ("Priority", _format_priority(notebook)),
        ("Compute Group", compute_group.get("name")),
        ("Image", img_str),
        ("GPU", gpu_str),
        ("CPU", quota.get("cpu_count")),
        ("Memory", f"{quota['memory_size']} GiB" if quota.get("memory_size") else None),
        ("SHM", f"{shm} GiB" if shm else None),
        ("Node", extra.get("NodeName") or None),
        ("Host IP", extra.get("HostIP") or None),
        ("Uptime", uptime or None),
        ("Workspace", workspace.get("name")),
        ("Created", format_timestamp_utc(notebook.get("created_at")) or "Unknown"),
    ]

    for label, value in fields:
        if value:
            click.echo(f"  {label:<15}: {value}")

    click.echo(f"{'=' * 60}\n")


def _print_notebook_list(
    items: list,
    json_output: bool,
    columns: str = DEFAULT_NOTEBOOK_LIST_COLUMNS,
    tunnel_config=None,
) -> None:
    """Print notebook list in appropriate format with customizable columns."""
    if json_output:
        click.echo(json_formatter.format_json({"items": items, "total": len(items)}))
        return

    if not items:
        click.echo("No notebook instances found.")
        return

    column_list = [c.strip().lower() for c in columns.split(",")]

    # Validate columns
    valid_columns = [c for c in column_list if c in COLUMN_SPECS]
    if not valid_columns:
        valid_columns = DEFAULT_NOTEBOOK_LIST_COLUMNS.split(",")

    # Build header
    headers = []
    for col in valid_columns:
        header, width = COLUMN_SPECS[col]
        headers.append(f"{header:<{width}}")

    # Build separator
    total_width = sum(COLUMN_SPECS[c][1] + 1 for c in valid_columns) - 1

    lines = [" ".join(headers), "-" * total_width]

    # Build rows
    for item in items:
        row_parts = []
        for col in valid_columns:
            width = COLUMN_SPECS[col][1]
            value = _format_column(item, col, tunnel_config)
            # Truncate if too long
            if len(value) > width:
                value = value[: width - 3] + "..."
            row_parts.append(f"{value:<{width}}")
        lines.append(" ".join(row_parts))

    lines.append(f"\nShowing {len(items)} notebook(s)")
    click.echo("\n".join(lines))


__all__ = [
    "_print_notebook_detail",
    "_print_notebook_list",
    "_public_notebook_list_item",
    "_public_notebook_list_items",
    "COLUMN_SPECS",
    "DEFAULT_NOTEBOOK_LIST_COLUMNS",
]
