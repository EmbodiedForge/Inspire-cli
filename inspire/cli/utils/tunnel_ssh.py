"""SSH tunnel utilities for Bridge access via ProxyCommand."""

from __future__ import annotations

import select
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from .tunnel_config import load_tunnel_config
from .tunnel_models import (
    BridgeNotFoundError,
    BridgeProfile,
    TunnelConfig,
    TunnelError,
    TunnelNotAvailableError,
)
from .tunnel_rtunnel import _ensure_rtunnel_binary


def _get_proxy_command(bridge: BridgeProfile, rtunnel_bin: Path, quiet: bool = False) -> str:
    """Build the ProxyCommand string for SSH.

    Args:
        bridge: Bridge profile with proxy_url
        rtunnel_bin: Path to rtunnel binary
        quiet: If True, suppress rtunnel stderr output (startup/shutdown messages)

    Returns:
        ProxyCommand string for SSH -o option
    """
    import shlex

    # Convert https:// URL to wss:// for websocket
    proxy_url = bridge.proxy_url
    if proxy_url.startswith("https://"):
        ws_url = "wss://" + proxy_url[8:]
    elif proxy_url.startswith("http://"):
        ws_url = "ws://" + proxy_url[7:]
    else:
        ws_url = proxy_url

    # ProxyCommand is executed by a shell on the client; quote the URL because it
    # can contain characters like '?' (e.g. token query params) that some shells
    # treat as glob patterns.
    if quiet:
        # Wrap in sh -c to redirect stderr, suppressing rtunnel's verbose output
        cmd = f"{rtunnel_bin} {shlex.quote(ws_url)} stdio://%h:%p 2>/dev/null"
        return f"sh -c {shlex.quote(cmd)}"
    else:
        return (
            f"{shlex.quote(str(rtunnel_bin))} {shlex.quote(ws_url)} {shlex.quote('stdio://%h:%p')}"
        )


def _test_ssh_connection(
    bridge: BridgeProfile,
    config: TunnelConfig,
    timeout: int = 10,
) -> bool:
    """Test if SSH connection works via ProxyCommand.

    Args:
        bridge: Bridge profile to test
        config: Tunnel configuration (for rtunnel binary path)
        timeout: SSH connection timeout in seconds (default: 10)

    Returns:
        True if SSH connection succeeds, False otherwise
    """
    # Ensure rtunnel binary exists
    try:
        _ensure_rtunnel_binary(config)
    except TunnelError:
        return False

    proxy_cmd = _get_proxy_command(bridge, config.rtunnel_bin, quiet=True)

    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={timeout}",
                "-o",
                f"ProxyCommand={proxy_cmd}",
                "-o",
                "LogLevel=ERROR",
                "-p",
                str(bridge.ssh_port),
                f"{bridge.ssh_user}@localhost",
                "echo ok",
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
        return result.returncode == 0 and "ok" in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def is_tunnel_available(
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    retries: int = 3,
    retry_pause: float = 2.0,
    progressive: bool = True,
) -> bool:
    """Check if SSH via ProxyCommand is available and responsive.

    Args:
        bridge_name: Name of bridge to check (uses default if None)
        config: Tunnel configuration (loads default if None)
        retries: Number of retries if SSH test fails (default: 3)
        retry_pause: Base pause between retries in seconds (default: 2.0)
        progressive: If True, increase pause with each retry (default: True)

    Returns:
        True if SSH via ProxyCommand works, False otherwise
    """
    if config is None:
        config = load_tunnel_config()

    bridge = config.get_bridge(bridge_name)
    if not bridge:
        return False

    # Test SSH connection with retry
    for attempt in range(retries + 1):
        if _test_ssh_connection(bridge, config):
            return True
        if attempt < retries:
            # Progressive: 2s, 3s, 4s for attempts 0, 1, 2
            pause = retry_pause + (attempt * 1.0) if progressive else retry_pause
            time.sleep(pause)
    return False


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


def get_tunnel_status(
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
) -> dict:
    """Get tunnel status for a bridge (ProxyCommand mode).

    Args:
        bridge_name: Name of bridge to check (uses default if None)
        config: Tunnel configuration

    Returns:
        Dict with keys:
        - configured: bool (bridge exists)
        - bridge_name: Optional[str]
        - ssh_works: bool
        - proxy_url: Optional[str]
        - rtunnel_path: Optional[str]
        - bridges: list of all bridge names
        - default_bridge: Optional[str]
        - error: Optional[str]
    """
    if config is None:
        config = load_tunnel_config()

    bridge = config.get_bridge(bridge_name)

    status = {
        "configured": bridge is not None,
        "bridge_name": bridge.name if bridge else None,
        "ssh_works": False,
        "proxy_url": bridge.proxy_url if bridge else None,
        "rtunnel_path": str(config.rtunnel_bin) if config.rtunnel_bin.exists() else None,
        "bridges": [b.name for b in config.list_bridges()],
        "default_bridge": config.default_bridge,
        "error": None,
    }

    if not bridge:
        if bridge_name:
            status["error"] = f"Bridge '{bridge_name}' not found."
        else:
            status["error"] = "No bridge configured. Run 'inspire tunnel add <name> <url>' first."
        return status

    # Check if rtunnel binary exists
    if not config.rtunnel_bin.exists():
        try:
            _ensure_rtunnel_binary(config)
            status["rtunnel_path"] = str(config.rtunnel_bin)
        except TunnelError as e:
            status["error"] = str(e)
            return status

    # Test SSH connection
    status["ssh_works"] = _test_ssh_connection(bridge, config)
    if not status["ssh_works"]:
        status["error"] = "SSH connection failed. Check proxy URL and Bridge rtunnel server."

    return status
