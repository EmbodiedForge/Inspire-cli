"""Output helpers for `inspire job logs` (single-job mode)."""

from __future__ import annotations

from pathlib import Path

import click

from inspire.cli.context import Context
from inspire.cli.formatters import json_formatter


def echo_log_path(ctx: Context, *, job_id: str, remote_log_path: str) -> None:
    if ctx.json_output:
        click.echo(json_formatter.format_json({"job_id": job_id, "log_path": remote_log_path}))
    else:
        click.echo(remote_log_path)


def echo_ssh_content(
    ctx: Context,
    *,
    job_id: str,
    remote_log_path: str,
    content: str,
    tail: int,
    head: int,
) -> None:
    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "job_id": job_id,
                    "log_path": remote_log_path,
                    "content": content,
                    "method": "ssh_tunnel",
                }
            )
        )
        return

    if tail:
        click.echo(f"=== Last {tail} lines ===\n")
    elif head:
        click.echo(f"=== First {head} lines ===\n")
    click.echo(content)


def echo_file_tail(ctx: Context, *, cache_path: Path, tail: int) -> None:
    with cache_path.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    tail_lines = lines[-tail:] if tail > 0 else lines

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "log_path": str(cache_path),
                    "lines": tail_lines,
                    "count": len(tail_lines),
                }
            )
        )
    else:
        click.echo(f"=== Last {len(tail_lines)} lines ===\n")
        for line in tail_lines:
            click.echo(line)


def echo_file_head(ctx: Context, *, cache_path: Path, head: int) -> None:
    with cache_path.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    head_lines = lines[:head] if head > 0 else lines

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "log_path": str(cache_path),
                    "lines": head_lines,
                    "count": len(head_lines),
                }
            )
        )
    else:
        click.echo(f"=== First {len(head_lines)} lines ===\n")
        for line in head_lines:
            click.echo(line)


def echo_file_content(ctx: Context, *, cache_path: Path) -> None:
    content = cache_path.read_text(encoding="utf-8", errors="replace")

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "log_path": str(cache_path),
                    "content": content,
                    "size_bytes": len(content),
                }
            )
        )
    else:
        click.echo(content)
