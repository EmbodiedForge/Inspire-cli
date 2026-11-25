"""SSH tunnel helpers: connection testing, ProxyCommand, ssh-config generation, and status."""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from inspire.config.toml_write import atomic_write_text

from .config import load_tunnel_config
from .models import BridgeProfile, TunnelConfig, TunnelError
from .rtunnel import _ensure_rtunnel_binary

logger = logging.getLogger(__name__)

INSPIRE_SSH_BLOCK_BEGIN = "# >>> Inspire Bridges (auto-generated) >>>"
INSPIRE_SSH_BLOCK_END = "# <<< Inspire Bridges (auto-generated) <<<"
INSPIRE_SSH_LEGACY_HEADER = "# Inspire Bridges (auto-generated)"

# ---------------------------------------------------------------------------
# ProxyCommand
# ---------------------------------------------------------------------------

_RTUNNEL_STDERR_REDACTION_SED = (
    r"s#([?&](token|access_token|refresh_token)=)[^&[:space:]\"<>]+#\1<redacted>#g; "
    r"s#(/(jupyter|vscode)/[^/]+/)[^/]+(/(proxy|lab)/)#\1<redacted>\3#g"
)


def _get_proxy_command(bridge: BridgeProfile, rtunnel_bin: Path, quiet: bool = False) -> str:
    """Build the ProxyCommand string for SSH.

    Args:
        bridge: Bridge profile with proxy_url
        rtunnel_bin: Path to rtunnel binary
        quiet: If True, suppress rtunnel stderr output (startup/shutdown messages)

    Returns:
        ProxyCommand string for SSH -o option
    """
    # Convert https:// URL to wss:// for websocket
    ws_url = _to_ws_url(bridge.proxy_url)

    # ProxyCommand is executed by a shell on the client; quote the URL because it
    # can contain characters like '?' (e.g. token query params) that some shells
    # treat as glob patterns.
    if quiet:
        # Wrap in sh -c to redirect stderr, suppressing rtunnel's verbose output
        cmd = f"{shlex.quote(str(rtunnel_bin))} {shlex.quote(ws_url)} stdio://%h:%p 2>/dev/null"
        return f"sh -c {shlex.quote(cmd)}"
    cmd = (
        '_err=$(mktemp "${TMPDIR:-/tmp}/inspire-rtunnel-stderr.XXXXXX") || exit 1; '
        "trap 'rm -f \"$_err\"' EXIT INT TERM; "
        f"{shlex.quote(str(rtunnel_bin))} {shlex.quote(ws_url)} "
        f"{shlex.quote('stdio://%h:%p')} 2>\"$_err\"; "
        "_rc=$?; "
        f'sed -E {shlex.quote(_RTUNNEL_STDERR_REDACTION_SED)} "$_err" >&2 || '
        'cat "$_err" >&2; '
        "exit $_rc"
    )
    return f"sh -c {shlex.quote(cmd)}"


def _to_ws_url(proxy_url: str) -> str:
    """Convert an http(s) proxy URL to ws(s)."""
    if proxy_url.startswith("https://"):
        return "wss://" + proxy_url[8:]
    if proxy_url.startswith("http://"):
        return "ws://" + proxy_url[7:]
    return proxy_url


def _build_rtunnel_listener_shell(
    *,
    rtunnel_bin: Path | str,
    proxy_url: str,
    target_command: str,
) -> str:
    """Build a bash wrapper that starts a per-invocation local rtunnel listener."""
    return (
        "set -e; "
        f"RTUNNEL_BIN={shlex.quote(str(rtunnel_bin))}; "
        f"RTUNNEL_URL={shlex.quote(_to_ws_url(proxy_url))}; "
        "pick_port(){ local candidate; "
        "for _ in $(seq 1 40); do "
        "candidate=$(( (RANDOM << 1 ^ RANDOM) % 16384 + 49152 )); "
        'if ! nc -z 127.0.0.1 "$candidate" >/dev/null 2>&1; then '
        'printf "%s" "$candidate"; return 0; '
        "fi; "
        "done; "
        "return 1; "
        "}; "
        'cleanup(){ if [ -n "${RT_PID:-}" ]; then kill "$RT_PID" >/dev/null 2>&1 || true; '
        'wait "$RT_PID" 2>/dev/null || true; RT_PID=; fi; }; '
        "trap cleanup EXIT INT TERM; "
        "for _attempt in $(seq 1 20); do "
        "LOCAL_PORT=$(pick_port) || break; "
        '"$RTUNNEL_BIN" "$RTUNNEL_URL" "127.0.0.1:$LOCAL_PORT" >/dev/null 2>&1 & '
        "RT_PID=$!; "
        "for _ in $(seq 1 50); do "
        'if nc -z 127.0.0.1 "$LOCAL_PORT" >/dev/null 2>&1; then '
        f"{target_command}; "
        "exit $?; "
        "fi; "
        'if ! kill -0 "$RT_PID" >/dev/null 2>&1; then break; '
        "fi; "
        "sleep 0.1; "
        "done; "
        "cleanup; "
        "done; "
        'echo "Failed to start local rtunnel listener" >&2; exit 1'
    )


def _identity_args(bridge: BridgeProfile) -> list[str]:
    identity_file = str(getattr(bridge, "identity_file", "") or "").strip()
    if not identity_file:
        return []
    return ["-i", identity_file]


def _ssh_locale_args() -> list[str]:
    return [
        "-F",
        "/dev/null",
        "-o",
        "SetEnv=LC_ALL=C",
        "-o",
        "SetEnv=LANG=C",
    ]


def _ssh_process_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update({"LC_ALL": "C", "LANG": "C"})
    return env


_SSH_PROBE_TEXT_LIMIT = 4000


@dataclass(frozen=True)
class SSHProbeAttempt:
    """One concrete SSH preflight command attempt."""

    label: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    timed_out: bool = False
    timeout: int | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and "ok" in self.stdout


@dataclass(frozen=True)
class SSHProbeResult:
    """Detailed SSH preflight result while preserving the old bool API."""

    bridge_name: str
    ok: bool
    attempts: list[SSHProbeAttempt] = field(default_factory=list)

    def format_failure(self) -> str:
        if self.ok:
            return "SSH preflight succeeded."

        lines = [f"SSH preflight failed for bridge '{self.bridge_name}'."]
        if not self.attempts:
            lines.append("No SSH probe attempts were recorded.")
            return "\n".join(lines)

        for attempt in self.attempts:
            status_parts: list[str] = []
            if attempt.timed_out:
                status_parts.append(f"timeout after {attempt.timeout}s")
            elif attempt.returncode is not None:
                status_parts.append(f"rc={attempt.returncode}")
            if attempt.error:
                status_parts.append(_excerpt_ssh_probe_text(attempt.error))
            status = "; ".join(part for part in status_parts if part) or "failed"
            lines.append(f"{attempt.label}: {status}")

            stderr = _excerpt_ssh_probe_text(attempt.stderr)
            if stderr:
                lines.append(f"{attempt.label} stderr:\n{_indent_probe_block(stderr)}")
            stdout = _excerpt_ssh_probe_text(attempt.stdout)
            if stdout:
                lines.append(f"{attempt.label} stdout:\n{_indent_probe_block(stdout)}")

        if not any(attempt.stdout or attempt.stderr or attempt.error for attempt in self.attempts):
            lines.append("No stdout/stderr was captured from ssh.")
        return "\n".join(lines)


def _redact_ssh_probe_text(text: str) -> str:
    value = str(text or "")
    if not value:
        return value

    query_token_re = re.compile(r"(?i)([?&](?:token|access_token|refresh_token)=)([^&\s]+)")
    path_token_re = re.compile(r"(/(?:jupyter|vscode)/[^/]+/)([^/]+)(/proxy/|/lab)")
    bearer_re = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s,}\]]+)")
    field_re = re.compile(
        r"""(?ix)
        (
            [\"']?
            (?:
                password|passwd|token|access[_-]?token|refresh[_-]?token|
                secret|api[_-]?key|authorization|cookie|set-cookie
            )
            [\"']?
            \s*[:=]\s*
        )
        (
            \"[^\"]*\" | '[^']*' | [^\s,}\]]+
        )
        """
    )
    value = bearer_re.sub(r"\1<redacted>", value)
    value = field_re.sub(r"\1<redacted>", value)
    value = query_token_re.sub(r"\1<redacted>", value)
    value = path_token_re.sub(r"\1<redacted>\3", value)
    return value


def _excerpt_ssh_probe_text(text: str, *, limit: int = _SSH_PROBE_TEXT_LIMIT) -> str:
    value = _redact_ssh_probe_text(text).strip()
    if not value:
        return ""
    value = "\n".join(line.rstrip() for line in value.splitlines()).strip()
    if len(value) > limit:
        value = f"...{value[-limit:]}"
    return value


def _indent_probe_block(text: str) -> str:
    return "\n".join(f"  {line}" if line else "  " for line in text.splitlines())


# ---------------------------------------------------------------------------
# Connection testing
# ---------------------------------------------------------------------------


def probe_ssh_connection(
    bridge: BridgeProfile,
    config: TunnelConfig,
    timeout: int = 10,
) -> SSHProbeResult:
    """Run SSH preflight and retain the concrete subprocess failure details."""
    attempts: list[SSHProbeAttempt] = []

    try:
        _ensure_rtunnel_binary(config)
    except TunnelError as exc:
        attempts.append(SSHProbeAttempt(label="rtunnel_binary", error=str(exc)))
        return SSHProbeResult(bridge_name=bridge.name, ok=False, attempts=attempts)

    proxy_cmd = _get_proxy_command(bridge, config.rtunnel_bin, quiet=True)
    primary_cmd = [
        "ssh",
        *_identity_args(bridge),
        *_ssh_locale_args(),
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
    ]
    primary = _run_ssh_probe_command(
        label="direct",
        cmd=primary_cmd,
        timeout=timeout + 5,
        stdin=subprocess.DEVNULL,
    )
    attempts.append(primary)
    if primary.ok:
        return SSHProbeResult(bridge_name=bridge.name, ok=True, attempts=attempts)

    from .ssh_exec import get_ssh_command_args

    try:
        fallback_cmd = get_ssh_command_args(
            bridge_name=bridge.name,
            config=config,
            remote_command="echo ok",
        )
    except Exception as exc:
        attempts.append(SSHProbeAttempt(label="wrapped", error=str(exc)))
        return SSHProbeResult(bridge_name=bridge.name, ok=False, attempts=attempts)

    fallback = _run_ssh_probe_command(
        label="wrapped",
        cmd=fallback_cmd,
        timeout=timeout + 8,
        stdin=subprocess.DEVNULL,
    )
    attempts.append(fallback)
    return SSHProbeResult(bridge_name=bridge.name, ok=fallback.ok, attempts=attempts)


def _run_ssh_probe_command(
    *,
    label: str,
    cmd: list[str],
    timeout: int,
    stdin: int | None = None,
) -> SSHProbeAttempt:
    try:
        result = subprocess.run(
            cmd,
            stdin=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_ssh_process_env(),
        )
        return SSHProbeAttempt(
            label=label,
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _timeout_output_text(exc.stdout)
        stderr = _timeout_output_text(exc.stderr)
        return SSHProbeAttempt(
            label=label,
            stdout=stdout,
            stderr=stderr,
            error=str(exc),
            timed_out=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return SSHProbeAttempt(label=label, error=str(exc), timeout=timeout)


def _timeout_output_text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    if isinstance(value, str):
        return value
    return ""


def _test_ssh_connection(
    bridge: BridgeProfile,
    config: TunnelConfig,
    timeout: int = 10,
) -> bool:
    """Test if SSH connection works.

    Args:
        bridge: Bridge profile to test
        config: Tunnel configuration (for rtunnel binary path)
        timeout: SSH connection timeout in seconds (default: 10)

    Returns:
        True if SSH connection succeeds, False otherwise.
    """
    result = probe_ssh_connection(bridge, config, timeout=timeout)
    if not result.ok:
        logger.debug("%s", result.format_failure())
    return result.ok


def probe_tunnel_available(
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    retries: int = 3,
    retry_pause: float = 2.0,
    progressive: bool = True,
    probe_timeout: int = 10,
) -> SSHProbeResult:
    """Check SSH availability with retries and return the final detailed result."""
    if config is None:
        config = load_tunnel_config()

    bridge = config.get_bridge(bridge_name)
    if not bridge:
        name = bridge_name or config.default_bridge or "<default>"
        return SSHProbeResult(
            bridge_name=name,
            ok=False,
            attempts=[SSHProbeAttempt(label="config", error=f"Bridge '{name}' not found.")],
        )

    last_result: SSHProbeResult | None = None
    for attempt in range(retries + 1):
        last_result = probe_ssh_connection(bridge, config, timeout=probe_timeout)
        if last_result.ok:
            return last_result
        if attempt < retries:
            pause = retry_pause + (attempt * 1.0) if progressive else retry_pause
            time.sleep(pause)
    return last_result or SSHProbeResult(bridge_name=bridge.name, ok=False, attempts=[])


def is_tunnel_available(
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    retries: int = 3,
    retry_pause: float = 2.0,
    progressive: bool = True,
    probe_timeout: int = 10,
) -> bool:
    """Check if SSH via ProxyCommand is available and responsive.

    Args:
        bridge_name: Name of bridge to check (uses default if None)
        config: Tunnel configuration (loads default if None)
        retries: Number of retries if SSH test fails (default: 3)
        retry_pause: Base pause between retries in seconds (default: 2.0)
        progressive: If True, increase pause with each retry (default: True)
        probe_timeout: Per-attempt SSH connect timeout in seconds (default: 10)

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
        if _test_ssh_connection(bridge, config, timeout=probe_timeout):
            return True
        if attempt < retries:
            # Progressive: 2s, 3s, 4s for attempts 0, 1, 2
            pause = retry_pause + (attempt * 1.0) if progressive else retry_pause
            time.sleep(pause)
    return False


# ---------------------------------------------------------------------------
# SSH config generation
# ---------------------------------------------------------------------------


def generate_ssh_config(
    bridge: BridgeProfile,
    rtunnel_path: Path,
    host_alias: Optional[str] = None,
) -> str:
    """Generate SSH config for ProxyCommand mode.

    Args:
        bridge: Bridge profile
        rtunnel_path: Path to rtunnel binary
        host_alias: SSH host alias to use (defaults to bridge name)

    Returns:
        SSH config string to add to ~/.ssh/config
    """
    if host_alias is None:
        host_alias = bridge.name

    proxy_cmd = _get_proxy_command(bridge, rtunnel_path)

    ssh_config = f"""Host {host_alias}
    HostName localhost
    User {bridge.ssh_user}
    Port {bridge.ssh_port}
    ProxyCommand {proxy_cmd}
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR"""

    if bridge.identity_file:
        ssh_config += f"\n    IdentityFile {bridge.identity_file}"

    return ssh_config


def generate_all_ssh_configs(config: TunnelConfig) -> str:
    """Generate SSH config for all bridges.

    Args:
        config: Tunnel configuration with all bridges

    Returns:
        SSH config string for all bridges
    """
    if not config.bridges:
        return ""

    rtunnel_path = _ensure_rtunnel_binary(config)
    configs = []
    for bridge in sorted(config.list_bridges(), key=lambda item: item.name):
        configs.append(generate_ssh_config(bridge, rtunnel_path))

    return "\n\n".join(configs)


def install_ssh_config(ssh_config: str, host_alias: str) -> dict:
    """Install SSH config to ~/.ssh/config.

    Args:
        ssh_config: SSH config block to add
        host_alias: Host alias to look for (for updating existing entries)

    Returns:
        Dict with keys:
        - success: bool
        - updated: bool (True if existing entry was updated)
        - error: Optional[str]
    """
    ssh_config_path = Path.home() / ".ssh" / "config"
    ssh_config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    existing_content = ""
    if ssh_config_path.exists():
        existing_content = ssh_config_path.read_text()

    blocks = _split_ssh_config_blocks(existing_content)
    updated = False
    rendered: list[str] = []
    for kind, block in blocks:
        if kind != "host":
            rendered.append(block)
            continue
        if host_alias in _host_aliases_from_block(block):
            if not updated:
                rendered.append(ssh_config.rstrip() + "\n")
                updated = True
            continue
        rendered.append(block)

    if not updated:
        content = "".join(rendered).rstrip()
        if content:
            content += "\n\n" + ssh_config.rstrip() + "\n"
        else:
            content = ssh_config.rstrip() + "\n"
        atomic_write_text(ssh_config_path, content)
        return {"success": True, "updated": False, "error": None}

    atomic_write_text(ssh_config_path, "".join(rendered))
    return {"success": True, "updated": True, "error": None}


def _split_ssh_config_blocks(content: str) -> list[tuple[str, str]]:
    """Split SSH config into ordered raw and Host blocks."""
    if not content:
        return []

    lines = content.splitlines(keepends=True)
    blocks: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        if lines[index].startswith("Host "):
            end = index + 1
            while end < len(lines) and not lines[end].startswith("Host "):
                end += 1
            blocks.append(("host", "".join(lines[index:end])))
            index = end
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("Host "):
            end += 1
        blocks.append(("raw", "".join(lines[index:end])))
        index = end
    return blocks


def _host_aliases_from_block(block: str) -> list[str]:
    first_line = block.splitlines()[0] if block.splitlines() else ""
    parts = first_line.strip().split()
    return parts[1:] if len(parts) > 1 else []


def _is_generated_inspire_host_block(block: str) -> bool:
    if not block.startswith("Host "):
        return False
    return (
        re.search(r"^\s*HostName\s+localhost\s*$", block, flags=re.MULTILINE) is not None
        and re.search(r"^\s*StrictHostKeyChecking\s+no\s*$", block, flags=re.MULTILINE) is not None
        and re.search(r"^\s*UserKnownHostsFile\s+/dev/null\s*$", block, flags=re.MULTILINE)
        is not None
        and re.search(r"^\s*LogLevel\s+ERROR\s*$", block, flags=re.MULTILINE) is not None
        and re.search(
            r"^\s*ProxyCommand\s+.*(?:rtunnel|localhost:\$PORT|localhost:%p).*$",
            block,
            flags=re.MULTILINE,
        )
        is not None
    )


def _strip_inspire_generated_entries(
    content: str,
    *,
    managed_aliases: Optional[set[str]] = None,
) -> str:
    """Remove managed and legacy Inspire-generated entries."""
    if not content:
        return ""

    managed_pattern = (
        rf"\n?{re.escape(INSPIRE_SSH_BLOCK_BEGIN)}\n.*?\n{re.escape(INSPIRE_SSH_BLOCK_END)}\n?"
    )
    content = re.sub(managed_pattern, "\n", content, flags=re.DOTALL)

    if INSPIRE_SSH_LEGACY_HEADER in content:
        prefix, suffix = content.split(INSPIRE_SSH_LEGACY_HEADER, 1)
        kept_suffix: list[str] = []
        dropping_legacy_hosts = True
        for kind, block in _split_ssh_config_blocks(suffix):
            if dropping_legacy_hosts and kind == "host":
                if _is_generated_inspire_host_block(block):
                    continue
                dropping_legacy_hosts = False
                kept_suffix.append(block)
                continue
            if dropping_legacy_hosts and kind == "raw":
                if block.strip():
                    dropping_legacy_hosts = False
                    kept_suffix.append(block)
                continue
            kept_suffix.append(block)
        content = prefix.rstrip()
        suffix_content = "".join(kept_suffix).lstrip("\n")
        if suffix_content:
            content = (content + "\n" + suffix_content) if content else suffix_content

    kept: list[str] = []
    for kind, block in _split_ssh_config_blocks(content):
        if kind == "raw":
            kept.append(block)
            continue
        if (
            managed_aliases
            and _is_generated_inspire_host_block(block)
            and managed_aliases.intersection(_host_aliases_from_block(block))
        ):
            continue
        kept.append(block)

    rendered = "".join(kept).rstrip()
    return (rendered + "\n") if rendered else ""


def has_installed_ssh_config(ssh_config_path: Optional[Path] = None) -> bool:
    """Return True when ~/.ssh/config already contains inspire-managed entries."""
    if ssh_config_path is None:
        ssh_config_path = Path.home() / ".ssh" / "config"
    if not ssh_config_path.exists():
        return False

    content = ssh_config_path.read_text()
    if INSPIRE_SSH_BLOCK_BEGIN in content or INSPIRE_SSH_LEGACY_HEADER in content:
        return True
    return any(
        kind == "host" and _is_generated_inspire_host_block(block)
        for kind, block in _split_ssh_config_blocks(content)
    )


def install_all_ssh_configs(
    config: TunnelConfig,
    *,
    removed_aliases: Optional[set[str]] = None,
) -> dict:
    """Install all bridge SSH configs as one managed block in ~/.ssh/config.

    ``removed_aliases`` names bridges that were just deleted from *config*. Their
    generated host blocks are cleaned up even though the profile is no longer in
    the config to identify them, while unrelated user-managed hosts survive.
    """
    ssh_config_path = Path.home() / ".ssh" / "config"
    ssh_config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    existing_content = ssh_config_path.read_text() if ssh_config_path.exists() else ""
    had_installed_entries = has_installed_ssh_config(ssh_config_path)
    managed_aliases = set(config.bridges.keys()) | set(removed_aliases or ())
    cleaned_content = _strip_inspire_generated_entries(
        existing_content,
        managed_aliases=managed_aliases,
    )
    all_configs = generate_all_ssh_configs(config) if config.bridges else ""

    managed_block = ""
    if all_configs:
        managed_block = f"{INSPIRE_SSH_BLOCK_BEGIN}\n{all_configs}\n{INSPIRE_SSH_BLOCK_END}\n"

    if cleaned_content and managed_block:
        final_content = cleaned_content.rstrip() + "\n\n" + managed_block
    elif cleaned_content:
        final_content = cleaned_content
    else:
        final_content = managed_block

    atomic_write_text(ssh_config_path, final_content)
    return {"success": True, "updated": had_installed_entries, "error": None}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


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
