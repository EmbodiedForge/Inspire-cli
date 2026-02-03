"""SSH-based log fetch + follow for `inspire job logs`."""

from __future__ import annotations

import logging
from typing import Optional

import click

from inspire.cli.utils.auth import AuthManager
from inspire.cli.utils.config import Config
from inspire.cli.utils.tunnel import run_ssh_command


def _fetch_log_via_ssh(
    remote_log_path: str,
    tail: Optional[int] = None,
    head: Optional[int] = None,
) -> str:
    """Fetch log content via SSH tunnel."""
    if tail:
        command = f"tail -n {tail} '{remote_log_path}'"
    elif head:
        command = f"head -n {head} '{remote_log_path}'"
    else:
        command = f"cat '{remote_log_path}'"

    result = run_ssh_command(command=command, capture_output=True)

    if result.returncode != 0:
        raise IOError(f"Failed to read log file: {result.stderr}")

    return result.stdout


def _follow_logs_via_ssh(
    job_id: str,
    config: Config,
    remote_log_path: str,
    tail_lines: int = 50,
    wait_timeout: int = 300,
) -> Optional[str]:
    """Stream log content via SSH tail -f with auto-stop on job completion."""
    import select
    import subprocess
    import time

    from inspire.cli.utils.tunnel import get_ssh_command_args, run_ssh_command

    # Suppress API logging during streaming to keep output clean
    api_logger = logging.getLogger("inspire.inspire_api_control")
    original_level = api_logger.level
    api_logger.setLevel(logging.CRITICAL)

    # Initialize API client for status checking
    api = AuthManager.get_api(config)
    terminal_statuses = {
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "job_succeeded",
        "job_failed",
        "job_cancelled",
    }
    final_status = None
    status_check_interval = 5  # Check status every 5 seconds

    click.echo(f"Log file: {remote_log_path}")

    # Wait for log file to exist (job may be queuing)
    check_cmd = f"test -f '{remote_log_path}' && echo 'exists' || echo 'waiting'"
    start_time = time.time()
    file_exists = False

    while time.time() - start_time < wait_timeout:
        try:
            result = run_ssh_command(check_cmd, timeout=10)
            if "exists" in result.stdout:
                file_exists = True
                break
        except Exception:
            pass

        elapsed = int(time.time() - start_time)
        click.echo(f"\rWaiting for job to start... ({elapsed}s)", nl=False)
        time.sleep(5)

    if not file_exists:
        click.echo(f"\n\nTimeout: Log file not created after {wait_timeout}s")
        click.echo("Job may still be queuing. Check status with: inspire job status <job_id>")
        return None

    click.echo("\nJob started! Following logs...")
    click.echo(f"(showing last {tail_lines} lines, then following new content)")
    click.echo("Press Ctrl+C to stop\n")

    # Build command: show last N lines then follow
    command = f"tail -n {tail_lines} -f '{remote_log_path}'"
    ssh_args = get_ssh_command_args(remote_command=command)

    process = None
    try:
        # Run SSH with real-time output
        process = subprocess.Popen(
            ssh_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )

        # Use select for non-blocking I/O with periodic status checks
        last_status_check = time.time()

        while True:
            # Check if process has ended
            if process.poll() is not None:
                # Drain any remaining output
                for line in process.stdout:
                    click.echo(line, nl=False)
                break

            # Use select to wait for output with timeout
            ready, _, _ = select.select([process.stdout], [], [], 1.0)

            if ready:
                line = process.stdout.readline()
                if line:
                    click.echo(line, nl=False)
                elif process.poll() is not None:
                    # EOF reached (process exited)
                    break

            # Periodically check job status
            current_time = time.time()
            if current_time - last_status_check >= status_check_interval:
                last_status_check = current_time
                try:
                    result = api.get_job_detail(job_id)
                    job_data = result.get("data", {})
                    current_status = job_data.get("status", "UNKNOWN")

                    if current_status in terminal_statuses:
                        final_status = current_status
                        # Grace period: wait a bit for final logs
                        time.sleep(3)
                        # Drain remaining output
                        process.stdout.close()
                        break
                except Exception:
                    # Status check failed, continue streaming
                    pass

        # Show completion message
        if final_status:
            click.echo(f"\n\nJob completed with status: {final_status}")

    except KeyboardInterrupt:
        click.echo("\n\nStopped following logs.")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
        # Restore API logging level
        api_logger.setLevel(original_level)

    return final_status
