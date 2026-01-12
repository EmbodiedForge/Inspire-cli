"""SSH tunnel utility module for Bridge access.

Provides functions to:
- Check if SSH tunnel is available
- Execute commands via SSH
- Start/stop rtunnel client
- Manage tunnel configuration
"""

import os
import signal
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
RTUNNEL_DOWNLOAD_URL = "https://github.com/Sarfflow/rtunnel/releases/download/v1.0.0/rtunnel-linux"


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


def _is_process_running(pid: int) -> bool:
    """Check if a process with given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _get_tunnel_pid(config: TunnelConfig) -> Optional[int]:
    """Get tunnel process PID from pid file."""
    if not config.pid_file.exists():
        return None

    try:
        pid = int(config.pid_file.read_text().strip())
        if _is_process_running(pid):
            return pid
        else:
            # Stale PID file, clean up
            config.pid_file.unlink(missing_ok=True)
            return None
    except (ValueError, FileNotFoundError):
        return None


def _test_ssh_connection(config: TunnelConfig, timeout: int = 10) -> bool:
    """Test if SSH connection works.

    Args:
        config: Tunnel configuration
        timeout: SSH connection timeout in seconds (default: 10)

    Returns:
        True if SSH connection succeeds, False otherwise
    """
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={timeout}",
                "-p", str(config.local_port),
                f"{config.ssh_user}@{config.ssh_host}",
                "echo ok",
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
        return result.returncode == 0 and "ok" in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def is_tunnel_available(config: Optional[TunnelConfig] = None, retries: int = 1) -> bool:
    """Check if SSH tunnel is running and responsive.

    Args:
        config: Tunnel configuration (loads default if None)
        retries: Number of retries if SSH test fails (default: 1)

    Returns:
        True if tunnel is available and SSH works, False otherwise
    """
    if config is None:
        config = load_tunnel_config()

    # Check if tunnel process is running
    pid = _get_tunnel_pid(config)
    if pid is None:
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
    """Execute a command on Bridge via SSH tunnel.

    Args:
        command: Shell command to execute on Bridge
        config: Tunnel configuration (loads default if None)
        timeout: Optional timeout in seconds
        capture_output: Whether to capture stdout/stderr
        check: Whether to raise on non-zero exit code

    Returns:
        CompletedProcess with result

    Raises:
        TunnelNotAvailableError: If tunnel is not available
        subprocess.TimeoutExpired: If command times out
        subprocess.CalledProcessError: If check=True and command fails
    """
    if config is None:
        config = load_tunnel_config()

    if not is_tunnel_available(config):
        raise TunnelNotAvailableError("SSH tunnel is not available")

    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-p", str(config.local_port),
        f"{config.ssh_user}@{config.ssh_host}",
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
    """Build SSH command arguments.

    Args:
        config: Tunnel configuration
        remote_command: Optional command to run (None for interactive shell)

    Returns:
        List of command arguments for subprocess
    """
    if config is None:
        config = load_tunnel_config()

    args = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-p", str(config.local_port),
        f"{config.ssh_user}@{config.ssh_host}",
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
        import urllib.request
        urllib.request.urlretrieve(RTUNNEL_DOWNLOAD_URL, config.rtunnel_bin)
        config.rtunnel_bin.chmod(0o755)
        return config.rtunnel_bin
    except Exception as e:
        raise TunnelError(f"Failed to download rtunnel: {e}")


def start_tunnel(
    proxy_url: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
) -> int:
    """Start the rtunnel client process.

    Args:
        proxy_url: rtunnel server URL (uses saved URL if None)
        config: Tunnel configuration

    Returns:
        PID of the started rtunnel process

    Raises:
        TunnelError: If rtunnel fails to start
    """
    if config is None:
        config = load_tunnel_config()

    # Use provided URL or fall back to saved
    url = proxy_url or config.proxy_url
    if not url:
        raise TunnelError(
            "No proxy URL provided. Use 'inspire tunnel set-url <URL>' first "
            "or provide URL with 'inspire tunnel start <URL>'"
        )

    # Save URL for future use
    if proxy_url:
        config.proxy_url = proxy_url
        save_tunnel_config(config)

    # Stop existing tunnel if running
    stop_tunnel(config)

    # Ensure rtunnel binary exists
    rtunnel_bin = _ensure_rtunnel_binary(config)

    # Start rtunnel
    config.config_dir.mkdir(parents=True, exist_ok=True)

    # Check for proxy env var mismatch (fail fast)
    env = os.environ.copy()
    for proxy_var in ("http_proxy", "https_proxy"):
        lower_val = env.get(proxy_var)
        upper_val = env.get(proxy_var.upper())
        if lower_val and upper_val and lower_val != upper_val:
            raise TunnelError(
                f"Proxy env mismatch: {proxy_var}={lower_val} but "
                f"{proxy_var.upper()}={upper_val}. "
                f"Run 'export {proxy_var.upper()}=\"${proxy_var}\"' to fix."
            )
        # Normalize: prefer lowercase, sync to uppercase for Go compatibility
        if lower_val:
            env[proxy_var.upper()] = lower_val
        elif upper_val:
            env[proxy_var] = upper_val

    with open(config.log_file, "w") as log_f:
        process = subprocess.Popen(
            [str(rtunnel_bin), url, str(config.local_port)],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )

    # Write PID file
    config.pid_file.write_text(str(process.pid))

    # Wait a moment for tunnel to establish
    time.sleep(2)

    # Verify tunnel started
    if not _is_process_running(process.pid):
        log_content = config.log_file.read_text() if config.log_file.exists() else ""
        raise TunnelError(f"Tunnel failed to start. Log:\n{log_content}")

    return process.pid


def stop_tunnel(config: Optional[TunnelConfig] = None) -> bool:
    """Stop the rtunnel client process.

    Args:
        config: Tunnel configuration

    Returns:
        True if process was stopped, False if not running
    """
    if config is None:
        config = load_tunnel_config()

    pid = _get_tunnel_pid(config)
    if pid is None:
        # Also try to kill any stray rtunnel processes
        subprocess.run(
            ["pkill", "-f", "rtunnel.*proxy"],
            capture_output=True,
        )
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait for process to terminate
        for _ in range(10):
            if not _is_process_running(pid):
                break
            time.sleep(0.2)
        else:
            # Force kill if still running
            os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass

    config.pid_file.unlink(missing_ok=True)
    return True


def get_tunnel_status(config: Optional[TunnelConfig] = None) -> dict:
    """Get comprehensive tunnel status.

    Returns:
        Dict with keys:
        - running: bool
        - pid: Optional[int]
        - ssh_works: bool
        - proxy_url: Optional[str]
        - local_port: int
        - error: Optional[str]
    """
    if config is None:
        config = load_tunnel_config()

    status = {
        "running": False,
        "pid": None,
        "ssh_works": False,
        "proxy_url": config.proxy_url,
        "local_port": config.local_port,
        "error": None,
    }

    pid = _get_tunnel_pid(config)
    if pid is not None:
        status["running"] = True
        status["pid"] = pid

        # Test SSH connection
        status["ssh_works"] = _test_ssh_connection(config)
        if not status["ssh_works"]:
            status["error"] = "Tunnel running but SSH connection failed"
    else:
        if not config.proxy_url:
            status["error"] = "No proxy URL configured"

    return status


def sync_via_ssh(
    target_dir: str,
    branch: str,
    commit_sha: str,
    force: bool = False,
    config: Optional[TunnelConfig] = None,
    timeout: int = 60,
) -> dict:
    """Sync code on Bridge via SSH tunnel.

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
        TunnelNotAvailableError: If tunnel is not available
    """
    if config is None:
        config = load_tunnel_config()

    if not is_tunnel_available(config):
        raise TunnelNotAvailableError("SSH tunnel is not available")

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

