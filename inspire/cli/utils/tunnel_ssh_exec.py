"""SSH command execution helpers (ProxyCommand mode)."""

from __future__ import annotations

import select
import subprocess
import time
from typing import Callable, Optional

from .tunnel_config import load_tunnel_config
from .tunnel_models import (
    BridgeNotFoundError,
    TunnelConfig,
    TunnelNotAvailableError,
)
from .tunnel_rtunnel import _ensure_rtunnel_binary
from .tunnel_ssh_proxy import _get_proxy_command


def run_ssh_command(
    command: str,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: Optional[int] = None,
    capture_output: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Execute a command on Bridge via SSH ProxyCommand.

    Args:
        command: Shell command to execute on Bridge
        bridge_name: Name of bridge to use (uses default if None)
        config: Tunnel configuration (loads default if None)
        timeout: Optional timeout in seconds
        capture_output: Whether to capture stdout/stderr
        check: Whether to raise on non-zero exit code

    Returns:
        CompletedProcess with result

    Raises:
        TunnelNotAvailableError: If no bridge configured
        BridgeNotFoundError: If specified bridge not found
        subprocess.TimeoutExpired: If command times out
        subprocess.CalledProcessError: If check=True and command fails
    """
    if config is None:
        config = load_tunnel_config()

    bridge = config.get_bridge(bridge_name)
    if not bridge:
        if bridge_name:
            raise BridgeNotFoundError(f"Bridge '{bridge_name}' not found")
        raise TunnelNotAvailableError(
            "No bridge configured. Run 'inspire tunnel add <name> <url>' first."
        )

    # Ensure rtunnel binary exists
    _ensure_rtunnel_binary(config)

    proxy_cmd = _get_proxy_command(bridge, config.rtunnel_bin, quiet=True)

    # Wrap command in login shell to source ~/.bash_profile for PATH etc.
    import shlex

    wrapped_command = f"LC_ALL=C LANG=C bash -l -c {shlex.quote(command)}"

    ssh_cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ProxyCommand={proxy_cmd}",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(bridge.ssh_port),
        f"{bridge.ssh_user}@localhost",
        wrapped_command,
    ]

    return subprocess.run(
        ssh_cmd,
        capture_output=capture_output,
        text=True,
        timeout=timeout,
        check=check,
    )


def run_ssh_command_streaming(
    command: str,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: Optional[int] = None,
    output_callback: Optional[Callable[[str], None]] = None,
) -> int:
    """Execute a command on Bridge via SSH with streaming output.

    Uses subprocess.Popen with select() for non-blocking I/O, allowing
    real-time output display as the command runs.

    Args:
        command: Shell command to execute on Bridge
        bridge_name: Name of bridge to use (uses default if None)
        config: Tunnel configuration (loads default if None)
        timeout: Optional timeout in seconds
        output_callback: Callback for each line of output (default: click.echo)

    Returns:
        Exit code from the remote command

    Raises:
        TunnelNotAvailableError: If no bridge configured
        BridgeNotFoundError: If specified bridge not found
        subprocess.TimeoutExpired: If command times out
    """
    import click
    import shlex

    if config is None:
        config = load_tunnel_config()

    bridge = config.get_bridge(bridge_name)
    if not bridge:
        if bridge_name:
            raise BridgeNotFoundError(f"Bridge '{bridge_name}' not found")
        raise TunnelNotAvailableError(
            "No bridge configured. Run 'inspire tunnel add <name> <url>' first."
        )

    # Ensure rtunnel binary exists
    _ensure_rtunnel_binary(config)

    proxy_cmd = _get_proxy_command(bridge, config.rtunnel_bin, quiet=True)

    # Wrap command in login shell to source ~/.bash_profile for PATH etc.
    wrapped_command = f"LC_ALL=C LANG=C bash -l -c {shlex.quote(command)}"

    ssh_cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ProxyCommand={proxy_cmd}",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(bridge.ssh_port),
        f"{bridge.ssh_user}@localhost",
        wrapped_command,
    ]

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


def get_ssh_command_args(
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    remote_command: Optional[str] = None,
) -> list[str]:
    """Build SSH command arguments with ProxyCommand.

    Args:
        bridge_name: Name of bridge to use (uses default if None)
        config: Tunnel configuration
        remote_command: Optional command to run (None for interactive shell)

    Returns:
        List of command arguments for subprocess

    Raises:
        TunnelNotAvailableError: If no bridge configured
        BridgeNotFoundError: If specified bridge not found
    """
    if config is None:
        config = load_tunnel_config()

    bridge = config.get_bridge(bridge_name)
    if not bridge:
        if bridge_name:
            raise BridgeNotFoundError(f"Bridge '{bridge_name}' not found")
        raise TunnelNotAvailableError(
            "No bridge configured. Run 'inspire tunnel add <name> <url>' first."
        )

    # Ensure rtunnel binary exists
    _ensure_rtunnel_binary(config)

    proxy_cmd = _get_proxy_command(bridge, config.rtunnel_bin, quiet=True)

    args = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        f"ProxyCommand={proxy_cmd}",
        "-o",
        "LogLevel=ERROR",
        "-p",
        str(bridge.ssh_port),
        f"{bridge.ssh_user}@localhost",
    ]

    if remote_command:
        args.append(remote_command)

    return args
