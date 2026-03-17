"""SSH tunnel helpers: connection testing, ProxyCommand, ssh-config generation, and status."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional

from .config import load_tunnel_config
from .models import BridgeProfile, TunnelConfig, TunnelError
from .rtunnel import _ensure_rtunnel_binary

INSPIRE_SSH_BLOCK_BEGIN = "# >>> Inspire Bridges (auto-generated) >>>"
INSPIRE_SSH_BLOCK_END = "# <<< Inspire Bridges (auto-generated) <<<"
INSPIRE_SSH_LEGACY_HEADER = "# Inspire Bridges (auto-generated)"

# ---------------------------------------------------------------------------
# ProxyCommand
# ---------------------------------------------------------------------------


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
    return f"{shlex.quote(str(rtunnel_bin))} {shlex.quote(ws_url)} {shlex.quote('stdio://%h:%p')}"


def _to_ws_url(proxy_url: str) -> str:
    """Convert an http(s) proxy URL to ws(s)."""
    if proxy_url.startswith("https://"):
        return "wss://" + proxy_url[8:]
    if proxy_url.startswith("http://"):
        return "ws://" + proxy_url[7:]
    return proxy_url


def _build_rtunnel_cleanup_shell(port_var: str) -> str:
    """Return shell snippet that kills stale rtunnel listeners for the target port only."""
    return (
        "if command -v lsof >/dev/null 2>&1; then "
        f"for pid in $(lsof -nP -t -iTCP:${port_var} -sTCP:LISTEN 2>/dev/null); do "
        "cmd=$(ps -p \"$pid\" -o command= 2>/dev/null || true); "
        "echo \"$cmd\" | grep -qi rtunnel && kill \"$pid\" >/dev/null 2>&1 || true; "
        "done; "
        "fi; "
    )


# ---------------------------------------------------------------------------
# Connection testing
# ---------------------------------------------------------------------------


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

    Notes:
        First probes the direct ProxyCommand path. If that probe fails,
        falls back to the same local rtunnel + nc bootstrap pattern used by
        generated SSH aliases. This prevents false negatives where direct
        ProxyCommand probing is flaky but alias-based SSH is healthy.
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
        if result.returncode == 0 and "ok" in result.stdout:
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback probe: mirror the generated ssh-config alias/bootstrap wrapper
    # path used by `ssh <bridge>` and `get_ssh_command_args()`.
    from .ssh_exec import get_ssh_command_args

    try:
        fallback_cmd = get_ssh_command_args(
            bridge_name=bridge.name,
            config=config,
            remote_command="echo ok",
        )
        fallback = subprocess.run(
            fallback_cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 8,
        )
        return fallback.returncode == 0 and "ok" in (fallback.stdout or "")
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

    import shlex

    proxy_script = (
        f"RTUNNEL_BIN={shlex.quote(str(rtunnel_path))}; "
        f"RTUNNEL_URL={shlex.quote(_to_ws_url(bridge.proxy_url))}; "
        "PORT=%p; "
        f"{_build_rtunnel_cleanup_shell('PORT')}"
        "\"$RTUNNEL_BIN\" \"$RTUNNEL_URL\" \"localhost:$PORT\" >/dev/null 2>&1 & "
        "RT_PID=$!; "
        "cleanup(){ kill \"$RT_PID\" >/dev/null 2>&1 || true; }; "
        "trap cleanup EXIT INT TERM; "
        "for _ in $(seq 1 50); do "
        "nc -z 127.0.0.1 \"$PORT\" >/dev/null 2>&1 && break; "
        "sleep 0.1; "
        "done; "
        "exec nc 127.0.0.1 \"$PORT\""
    )
    proxy_cmd = f"sh -c {shlex.quote(proxy_script)}"

    ssh_config = f"""Host {host_alias}
    HostName localhost
    User {bridge.ssh_user}
    Port {bridge.ssh_port}
    ProxyCommand {proxy_cmd}
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR"""

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
    """Detect host blocks previously generated by inspire."""
    if not block.startswith("Host "):
        return False
    return (
        re.search(r"^\s*HostName\s+localhost\s*$", block, flags=re.MULTILINE) is not None
        and re.search(r"^\s*StrictHostKeyChecking\s+no\s*$", block, flags=re.MULTILINE) is not None
        and re.search(r"^\s*UserKnownHostsFile\s+/dev/null\s*$", block, flags=re.MULTILINE)
        is not None
        and re.search(r"^\s*LogLevel\s+ERROR\s*$", block, flags=re.MULTILINE) is not None
        and re.search(r"^\s*ProxyCommand\s+.*(?:rtunnel|localhost:\$PORT|localhost:%p).*$", block, flags=re.MULTILINE)
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


def install_all_ssh_configs(config: TunnelConfig) -> dict:
    """Install all bridge SSH configs as one managed block in ~/.ssh/config."""
    ssh_config_path = Path.home() / ".ssh" / "config"
    ssh_config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    existing_content = ssh_config_path.read_text() if ssh_config_path.exists() else ""
    had_installed_entries = has_installed_ssh_config(ssh_config_path)
    cleaned_content = _strip_inspire_generated_entries(
        existing_content,
        managed_aliases=set(config.bridges.keys()),
    )
    all_configs = generate_all_ssh_configs(config) if config.bridges else ""

    managed_block = ""
    if all_configs:
        managed_block = (
            f"{INSPIRE_SSH_BLOCK_BEGIN}\n"
            f"{all_configs}\n"
            f"{INSPIRE_SSH_BLOCK_END}\n"
        )

    if cleaned_content and managed_block:
        final_content = cleaned_content.rstrip() + "\n\n" + managed_block
    elif cleaned_content:
        final_content = cleaned_content
    else:
        final_content = managed_block

    ssh_config_path.write_text(final_content)
    return {"success": True, "updated": had_installed_entries, "error": None}


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
        ssh_config_path.write_text(content)
        return {"success": True, "updated": False, "error": None}

    ssh_config_path.write_text("".join(rendered))
    return {"success": True, "updated": True, "error": None}


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
