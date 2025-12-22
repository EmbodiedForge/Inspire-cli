"""Bridge commands for executing raw commands on the Bridge runner."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_SUCCESS,
    EXIT_GENERAL_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_TIMEOUT,
    pass_context,
)
from inspire.cli.utils.config import Config, ConfigError
from inspire.cli.utils.gitlab import (
    GitLabError,
    GitLabAuthError,
    trigger_bridge_action_pipeline,
    wait_for_bridge_action_completion,
    download_bridge_artifact,
    fetch_bridge_output_log,
)


def _split_denylist(items: tuple[str, ...]) -> list[str]:
    parts: list[str] = []
    for raw in items:
        for chunk in raw.replace("\r", "").replace("\n", ",").split(","):
            item = chunk.strip()
            if item:
                parts.append(item)
    return parts


@click.group()
def bridge() -> None:
    """Run commands on the Bridge runner (executes in INSPIRE_TARGET_DIR)."""


@bridge.command("exec")
@click.argument("command")
@click.option(
    "denylist",
    "--denylist",
    multiple=True,
    help="Denylist pattern to block (repeatable or comma-separated)",
)
@click.option(
    "artifact_path",
    "--artifact-path",
    multiple=True,
    help="Path relative to INSPIRE_TARGET_DIR to upload as artifact (repeatable)",
)
@click.option(
    "download",
    "--download",
    type=click.Path(),
    help="Local directory to download artifact contents",
)
@click.option("wait", "--wait/--no-wait", default=True, help="Wait for completion (default: wait)")
@click.option("timeout", "--timeout", type=int, default=None, help="Timeout in seconds (default: config value)")
@pass_context
def exec_command(
    ctx: Context,
    command: str,
    denylist: tuple[str, ...],
    artifact_path: tuple[str, ...],
    download: Optional[str],
    wait: bool,
    timeout: Optional[int],
) -> None:
    """Execute a command on the Bridge runner (self-hosted GitLab runner).

    COMMAND is the shell command to run on Bridge (in INSPIRE_TARGET_DIR).
    Command output (stdout/stderr) is automatically displayed after completion.

    \b
    Examples:
        inspire bridge exec "uv venv .venv"
        inspire bridge exec "pip install torch" --timeout 600
        inspire bridge exec "uv venv .venv" \\
            --artifact-path .venv --download ./local
        inspire bridge exec "python train.py" --no-wait
    """

    try:
        config = Config.from_env_for_sync()
    except ConfigError as e:
        if ctx.json_output:
            click.echo(json.dumps({"error": str(e), "type": "config_error"}))
        else:
            click.echo(f"Configuration error: {e}", err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    # Merge denylist from env + CLI
    merged_denylist: list[str] = []
    if config.bridge_action_denylist:
        merged_denylist.extend(config.bridge_action_denylist)
    merged_denylist.extend(_split_denylist(denylist))

    if not merged_denylist and not ctx.json_output:
        click.echo("Warning: no denylist provided; proceeding", err=True)

    request_id = f"{int(time.time())}-{os.getpid()}"
    artifact_paths_list = list(artifact_path)

    if not ctx.json_output:
        click.echo(f"Triggering bridge exec (request {request_id})")
        click.echo(f"Command: {command}")
        click.echo(f"Working dir: {config.target_dir}")
        if merged_denylist:
            click.echo(f"Denylist: {merged_denylist}")
        if artifact_paths_list:
            click.echo(f"Artifact paths: {artifact_paths_list}")

    try:
        pipeline_response = trigger_bridge_action_pipeline(
            config=config,
            raw_command=command,
            artifact_paths=artifact_paths_list,
            request_id=request_id,
            denylist=merged_denylist,
        )
        pipeline_id = pipeline_response.get("id")
        pipeline_url = pipeline_response.get("web_url", "")
    except (GitLabError, GitLabAuthError) as e:
        if ctx.json_output:
            click.echo(json.dumps({"error": str(e), "type": "gitlab_error"}))
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_GENERAL_ERROR)

    if not pipeline_id:
        if ctx.json_output:
            click.echo(json.dumps({"error": "Failed to get pipeline ID", "type": "gitlab_error"}))
        else:
            click.echo("Error: Failed to get pipeline ID from response", err=True)
        sys.exit(EXIT_GENERAL_ERROR)

    if not wait:
        if ctx.json_output:
            click.echo(
                json.dumps(
                    {
                        "status": "triggered",
                        "request_id": request_id,
                        "pipeline_id": pipeline_id,
                        "pipeline_url": pipeline_url,
                        "command": command,
                    }
                )
            )
        else:
            click.echo("Pipeline dispatched; not waiting for completion")
            if pipeline_url:
                click.echo(f"Pipeline: {pipeline_url}")
        sys.exit(EXIT_SUCCESS)

    action_timeout = timeout or config.bridge_action_timeout or 300

    if not ctx.json_output:
        click.echo(f"Waiting for completion (timeout {action_timeout}s)...")

    try:
        result = wait_for_bridge_action_completion(
            config=config,
            pipeline_id=pipeline_id,
            timeout=action_timeout,
        )
    except TimeoutError as e:
        if ctx.json_output:
            click.echo(json.dumps({"status": "timeout", "error": str(e)}))
        else:
            click.echo(f"Timeout: {e}", err=True)
        sys.exit(EXIT_TIMEOUT)
    except GitLabError as e:
        if ctx.json_output:
            click.echo(json.dumps({"error": str(e), "type": "gitlab_error"}))
        else:
            click.echo(f"Error: {e}", err=True)
        sys.exit(EXIT_GENERAL_ERROR)

    # Fetch and display command output
    output_log: Optional[str] = None
    try:
        output_log = fetch_bridge_output_log(config, pipeline_id)
    except GitLabError:
        pass  # Output fetch is best-effort

    if output_log and not ctx.json_output:
        click.echo("")
        click.echo("--- Command Output ---")
        click.echo(output_log)
        click.echo("--- End Output ---")
        click.echo("")

    if result.get("conclusion") != "success":
        if ctx.json_output:
            click.echo(
                json.dumps(
                    {
                        "status": "failed",
                        "conclusion": result.get("conclusion"),
                        "pipeline_url": result.get("web_url", ""),
                        "output": output_log,
                    }
                )
            )
        else:
            click.echo(
                f"Action failed: {result.get('conclusion')} (see {result.get('web_url', '')})",
                err=True,
            )
        sys.exit(EXIT_GENERAL_ERROR)

    if download:
        if not ctx.json_output:
            click.echo(f"Downloading artifact to {download}...")
        try:
            download_bridge_artifact(config, pipeline_id, Path(download))
        except GitLabError as e:
            if ctx.json_output:
                click.echo(
                    json.dumps(
                        {
                            "status": "partial_success",
                            "error": f"Artifact download failed: {e}",
                        }
                    )
                )
            else:
                click.echo(f"Warning: artifact download failed: {e}", err=True)
            sys.exit(EXIT_GENERAL_ERROR)

    if ctx.json_output:
        click.echo(
            json.dumps(
                {
                    "status": "success",
                    "request_id": request_id,
                    "pipeline_id": pipeline_id,
                    "artifact_downloaded": bool(download),
                    "output": output_log,
                }
            )
        )
    else:
        click.echo("✓ Action completed successfully")
        if result.get("web_url"):
            click.echo(f"Pipeline: {result.get('web_url')}")
        if download:
            click.echo("Artifacts downloaded")

    sys.exit(EXIT_SUCCESS)
