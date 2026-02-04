"""Config options: SSH."""

from __future__ import annotations

from inspire.cli.utils.config_schema_models import ConfigOption

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
        default=(
            "https://github.com/Sarfflow/rtunnel/releases/download/nightly/"
            "rtunnel-linux-amd64.tar.gz"
        ),
        category="SSH",
        scope="global",
    ),
]
