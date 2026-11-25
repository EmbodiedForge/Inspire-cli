"""SSH command execution via ProxyCommand: run, stream, and argument helpers.

Protocol
--------
The three public execution functions (``run_ssh_command``,
``run_ssh_command_streaming``, ``run_ssh_command_pooled``) form the
``SSHClient`` interface. Both the subprocess-based and Paramiko-pooled
implementations expose the same three methods, making it possible to
swap implementations without changing call sites.
"""

from __future__ import annotations

import logging
import select
import subprocess
import time
from typing import TYPE_CHECKING, Callable, Optional, Protocol

from .ssh import _ensure_rtunnel_binary, _get_proxy_command, _identity_args
from .ssh_helpers import (
    build_ssh_base_args as _build_ssh_base_args,
    build_ssh_process_env,
    resolve_bridge_and_proxy,
    ssh_locale_args,
    wrap_remote_command,
)
from .config import load_tunnel_config
from .models import BridgeNotFoundError, TunnelNotAvailableError

if TYPE_CHECKING:
    from .models import TunnelConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSHClient Protocol
# ---------------------------------------------------------------------------


class SSHClient(Protocol):
    """Structural protocol for SSH command execution implementations.

    Both the subprocess-based and Paramiko-pooled implementations expose
    the same three methods, allowing call sites to be written against the
    protocol without coupling to a specific backend.

    Subprocess implementation: this module (``ssh_exec.py``)
    Paramiko implementation: ``connection_pool.py``

    Note: the Paramiko paths (``run_ssh_command_pooled``,
    ``run_ssh_command_streaming_pooled``) are defined in this file
    but delegate to ``connection_pool.py`` internally.
    """

    def run_ssh_command(
        self,
        command: str,
        bridge_name: Optional[str] = None,
        config: Optional["TunnelConfig"] = None,
        timeout: Optional[int] = None,
        capture_output: bool = True,
        check: bool = False,
        *,
        quiet_proxy: bool = True,
        pass_stdin: bool = False,
        allocate_pty: bool = False,
    ) -> subprocess.CompletedProcess:
        """Execute a command on Bridge via SSH, blocking."""
        ...

    def run_ssh_command_streaming(
        self,
        command: str,
        bridge_name: Optional[str] = None,
        config: Optional["TunnelConfig"] = None,
        timeout: Optional[int] = None,
        output_callback: Optional[Callable[[str], None]] = None,
        *,
        pass_stdin: bool = False,
        allocate_pty: bool = False,
    ) -> int:
        """Execute a command on Bridge with streaming output. Returns exit code."""
        ...

    def run_ssh_command_pooled(
        self,
        command: str,
        bridge_name: Optional[str] = None,
        config: Optional["TunnelConfig"] = None,
        timeout: Optional[int] = None,
        capture_output: bool = True,
        check: bool = False,
        *,
        pass_stdin: bool = False,
        allocate_pty: bool = False,
    ) -> subprocess.CompletedProcess:
        """Execute using a persistent Paramiko connection (lower latency)."""
        ...


# ---------------------------------------------------------------------------
# Run (subprocess backend)
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
    pass_stdin: bool = False,
    allocate_pty: bool = False,
    keepalive: bool = True,
) -> subprocess.CompletedProcess:
    """Execute a command on Bridge via SSH ProxyCommand."""
    _config, bridge, proxy_cmd = resolve_bridge_and_proxy(bridge_name, config, quiet=quiet_proxy)
    ssh_cmd = _build_ssh_base_args(
        bridge=bridge, proxy_cmd=proxy_cmd, allocate_pty=allocate_pty, keepalive=keepalive
    )
    input_payload: Optional[str] = None
    if pass_stdin:
        ssh_cmd.append(wrap_remote_command(command))
    else:
        # Send command via stdin script so the command string is hidden from
        # `ps aux` on the local machine (command only appears in SSH argument
        # when pass_stdin=True).  The script approach also ensures && chains
        # in the command are parsed correctly by the remote bash.
        from .ssh_helpers import build_stdin_script

        input_payload = build_stdin_script(command)

    logger.debug(
        (
            "run_ssh_command bridge=%s timeout=%s capture_output=%s "
            "quiet_proxy=%s pass_stdin=%s command=%s"
        ),
        bridge.name,
        timeout,
        capture_output,
        quiet_proxy,
        pass_stdin,
        command,
    )

    result = subprocess.run(
        ssh_cmd,
        input=input_payload,
        capture_output=capture_output,
        text=True,
        timeout=timeout,
        check=check,
        env=build_ssh_process_env(),
    )
    logger.debug(
        "run_ssh_command completed bridge=%s returncode=%s",
        bridge.name,
        result.returncode,
    )
    if capture_output:
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        logger.debug(
            "run_ssh_command output bridge=%s stdout_chars=%s stderr_chars=%s",
            bridge.name,
            len(stdout),
            len(stderr),
        )
        if stdout:
            logger.debug("run_ssh_command stdout:\n%s", stdout)
        if stderr:
            logger.debug("run_ssh_command stderr:\n%s", stderr)
    return result


# ---------------------------------------------------------------------------
# Detached (background process)
# ---------------------------------------------------------------------------


def run_ssh_command_detached(
    command: str,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: Optional[int] = None,
    *,
    quiet_proxy: bool = True,
    keepalive: bool = True,
) -> subprocess.CompletedProcess:
    """Launch a command on Bridge that survives after SSH returns.

    Unlike run_ssh_command which waits for the SSH channel to close, this
    sends the command and immediately returns. The remote process runs
    fully independently — SSH exit does not affect it.

    stdin/stdout/stderr are all /dev/null so subprocess.run returns
    immediately. The user's command is responsible for its own output
    handling (use '> log 2>&1' or 'nohup ... &' in the command itself).

    Args:
        command: Shell command to run on the remote bridge (include 'nohup'
            and/or output redirection in the command if desired)
        bridge_name: Bridge to use (default if None)
        config: Tunnel config (loads default if None)
        timeout: SSH connection timeout in seconds
        quiet_proxy: Suppress rtunnel stderr output
        keepalive: Enable SSH keepalive probes (recommended)

    Returns:
        CompletedProcess with returncode=0 on success
    """
    _config, bridge, proxy_cmd = resolve_bridge_and_proxy(bridge_name, config, quiet=quiet_proxy)
    ssh_cmd = _build_ssh_base_args(
        bridge=bridge, proxy_cmd=proxy_cmd, allocate_pty=False, keepalive=keepalive
    )
    # Base64-encode the command and pipe it through bash on the remote side.
    # This avoids the shell-escaping trap of "bash -c" where SSH joins argv
    # elements with spaces, causing only the first word after -c to be treated
    # as the command and the rest as positional parameters.
    ssh_cmd.append(wrap_remote_command(command))

    logger.debug(
        "run_ssh_command_detached bridge=%s timeout=%s command=%s",
        bridge.name,
        timeout,
        command,
    )

    # All FDs to /dev/null — SSH exits as soon as command is sent, process
    # continues independently on the remote bridge.
    result = subprocess.run(
        ssh_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        env=build_ssh_process_env(),
    )
    logger.debug(
        "run_ssh_command_detached completed bridge=%s returncode=%s",
        bridge.name,
        result.returncode,
    )
    return result


# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------


def get_ssh_command_args(
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    remote_command: Optional[str] = None,
) -> list[str]:
    """Build SSH command arguments using stdio:// ProxyCommand."""
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

    proxy_cmd = _get_proxy_command(bridge, config.rtunnel_bin)

    args = [
        "ssh",
        *_identity_args(bridge),
        *ssh_locale_args(),
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
    ]
    if remote_command:
        args.append(wrap_remote_command(remote_command))
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
    *,
    pass_stdin: bool = False,
    allocate_pty: bool = False,
    keepalive: bool = True,
) -> int:
    """Execute a command on Bridge via SSH with streaming output."""
    import click

    _config, bridge, proxy_cmd = resolve_bridge_and_proxy(bridge_name, config)
    ssh_cmd = _build_ssh_base_args(
        bridge=bridge, proxy_cmd=proxy_cmd, allocate_pty=allocate_pty, keepalive=keepalive
    )
    input_payload: Optional[str] = None
    if pass_stdin:
        ssh_cmd.append(wrap_remote_command(command))
    else:
        # Pass command via stdin script to hide it from `ps aux`.  The script
        # approach also correctly handles && chains and other shell operators
        # in the remote command.
        from .ssh_helpers import build_stdin_script

        input_payload = build_stdin_script(command)

    logger.debug(
        "run_ssh_command_streaming bridge=%s timeout=%s pass_stdin=%s command=%s",
        bridge.name,
        timeout,
        pass_stdin,
        command,
    )

    # Default callback: print to stdout
    if output_callback is None:

        def _default_output_callback(line: str) -> None:
            click.echo(line, nl=False)

        output_callback = _default_output_callback

    process = subprocess.Popen(
        ssh_cmd,
        stdin=None if pass_stdin else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        env=build_ssh_process_env(),
    )

    # Write command payload to stdin if not using SSH argument method
    if not pass_stdin and input_payload is not None:
        assert process.stdin is not None
        process.stdin.write(input_payload)
        process.stdin.close()

    assert process.stdout is not None
    process_stdout = process.stdout
    start_time = time.time()

    try:
        while True:
            # Check timeout
            if timeout is not None:
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    logger.debug(
                        "run_ssh_command_streaming timeout bridge=%s elapsed=%.2fs limit=%ss",
                        bridge.name,
                        elapsed,
                        timeout,
                    )
                    process.terminate()
                    process.wait()
                    raise subprocess.TimeoutExpired(ssh_cmd, timeout)

            # Read from a single path (readline only) so lines cannot be emitted twice.
            if process.poll() is not None:
                line = process_stdout.readline()
            else:
                ready, _, _ = select.select([process_stdout], [], [], 1.0)
                if not ready:
                    continue
                line = process_stdout.readline()

            if line:
                logger.debug("run_ssh_command_streaming line=%s", line.rstrip("\n"))
                output_callback(line)
                continue

            if process.poll() is not None:
                break

        logger.debug(
            "run_ssh_command_streaming completed bridge=%s returncode=%s",
            bridge.name,
            process.returncode,
        )
        return process.returncode

    except KeyboardInterrupt:
        logger.debug("run_ssh_command_streaming interrupted bridge=%s", bridge.name)
        process.terminate()
        process.wait()
        raise
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()


# ---------------------------------------------------------------------------
# Connection Pool (Paramiko-based)
# ---------------------------------------------------------------------------


def run_ssh_command_pooled(
    command: str,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: Optional[int] = None,
    capture_output: bool = True,
    check: bool = False,
    *,
    pass_stdin: bool = False,
    allocate_pty: bool = False,
    keepalive: bool = True,
) -> subprocess.CompletedProcess:
    """Execute a command using the connection pool for lower latency.

    This function uses a persistent Paramiko SSH connection to avoid
    the ~200-500ms handshake overhead of subprocess-based SSH.

    Falls back to subprocess-based SSH if paramiko is not available or if the
    pooled connection fails before the command starts. Once the command has
    been handed to the remote side, errors propagate instead of re-running it.

    Args:
        command: The command to execute on the remote bridge
        bridge_name: Name of the bridge to use (uses default if None)
        config: Tunnel configuration (loads default if None)
        timeout: Command timeout in seconds
        capture_output: Whether to capture stdout/stderr
        check: Whether to raise an exception on non-zero exit
        pass_stdin: Only meaningful for the subprocess fallback path.
            When ``True``, passes the command as an SSH argument; when
            ``False``, sends it via stdin to hide it from ``ps`` output.
            The Paramiko path always uses base64 wrapping internally.

    Returns:
        CompletedProcess with returncode, stdout, stderr
    """
    from .connection_pool import get_connection_pool, is_paramiko_available

    if not is_paramiko_available():
        # Fall back to subprocess-based SSH
        return run_ssh_command(
            command,
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
            capture_output=capture_output,
            check=check,
            pass_stdin=pass_stdin,
            keepalive=keepalive,
        )

    _config, bridge, _proxy_cmd = resolve_bridge_and_proxy(bridge_name, config, quiet=True)
    pool = get_connection_pool()

    started = False
    try:
        with pool.get_connection(bridge, _config) as client:
            # Execute the command
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("SSH transport is not active")

            if keepalive:
                transport.set_keepalive(30)

            # Set timeout if specified
            channel_timeout = timeout if timeout else 3600  # Default 1 hour

            stdin, stdout, stderr = client.exec_command(
                wrap_remote_command(command), timeout=channel_timeout
            )
            started = True
            stdin.close()

            # Read output
            stdout_data = stdout.read().decode("utf-8", errors="replace")
            stderr_data = stderr.read().decode("utf-8", errors="replace")
            exit_status = stdout.channel.recv_exit_status()

            result = subprocess.CompletedProcess(
                args=["ssh", bridge.name, command],
                returncode=exit_status,
                stdout=stdout_data,
                stderr=stderr_data,
            )

            if check and result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode, result.args, result.stdout, result.stderr
                )

            return result

    except Exception as e:
        if started:
            logger.debug("Pooled SSH failed after the command started, not retrying: %s", e)
            raise
        logger.debug("Pooled SSH failed, falling back to subprocess: %s", e)
        return run_ssh_command(
            command,
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
            capture_output=capture_output,
            check=check,
            pass_stdin=pass_stdin,
            keepalive=keepalive,
        )


def run_ssh_command_streaming_pooled(
    command: str,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: Optional[int] = None,
    output_callback: Optional[Callable[[str], None]] = None,
    *,
    pass_stdin: bool = False,
    allocate_pty: bool = False,
    keepalive: bool = True,
) -> int:
    """Execute a command with streaming output using the connection pool.

    Falls back to subprocess-based SSH if paramiko is not available or if the
    pooled connection fails before the command starts. Once the command has
    been handed to the remote side, errors propagate instead of re-running it.

    Args:
        command: The command to execute on the remote bridge
        bridge_name: Name of the bridge to use (uses default if None)
        config: Tunnel configuration (loads default if None)
        timeout: Command timeout in seconds
        output_callback: Callback function for each line of output
        pass_stdin: Only meaningful for the subprocess fallback path.
            When ``True``, passes the command as an SSH argument; when
            ``False``, sends it via stdin to hide it from ``ps`` output.
            The Paramiko path always uses base64 wrapping internally.

    Returns:
        Exit code of the command
    """
    import click
    from .connection_pool import get_connection_pool, is_paramiko_available

    if not is_paramiko_available():
        return run_ssh_command_streaming(
            command,
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
            output_callback=output_callback,
            pass_stdin=pass_stdin,
            allocate_pty=allocate_pty,
            keepalive=keepalive,
        )

    _config, bridge, _proxy_cmd = resolve_bridge_and_proxy(bridge_name, config, quiet=True)
    pool = get_connection_pool()

    # Default callback: print to stdout
    if output_callback is None:

        def _default_output_callback(line: str) -> None:
            click.echo(line, nl=False)

        output_callback = _default_output_callback

    started = False
    try:
        with pool.get_connection(bridge, _config) as client:
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("SSH transport is not active")

            if keepalive:
                transport.set_keepalive(30)

            channel = transport.open_session()
            if allocate_pty:
                channel.get_pty()
            channel.exec_command(wrap_remote_command(command))
            started = True

            start_time = time.time()

            def _drain_channel_output() -> bool:
                """Drain up to one chunk from each stream. Returns True if data was read."""
                drained = False
                if channel.recv_ready():
                    data = channel.recv(4096).decode("utf-8", errors="replace")
                    if data:
                        output_callback(data)
                    drained = True
                if channel.recv_stderr_ready():
                    data = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                    if data:
                        output_callback(data)
                    drained = True
                return drained

            # Read output line by line
            while True:
                if timeout is not None:
                    elapsed = time.time() - start_time
                    if elapsed >= timeout:
                        # Drain any buffered output before raising timeout
                        while _drain_channel_output():
                            pass
                        channel.close()
                        raise subprocess.TimeoutExpired(["ssh", bridge.name, command], timeout)

                if channel.exit_status_ready():
                    # Read remaining output
                    while _drain_channel_output():
                        pass
                    break

                if not _drain_channel_output():
                    time.sleep(0.01)  # Small delay to prevent busy-waiting

            exit_status = channel.recv_exit_status()
            channel.close()
            return exit_status

    except subprocess.TimeoutExpired:
        raise
    except Exception as e:
        if started:
            logger.debug("Pooled streaming SSH failed after command start, not retrying: %s", e)
            raise
        logger.debug("Pooled streaming SSH failed, falling back to subprocess: %s", e)
        return run_ssh_command_streaming(
            command,
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
            output_callback=output_callback,
            pass_stdin=pass_stdin,
            allocate_pty=allocate_pty,
            keepalive=keepalive,
        )


__all__ = [
    "build_ssh_process_env",
    "get_ssh_command_args",
    "run_ssh_command",
    "run_ssh_command_detached",
    "run_ssh_command_pooled",
    "run_ssh_command_streaming",
    "run_ssh_command_streaming_pooled",
]
