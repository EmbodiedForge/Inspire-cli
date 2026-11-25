"""Config options: SSH, Tunnel, Bridge, and Paths."""

from __future__ import annotations

from inspire.config.rtunnel_defaults import DEFAULT_RTUNNEL_DOWNLOAD_URL
from inspire.config.schema_models import (
    ConfigOption,
    _parse_float,
    _parse_int,
    _parse_list,
    _parse_upload_policy,
)

SSH_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_RTUNNEL_BIN",
        toml_key="ssh.rtunnel_bin",
        field_name="rtunnel_bin",
        description="Path to rtunnel binary",
        default=None,
        category="SSH",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_SSHD_DEB_DIR",
        toml_key="ssh.sshd_deb_dir",
        field_name="sshd_deb_dir",
        description="Directory containing sshd deb package",
        default=None,
        category="SSH",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_DROPBEAR_DEB_DIR",
        toml_key="ssh.dropbear_deb_dir",
        field_name="dropbear_deb_dir",
        description="Directory containing dropbear deb package",
        default=None,
        category="SSH",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_SETUP_SCRIPT",
        toml_key="ssh.setup_script",
        field_name="setup_script",
        description="Path to SSH setup script on the cluster",
        default=None,
        category="SSH",
        scope="global",
        secret=True,
    ),
    ConfigOption(
        env_var="INSPIRE_RTUNNEL_DOWNLOAD_URL",
        toml_key="ssh.rtunnel_download_url",
        field_name="rtunnel_download_url",
        description="Download URL for rtunnel binary",
        default=DEFAULT_RTUNNEL_DOWNLOAD_URL,
        category="SSH",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_APT_MIRROR_URL",
        toml_key="ssh.apt_mirror_url",
        field_name="apt_mirror_url",
        description="APT mirror URL for offline dropbear installation (e.g. http://nexus.example/repository/ubuntu/)",
        default=None,
        category="SSH",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_RTUNNEL_UPLOAD_POLICY",
        toml_key="ssh.rtunnel_upload_policy",
        field_name="rtunnel_upload_policy",
        description="Rtunnel upload fallback policy: auto (default), never, or always",
        default="auto",
        category="SSH",
        scope="global",
        parser=_parse_upload_policy,
        value_type=str,
    ),
    ConfigOption(
        env_var="INSPIRE_SSH_HOST_KEY_DIR",
        toml_key="ssh.host_key_dir",
        field_name="host_key_dir",
        description=(
            "Persistent remote directory for the dropbear host key. "
            "Defaults to <target_dir>/.inspire/ssh/dropbear so the key survives "
            "notebook restarts (avoid ephemeral /etc and /tmp)."
        ),
        default=None,
        category="SSH",
        scope="global",
    ),
]

TUNNEL_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_TUNNEL_RETRIES",
        toml_key="tunnel.retries",
        field_name="tunnel_retries",
        description="SSH tunnel connection retries",
        default=3,
        category="Tunnel",
        parser=_parse_int,
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_TUNNEL_RETRY_PAUSE",
        toml_key="tunnel.retry_pause",
        field_name="tunnel_retry_pause",
        description="Seconds to wait between SSH tunnel retries",
        default=2.0,
        category="Tunnel",
        parser=_parse_float,
        scope="global",
    ),
]

BRIDGE_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_BRIDGE_ACTION_TIMEOUT",
        toml_key="bridge.action_timeout",
        field_name="bridge_action_timeout",
        description="Bridge action timeout in seconds",
        default=600,
        category="Bridge",
        parser=_parse_int,
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_BRIDGE_DENYLIST",
        toml_key="bridge.denylist",
        field_name="bridge_action_denylist",
        description="Glob patterns to block from sync (comma/newline separated)",
        default=[],
        category="Bridge",
        parser=_parse_list,
        scope="project",
    ),
]

PATHS_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_TARGET_DIR_LEGACY",
        toml_key="paths.target_dir",
        field_name="target_dir",
        description=(
            "Legacy target directory on Bridge shared filesystem "
            "(deprecated: use defaults.target_dir and INSPIRE_TARGET_DIR)"
        ),
        default=None,
        category="Paths",
        scope="project",
    ),
    ConfigOption(
        env_var="INSP_LOG_CACHE_DIR",
        toml_key="paths.log_cache_dir",
        field_name="log_cache_dir",
        description="Cache directory for remote logs",
        default="~/.inspire/logs",
        category="Paths",
        scope="global",
    ),
]
