"""SSH command execution via ProxyCommand: run, stream, and argument helpers."""

from __future__ import annotations

import os
import select
import subprocess
import time
from typing import Callable, Optional

from .config import load_tunnel_config
from .models import (
    BridgeNotFoundError,
    BridgeProfile,
    TunnelConfig,
    TunnelNotAvailableError,
)
from .rtunnel import _ensure_rtunnel_binary
from .ssh import _get_proxy_command


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def _build_ssh_process_env() -> dict[str, str]:
    """Build environment for SSH subprocesses with a universally available locale.

    This prevents remote login shells from inheriting unsupported locale values
    (for example ``en_US.UTF-8``) via SSH env forwarding.
    """
    env = os.environ.copy()
    env.update({"LC_ALL": "C", "LANG": "C"})
    return env


def _resolve_bridge_and_proxy(
    bridge_name: Optional[str],
    config: Optional[TunnelConfig],
    *,
    quiet: bool = True,
) -> tuple[TunnelConfig, BridgeProfile, str]:
    if config is None:
        config = load_tunnel_config()

    bridge = config.get_bridge(bridge_name)
    if not bridge:
        if bridge_name:
            raise BridgeNotFoundError(f"Bridge '{bridge_name}' not found")
        raise TunnelNotAvailableError(
            "No bridge configured. Run 'inspire tunnel add <name> <url>' first."
        )

    _ensure_rtunnel_binary(config)
    proxy_cmd = _get_proxy_command(bridge, config.rtunnel_bin, quiet=quiet)
    return config, bridge, proxy_cmd


def _build_stdin_script(command: str) -> str:
    """Build a short shell script to pipe into ``bash -l`` via stdin.

    This avoids embedding *command* in the SSH process's command-line
    arguments, which would otherwise make ``pkill -f <pattern>`` match
    the parent bash process and tear down the SSH session.
    """
    return f"export LC_ALL=C LANG=C; {command}\n"


def _build_ssh_base_args(
    *,
    bridge: BridgeProfile,
    proxy_cmd: str,
    batch_mode: bool = True,
) -> list[str]:
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
    if batch_mode:
        args[5:5] = ["-o", "BatchMode=yes"]
    return args


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_ssh_command(
    command: str,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: Optional[int] = None,
    capture_output: bool = True,
    check: bool = False,
    *,
    quiet_proxy: bool = True,
) -> subprocess.CompletedProcess:
    """Execute a command on Bridge via SSH ProxyCommand."""
    _config, bridge, proxy_cmd = _resolve_bridge_and_proxy(bridge_name, config, quiet=quiet_proxy)
    ssh_cmd = _build_ssh_base_args(bridge=bridge, proxy_cmd=proxy_cmd)
    ssh_cmd.append("bash -l")

    return subprocess.run(
        ssh_cmd,
        input=_build_stdin_script(command),
        capture_output=capture_output,
        text=True,
        timeout=timeout,
        check=check,
        env=_build_ssh_process_env(),
    )


# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------


def get_ssh_command_args(
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    remote_command: Optional[str] = None,
) -> list[str]:
    """Build SSH command arguments with ProxyCommand."""
    _config, bridge, proxy_cmd = _resolve_bridge_and_proxy(bridge_name, config)
    args = _build_ssh_base_args(bridge=bridge, proxy_cmd=proxy_cmd, batch_mode=False)
    if remote_command:
        args.append(remote_command)
    return args


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


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
    ssh_cmd.append("bash -l")

    # Default callback: print to stdout
    if output_callback is None:

        def _default_output_callback(line: str) -> None:
            click.echo(line, nl=False)

        output_callback = _default_output_callback

    process = subprocess.Popen(
        ssh_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        env=_build_ssh_process_env(),
    )

    # Feed the command via stdin so it never appears in the process cmdline.
    script = _build_stdin_script(command)
    process.stdin.write(script)
    process.stdin.close()

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

            # Read from a single path (readline only) so lines cannot be emitted twice.
            if process.poll() is not None:
                line = process.stdout.readline()
            else:
                ready, _, _ = select.select([process.stdout], [], [], 1.0)
                if not ready:
                    continue
                line = process.stdout.readline()

            if line:
                output_callback(line)
                continue

            if process.poll() is not None:
                break

        return process.returncode

    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        raise
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()


__all__ = [
    "_build_ssh_process_env",
    "_build_ssh_base_args",
    "_build_stdin_script",
    "_resolve_bridge_and_proxy",
    "get_ssh_command_args",
    "run_ssh_command",
    "run_ssh_command_streaming",
]
