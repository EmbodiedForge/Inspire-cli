"""SSH tunnel utility module for Bridge access via ProxyCommand.

Provides functions to:
- Check if SSH via ProxyCommand is available
- Execute commands via SSH with ProxyCommand
- Manage tunnel configuration with multiple bridge profiles
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class TunnelError(Exception):
    """Base exception for tunnel-related errors."""


class TunnelNotAvailableError(TunnelError):
    """Raised when tunnel is not available or not running."""


class BridgeNotFoundError(TunnelError):
    """Raised when specified bridge profile is not found."""


# Default configuration
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_PORT = 22222
# nightly release includes stdio:// mode for SSH ProxyCommand support
DEFAULT_RTUNNEL_DOWNLOAD_URL = "https://github.com/Sarfflow/rtunnel/releases/download/nightly/rtunnel-linux-amd64.tar.gz"


def _get_rtunnel_download_url() -> str:
    """Get the rtunnel download URL from config or environment.

    Returns:
        Download URL for rtunnel binary
    """
    # Check environment variable first (highest priority)
    env_url = os.environ.get("INSPIRE_RTUNNEL_DOWNLOAD_URL")
    if env_url:
        return env_url

    # Try to load from config files
    try:
        from .config import Config

        config, _ = Config.from_files_and_env(
            require_credentials=False, require_target_dir=False
        )
        if config.rtunnel_download_url:
            return config.rtunnel_download_url
    except Exception:
        pass

    # Use default
    return DEFAULT_RTUNNEL_DOWNLOAD_URL


@dataclass
class BridgeProfile:
    """A single bridge configuration."""

    name: str
    proxy_url: str
    ssh_user: str = DEFAULT_SSH_USER
    ssh_port: int = DEFAULT_SSH_PORT

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "proxy_url": self.proxy_url,
            "ssh_user": self.ssh_user,
            "ssh_port": self.ssh_port,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BridgeProfile":
        return cls(
            name=data["name"],
            proxy_url=data["proxy_url"],
            ssh_user=data.get("ssh_user", DEFAULT_SSH_USER),
            ssh_port=data.get("ssh_port", DEFAULT_SSH_PORT),
        )


@dataclass
class TunnelConfig:
    """Tunnel configuration with multiple bridge profiles."""

    bridges: dict[str, BridgeProfile] = field(default_factory=dict)
    default_bridge: Optional[str] = None

    # Paths
    config_dir: Path = field(default_factory=lambda: Path.home() / ".inspire")

    @property
    def config_file(self) -> Path:
        return self.config_dir / "bridges.json"

    @property
    def rtunnel_bin(self) -> Path:
        return Path.home() / ".local" / "bin" / "rtunnel"

    def get_bridge(self, name: Optional[str] = None) -> Optional[BridgeProfile]:
        """Get a bridge profile by name, or the default if name is None."""
        if name:
            return self.bridges.get(name)
        elif self.default_bridge:
            return self.bridges.get(self.default_bridge)
        elif len(self.bridges) == 1:
            # If only one bridge, use it as default
            return next(iter(self.bridges.values()))
        return None

    def add_bridge(self, profile: BridgeProfile) -> None:
        """Add or update a bridge profile."""
        self.bridges[profile.name] = profile
        # Set as default if it's the first bridge
        if self.default_bridge is None:
            self.default_bridge = profile.name

    def remove_bridge(self, name: str) -> bool:
        """Remove a bridge profile. Returns True if removed."""
        if name in self.bridges:
            del self.bridges[name]
            if self.default_bridge == name:
                # Set new default
                self.default_bridge = next(iter(self.bridges.keys()), None)
            return True
        return False

    def list_bridges(self) -> list[BridgeProfile]:
        """List all bridge profiles."""
        return list(self.bridges.values())


def load_tunnel_config(config_dir: Optional[Path] = None) -> TunnelConfig:
    """Load tunnel configuration from ~/.inspire/bridges.json."""
    config = TunnelConfig()
    if config_dir:
        config.config_dir = config_dir

    config.config_dir.mkdir(parents=True, exist_ok=True)

    # Try new JSON format first
    if config.config_file.exists():
        try:
            with open(config.config_file) as f:
                data = json.load(f)
                config.default_bridge = data.get("default")
                for bridge_data in data.get("bridges", []):
                    profile = BridgeProfile.from_dict(bridge_data)
                    config.bridges[profile.name] = profile
        except (json.JSONDecodeError, KeyError):
            pass

    # Migrate from old format if new format is empty
    old_config_file = config.config_dir / "tunnel.conf"
    if not config.bridges and old_config_file.exists():
        proxy_url = None
        ssh_user = DEFAULT_SSH_USER
        with open(old_config_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key == "PROXY_URL":
                        proxy_url = value
                    elif key == "SSH_USER":
                        ssh_user = value

        if proxy_url:
            # Create a default bridge from old config
            profile = BridgeProfile(
                name="default",
                proxy_url=proxy_url,
                ssh_user=ssh_user,
            )
            config.add_bridge(profile)
            # Save in new format
            save_tunnel_config(config)

    return config


def save_tunnel_config(config: TunnelConfig) -> None:
    """Save tunnel configuration to ~/.inspire/bridges.json."""
    config.config_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "default": config.default_bridge,
        "bridges": [p.to_dict() for p in config.bridges.values()],
    }

    with open(config.config_file, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


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
        return f"{shlex.quote(str(rtunnel_bin))} {shlex.quote(ws_url)} {shlex.quote('stdio://%h:%p')}"


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
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={timeout}",
                "-o", f"ProxyCommand={proxy_cmd}",
                "-o", "LogLevel=ERROR",
                "-p", str(bridge.ssh_port),
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
    retries: int = 1,
) -> bool:
    """Check if SSH via ProxyCommand is available and responsive.

    Args:
        bridge_name: Name of bridge to check (uses default if None)
        config: Tunnel configuration (loads default if None)
        retries: Number of retries if SSH test fails (default: 1)

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
            time.sleep(1)  # Brief pause before retry
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
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        "-o", f"ProxyCommand={proxy_cmd}",
        "-o", "LogLevel=ERROR",
        "-p", str(bridge.ssh_port),
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
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ProxyCommand={proxy_cmd}",
        "-o", "LogLevel=ERROR",
        "-p", str(bridge.ssh_port),
        f"{bridge.ssh_user}@localhost",
    ]

    if remote_command:
        args.append(remote_command)

    return args


def _ensure_rtunnel_binary(config: TunnelConfig) -> Path:
    """Ensure rtunnel binary exists, download if needed."""
    if config.rtunnel_bin.exists() and os.access(config.rtunnel_bin, os.X_OK):
        return config.rtunnel_bin

    # Download rtunnel
    config.rtunnel_bin.parent.mkdir(parents=True, exist_ok=True)

    try:
        import tarfile
        import tempfile
        import urllib.request

        # Download tar.gz and extract
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            urllib.request.urlretrieve(_get_rtunnel_download_url(), tmp.name)
            with tarfile.open(tmp.name, "r:gz") as tar:
                # Extract the rtunnel binary (should be the only file or named rtunnel*)
                for member in tar.getmembers():
                    if member.isfile() and "rtunnel" in member.name:
                        # Extract to a temp location first
                        extracted = tar.extractfile(member)
                        if extracted:
                            config.rtunnel_bin.write_bytes(extracted.read())
                            config.rtunnel_bin.chmod(0o755)
                            break
            # Clean up temp file
            Path(tmp.name).unlink(missing_ok=True)

        if not config.rtunnel_bin.exists():
            raise TunnelError("rtunnel binary not found in archive")

        return config.rtunnel_bin
    except Exception as e:
        raise TunnelError(f"Failed to download rtunnel: {e}")


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


def get_rtunnel_path(config: Optional[TunnelConfig] = None) -> Path:
    """Get rtunnel binary path, downloading if needed.

    Args:
        config: Tunnel configuration

    Returns:
        Path to rtunnel binary

    Raises:
        TunnelError: If rtunnel cannot be found or downloaded
    """
    if config is None:
        config = load_tunnel_config()
    return _ensure_rtunnel_binary(config)


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

    # Convert https:// URL to wss:// for websocket
    proxy_url = bridge.proxy_url
    if proxy_url.startswith("https://"):
        ws_url = "wss://" + proxy_url[8:]
    elif proxy_url.startswith("http://"):
        ws_url = "ws://" + proxy_url[7:]
    else:
        ws_url = proxy_url

    ssh_config = f"""Host {host_alias}
    HostName localhost
    User {bridge.ssh_user}
    Port {bridge.ssh_port}
    ProxyCommand {rtunnel_path} {ws_url} stdio://%h:%p
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
    for bridge in config.list_bridges():
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
    import re

    ssh_config_path = Path.home() / ".ssh" / "config"
    ssh_config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    existing_content = ""
    if ssh_config_path.exists():
        existing_content = ssh_config_path.read_text()

    # Check if host alias already exists
    # Match "Host <alias>" at start of line, possibly with other hosts on same line
    host_pattern = rf"^Host\s+.*?\b{re.escape(host_alias)}\b.*$"
    match = re.search(host_pattern, existing_content, re.MULTILINE)

    if match:
        # Find the full block to replace (from Host line to next Host line or end)
        block_pattern = rf"(^Host\s+.*?\b{re.escape(host_alias)}\b.*$)((?:\n(?!Host\s).*)*)"
        new_content = re.sub(block_pattern, ssh_config, existing_content, flags=re.MULTILINE)

        ssh_config_path.write_text(new_content)
        return {"success": True, "updated": True, "error": None}
    else:
        # Append new entry
        if existing_content and not existing_content.endswith("\n"):
            existing_content += "\n"
        if existing_content:
            existing_content += "\n"

        ssh_config_path.write_text(existing_content + ssh_config + "\n")
        return {"success": True, "updated": False, "error": None}


def sync_via_ssh(
    target_dir: str,
    branch: str,
    commit_sha: str,
    force: bool = False,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: int = 60,
) -> dict:
    """Sync code on Bridge via SSH ProxyCommand.

    Runs git fetch && git checkout on the remote Bridge machine.

    Args:
        target_dir: Target directory on Bridge (INSPIRE_TARGET_DIR)
        branch: Branch to sync
        commit_sha: Expected commit SHA after sync
        force: If True, use git reset --hard (discard local changes)
        bridge_name: Name of bridge to use (uses default if None)
        config: Tunnel configuration
        timeout: Command timeout in seconds

    Returns:
        Dict with keys:
        - success: bool
        - synced_sha: Optional[str]
        - error: Optional[str]

    Raises:
        TunnelNotAvailableError: If no bridge configured
        BridgeNotFoundError: If specified bridge not found
    """
    if config is None:
        config = load_tunnel_config()

    # Build the sync command
    if force:
        sync_cmd = f"""
cd "{target_dir}" && \
git fetch --all && \
git checkout "{branch}" && \
git reset --hard "origin/{branch}" && \
git rev-parse HEAD
"""
    else:
        sync_cmd = f"""
cd "{target_dir}" && \
git fetch --all && \
git checkout "{branch}" && \
git pull --ff-only && \
git rev-parse HEAD
"""

    try:
        result = run_ssh_command(
            sync_cmd.strip(),
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
            capture_output=True,
            check=False,
        )

        if result.returncode == 0:
            # Extract the synced SHA from output (last line)
            lines = result.stdout.strip().split('\n')
            synced_sha = lines[-1].strip() if lines else ""

            return {
                "success": True,
                "synced_sha": synced_sha,
                "error": None,
            }
        else:
            error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            return {
                "success": False,
                "synced_sha": None,
                "error": error_msg,
            }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "synced_sha": None,
            "error": f"Sync command timed out after {timeout}s",
        }
    except Exception as e:
        return {
            "success": False,
            "synced_sha": None,
            "error": str(e),
        }

