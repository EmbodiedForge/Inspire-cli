"""SSH tunnel utility module for Bridge access via ProxyCommand.

Provides functions to:
- Check if SSH via ProxyCommand is available
- Execute commands via SSH with ProxyCommand
- Manage tunnel configuration
"""

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


# Default configuration
DEFAULT_LOCAL_PORT = 2222
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_HOST = "localhost"
# nightly release includes stdio:// mode for SSH ProxyCommand support
RTUNNEL_DOWNLOAD_URL = "https://github.com/Sarfflow/rtunnel/releases/download/nightly/rtunnel-linux-amd64.tar.gz"


@dataclass
class TunnelConfig:
    """Tunnel configuration."""

    proxy_url: Optional[str] = None
    local_port: int = DEFAULT_LOCAL_PORT
    ssh_user: str = DEFAULT_SSH_USER
    ssh_host: str = DEFAULT_SSH_HOST

    # Paths
    config_dir: Path = field(default_factory=lambda: Path.home() / ".inspire")

    @property
    def pid_file(self) -> Path:
        return self.config_dir / "tunnel.pid"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "tunnel.conf"

    @property
    def log_file(self) -> Path:
        return self.config_dir / "tunnel.log"

    @property
    def rtunnel_bin(self) -> Path:
        return Path.home() / ".local" / "bin" / "rtunnel"


def load_tunnel_config(config_dir: Optional[Path] = None) -> TunnelConfig:
    """Load tunnel configuration from ~/.inspire/tunnel.conf."""
    config = TunnelConfig()
    if config_dir:
        config.config_dir = config_dir

    config.config_dir.mkdir(parents=True, exist_ok=True)

    if config.config_file.exists():
        # Parse shell-style config file
        with open(config.config_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")

                    if key == "PROXY_URL":
                        config.proxy_url = value
                    elif key == "LOCAL_PORT":
                        config.local_port = int(value)
                    elif key == "SSH_USER":
                        config.ssh_user = value
                    elif key == "SSH_HOST":
                        config.ssh_host = value

    return config


def save_tunnel_config(config: TunnelConfig) -> None:
    """Save tunnel configuration to ~/.inspire/tunnel.conf."""
    config.config_dir.mkdir(parents=True, exist_ok=True)

    with open(config.config_file, "w") as f:
        f.write("# Inspire SSH Tunnel Configuration\n")
        f.write("# This file is auto-generated. Edit with care.\n\n")
        if config.proxy_url:
            f.write(f'PROXY_URL="{config.proxy_url}"\n')
        f.write(f"LOCAL_PORT={config.local_port}\n")
        f.write(f'SSH_USER="{config.ssh_user}"\n')
        f.write(f'SSH_HOST="{config.ssh_host}"\n')


def _get_proxy_command(config: TunnelConfig) -> str:
    """Build the ProxyCommand string for SSH.

    Args:
        config: Tunnel configuration with proxy_url and rtunnel_bin

    Returns:
        ProxyCommand string for SSH -o option
    """
    # Convert https:// URL to wss:// for websocket
    proxy_url = config.proxy_url or ""
    if proxy_url.startswith("https://"):
        ws_url = "wss://" + proxy_url[8:]
    elif proxy_url.startswith("http://"):
        ws_url = "ws://" + proxy_url[7:]
    else:
        ws_url = proxy_url

    return f"{config.rtunnel_bin} {ws_url} stdio://%h:%p"


def _test_ssh_connection(config: TunnelConfig, timeout: int = 10) -> bool:
    """Test if SSH connection works via ProxyCommand.

    Args:
        config: Tunnel configuration
        timeout: SSH connection timeout in seconds (default: 10)

    Returns:
        True if SSH connection succeeds, False otherwise
    """
    if not config.proxy_url:
        return False

    # Ensure rtunnel binary exists
    try:
        _ensure_rtunnel_binary(config)
    except TunnelError:
        return False

    proxy_cmd = _get_proxy_command(config)

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
                "-p", "22222",
                f"{config.ssh_user}@localhost",
                "echo ok",
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
        return result.returncode == 0 and "ok" in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def is_tunnel_available(config: Optional[TunnelConfig] = None, retries: int = 1) -> bool:
    """Check if SSH via ProxyCommand is available and responsive.

    Args:
        config: Tunnel configuration (loads default if None)
        retries: Number of retries if SSH test fails (default: 1)

    Returns:
        True if SSH via ProxyCommand works, False otherwise
    """
    if config is None:
        config = load_tunnel_config()

    # Check if proxy URL is configured
    if not config.proxy_url:
        return False

    # Test SSH connection with retry
    for attempt in range(retries + 1):
        if _test_ssh_connection(config):
            return True
        if attempt < retries:
            time.sleep(1)  # Brief pause before retry
    return False


def run_ssh_command(
    command: str,
    config: Optional[TunnelConfig] = None,
    timeout: Optional[int] = None,
    capture_output: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Execute a command on Bridge via SSH ProxyCommand.

    Args:
        command: Shell command to execute on Bridge
        config: Tunnel configuration (loads default if None)
        timeout: Optional timeout in seconds
        capture_output: Whether to capture stdout/stderr
        check: Whether to raise on non-zero exit code

    Returns:
        CompletedProcess with result

    Raises:
        TunnelNotAvailableError: If tunnel is not configured
        subprocess.TimeoutExpired: If command times out
        subprocess.CalledProcessError: If check=True and command fails
    """
    if config is None:
        config = load_tunnel_config()

    if not config.proxy_url:
        raise TunnelNotAvailableError(
            "No proxy URL configured. Run 'inspire tunnel set-url <URL>' first."
        )

    # Ensure rtunnel binary exists
    _ensure_rtunnel_binary(config)

    proxy_cmd = _get_proxy_command(config)

    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        "-o", f"ProxyCommand={proxy_cmd}",
        "-o", "LogLevel=ERROR",
        "-p", "22222",
        f"{config.ssh_user}@localhost",
        command,
    ]

    return subprocess.run(
        ssh_cmd,
        capture_output=capture_output,
        text=True,
        timeout=timeout,
        check=check,
    )


def get_ssh_command_args(
    config: Optional[TunnelConfig] = None,
    remote_command: Optional[str] = None,
) -> list[str]:
    """Build SSH command arguments with ProxyCommand.

    Args:
        config: Tunnel configuration
        remote_command: Optional command to run (None for interactive shell)

    Returns:
        List of command arguments for subprocess

    Raises:
        TunnelNotAvailableError: If tunnel is not configured
    """
    if config is None:
        config = load_tunnel_config()

    if not config.proxy_url:
        raise TunnelNotAvailableError(
            "No proxy URL configured. Run 'inspire tunnel set-url <URL>' first."
        )

    # Ensure rtunnel binary exists
    _ensure_rtunnel_binary(config)

    proxy_cmd = _get_proxy_command(config)

    args = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ProxyCommand={proxy_cmd}",
        "-o", "LogLevel=ERROR",
        "-p", "22222",
        f"{config.ssh_user}@localhost",
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
            urllib.request.urlretrieve(RTUNNEL_DOWNLOAD_URL, tmp.name)
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


def get_tunnel_status(config: Optional[TunnelConfig] = None) -> dict:
    """Get tunnel status (ProxyCommand mode).

    Returns:
        Dict with keys:
        - configured: bool (proxy URL is set)
        - ssh_works: bool
        - proxy_url: Optional[str]
        - rtunnel_path: Optional[str]
        - error: Optional[str]
    """
    if config is None:
        config = load_tunnel_config()

    status = {
        "configured": bool(config.proxy_url),
        "ssh_works": False,
        "proxy_url": config.proxy_url,
        "rtunnel_path": str(config.rtunnel_bin) if config.rtunnel_bin.exists() else None,
        "error": None,
    }

    if not config.proxy_url:
        status["error"] = "No proxy URL configured. Run 'inspire tunnel set-url <URL>' first."
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
    status["ssh_works"] = _test_ssh_connection(config)
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
    config: TunnelConfig,
    host_alias: str = "inspire-bridge",
    rtunnel_path: Optional[Path] = None,
) -> str:
    """Generate SSH config for ProxyCommand mode.

    Args:
        config: Tunnel configuration with proxy_url
        host_alias: SSH host alias to use
        rtunnel_path: Path to rtunnel binary

    Returns:
        SSH config string to add to ~/.ssh/config
    """
    if rtunnel_path is None:
        rtunnel_path = config.rtunnel_bin

    # Convert https:// URL to wss:// for websocket
    proxy_url = config.proxy_url or ""
    if proxy_url.startswith("https://"):
        ws_url = "wss://" + proxy_url[8:]
    elif proxy_url.startswith("http://"):
        ws_url = "ws://" + proxy_url[7:]
    else:
        ws_url = proxy_url

    # The target is the sshd on the Bridge (localhost:22222 from rtunnel server's perspective)
    # In stdio mode, we use stdio://%h:%p which will be replaced by SSH with the target host:port
    # But since we're tunneling to a fixed sshd, we use the actual target
    ssh_config = f"""Host {host_alias}
    HostName localhost
    User {config.ssh_user}
    Port 22222
    ProxyCommand {rtunnel_path} {ws_url} stdio://%h:%p
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR"""

    return ssh_config


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
        config: Tunnel configuration
        timeout: Command timeout in seconds

    Returns:
        Dict with keys:
        - success: bool
        - synced_sha: Optional[str]
        - error: Optional[str]

    Raises:
        TunnelNotAvailableError: If tunnel is not configured
    """
    if config is None:
        config = load_tunnel_config()

    if not config.proxy_url:
        raise TunnelNotAvailableError(
            "No proxy URL configured. Run 'inspire tunnel set-url <URL>' first."
        )

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

