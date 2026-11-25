"""Shared SSH helpers used by both subprocess and Paramiko SSH clients.

These helpers are used by both the subprocess-based and Paramiko-pooled
SSH implementations. Keeping them in one place eliminates the duplication
that previously existed between the two code paths.
"""

from __future__ import annotations

import logging
import os
import shlex
from typing import Optional

from .config import load_tunnel_config
from .models import (
    BridgeNotFoundError,
    BridgeProfile,
    TunnelConfig,
    TunnelNotAvailableError,
)
from .rtunnel import _ensure_rtunnel_binary
from .ssh import _get_proxy_command

logger = logging.getLogger(__name__)

REMOTE_LOCALE_EXPORT = "export LC_ALL=C LANG=C;"
REMOTE_BASH_RESOLVER = (
    "$(if [ -x /bin/bash ]; then printf %s /bin/bash; "
    "else command -v bash 2>/dev/null || printf %s bash; fi)"
)
REMOTE_BASE64_RESOLVER = (
    "$(if [ -x /usr/bin/base64 ]; then printf %s /usr/bin/base64; "
    "else command -v base64 2>/dev/null || printf %s base64; fi)"
)
QUIET_SHELL_ARGS = ["/bin/bash", "--noprofile", "--norc"]


def build_ssh_process_env() -> dict[str, str]:
    """Build environment for SSH subprocesses with a universally available locale.

    This prevents remote login shells from inheriting unsupported locale values
    (for example ``en_US.UTF-8``) via SSH env forwarding.
    """
    env = os.environ.copy()
    env.update({"LC_ALL": "C", "LANG": "C"})
    return env


def resolve_bridge_and_proxy(
    bridge_name: Optional[str],
    config: Optional[TunnelConfig],
    *,
    quiet: bool = True,
) -> tuple[TunnelConfig, BridgeProfile, str]:
    """Resolve bridge profile and ProxyCommand string.

    Shared by both subprocess and Paramiko SSH implementations.
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

    _ensure_rtunnel_binary(config)
    proxy_cmd = _get_proxy_command(bridge, config.rtunnel_bin, quiet=quiet)
    return config, bridge, proxy_cmd


def build_stdin_script(command: str) -> str:
    """Build a shell script to pipe into a non-login bash via stdin.

    This avoids embedding *command* in the SSH process's command-line
    arguments, which would otherwise make ``pkill -f <pattern>`` match
    the parent bash process and tear down the SSH session.
    """
    return f"{REMOTE_LOCALE_EXPORT} {command}\n"


def ssh_locale_args() -> list[str]:
    """Return SSH options that suppress unsupported locale forwarding."""
    return [
        "-F",
        "/dev/null",
        "-o",
        "SetEnv=LC_ALL=C",
        "-o",
        "SetEnv=LANG=C",
    ]


def quiet_remote_shell_args() -> list[str]:
    """Return the preferred non-login shell argv for command execution."""
    return QUIET_SHELL_ARGS[:]


def remote_bash_invocation(*args: str) -> str:
    """Return a remote shell snippet that prefers /bin/bash and falls back to PATH."""
    quoted_args = " ".join(shlex.quote(arg) for arg in args)
    if quoted_args:
        return f"{REMOTE_BASH_RESOLVER} {quoted_args}"
    return REMOTE_BASH_RESOLVER


def remote_base64_invocation(*args: str) -> str:
    """Return a remote base64 snippet that prefers /usr/bin/base64."""
    quoted_args = " ".join(shlex.quote(arg) for arg in args)
    if quoted_args:
        return f"{REMOTE_BASE64_RESOLVER} {quoted_args}"
    return REMOTE_BASE64_RESOLVER


def ssh_keepalive_args() -> list[str]:
    """Return SSH options that send keepalive probes to prevent idle timeout."""
    return [
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
    ]


def wrap_remote_command(command: str) -> str:
    """Wrap a remote command via base64 to avoid all shell escaping issues.

    Instead of trying to escape special characters inside double quotes (which
    breaks with complex nested quotes and newlines), we base64-encode the
    entire command and pipe it through a resolved remote bash.

    This guarantees that *any* command content — including arbitrary quotes,
    backticks, newlines, and $-signs — arrives intact at the remote bash.

    Raises:
        ValueError: If the command exceeds ~95 KB after encoding, which would
            approach the Linux ``MAX_ARG_STRLEN`` limit (~131 KB).
    """
    import base64

    MAX_RAW_BYTES = 95_000  # ~131 KB MAX_ARG_STRLEN limit minus base64 overhead
    if len(command.encode()) > MAX_RAW_BYTES:
        raise ValueError(
            f"Command too large ({len(command.encode())} bytes). "
            f"Use pass_stdin=False to send via stdin instead of command-line arguments."
        )

    encoded = base64.b64encode(command.encode()).decode()
    return (
        f"printf '%s' '{encoded}' | {remote_base64_invocation('-d')} | "
        f"{remote_bash_invocation('--noprofile', '--norc')}"
    )


def build_ssh_base_args(
    *,
    bridge: BridgeProfile,
    proxy_cmd: str,
    batch_mode: bool = True,
    allocate_pty: bool = False,
    keepalive: bool = True,
) -> list[str]:
    """Build the base SSH argument list for subprocess SSH.

    Shared by subprocess-based execution. The Paramiko implementation
    uses its own connection API and does not call this function.
    """
    args = [
        "ssh",
        *([] if not bridge.identity_file else ["-i", bridge.identity_file]),
        *ssh_locale_args(),
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
    if keepalive:
        args[5:5] = ssh_keepalive_args()
    if not allocate_pty:
        args.append("-T")
    return args
