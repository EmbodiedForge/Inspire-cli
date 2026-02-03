"""Streaming SSH command execution helpers (ProxyCommand mode)."""

from __future__ import annotations

import select
import subprocess
import time
from typing import Callable, Optional

from inspire.cli.utils.tunnel_models import TunnelConfig
from .core import (
    _build_ssh_base_args,
    _resolve_bridge_and_proxy,
    _wrap_command_in_login_shell,
)


def run_ssh_command_streaming(
    command: str,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: Optional[int] = None,
    output_callback: Optional[Callable[[str], None]] = None,
) -> int:
    """Execute a command on Bridge via SSH with streaming output."""
    import click

    _config, bridge, proxy_cmd = _resolve_bridge_and_proxy(bridge_name, config)
    ssh_cmd = _build_ssh_base_args(bridge=bridge, proxy_cmd=proxy_cmd)
    ssh_cmd.append(_wrap_command_in_login_shell(command))

    # Default callback: print to stdout
    if output_callback is None:

        def _default_output_callback(line: str) -> None:
            click.echo(line, nl=False)

        output_callback = _default_output_callback

    process = subprocess.Popen(
        ssh_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
    )

    start_time = time.time()

    try:
        while True:
            # Check timeout
            if timeout is not None:
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    process.terminate()
                    process.wait()
                    raise subprocess.TimeoutExpired(ssh_cmd, timeout)

            # Check if process has ended
            if process.poll() is not None:
                # Drain any remaining output
                for line in process.stdout:
                    output_callback(line)
                break

            # Use select to wait for output with 1-second timeout
            ready, _, _ = select.select([process.stdout], [], [], 1.0)

            if ready:
                line = process.stdout.readline()
                if line:
                    output_callback(line)
                elif process.poll() is not None:
                    # EOF reached (process exited)
                    break
                # else: temporary no data, continue waiting

        return process.returncode

    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        raise
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()


__all__ = ["run_ssh_command_streaming"]
