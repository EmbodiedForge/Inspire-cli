"""Runtime SSH option resolution for notebook and tunnel flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from inspire.config.env import resolve_remote_env
from inspire.config.models import Config
from inspire.config.rtunnel_defaults import DEFAULT_RTUNNEL_DOWNLOAD_URL
from inspire.config.schema_models import _parse_upload_policy


@dataclass(frozen=True)
class SshRuntimeConfig:
    """Resolved SSH runtime options used by notebook and tunnel workflows."""

    rtunnel_bin: Optional[str] = None
    sshd_deb_dir: Optional[str] = None
    dropbear_deb_dir: Optional[str] = None
    setup_script: Optional[str] = None
    rtunnel_download_url: str = DEFAULT_RTUNNEL_DOWNLOAD_URL
    apt_mirror_url: Optional[str] = None
    rtunnel_upload_policy: str = "auto"
    host_key_dir: Optional[str] = None
    ssh_port: int = 22222


def _shared_root_from_target(target_dir: str) -> str | None:
    """Reduce a project path under the shared volume to its writable user root.

    Target dirs are typically ``/inspire/hdd/global_user/<user>/<...>``.  The
    project sub-path may not exist yet (or may be intentionally scoped), so the
    dropbear host key should live on the *user's* shared root which is always a
    writable ``rw`` mount.  Falls back to the target dir itself when no shared
    marker is present.
    """
    stripped = str(target_dir or "").strip().rstrip("/")
    if not stripped:
        return None
    parts = stripped.split("/")
    for marker in ("global_user", "global_public", "hdd", "ssd"):
        if marker in parts:
            idx = parts.index(marker)
            # Keep up to and including the entry after the marker (e.g. the user).
            if idx + 1 < len(parts):
                return "/" + "/".join(parts[1 : idx + 2])
    return stripped


def resolve_ssh_runtime_config(
    *,
    cli_overrides: Optional[Mapping[str, Optional[str]]] = None,
) -> SshRuntimeConfig:
    """Resolve SSH runtime configuration with layered precedence.

    The base resolution is delegated to Config.from_files_and_env(), which already applies
    cli.prefer_source behavior between project TOML and environment variables.
    CLI overrides are then applied as the highest-priority layer.
    """
    config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)

    # Persistent dropbear host-key directory on the shared volume by default,
    # so the key survives notebook restarts (ephemeral /etc and /tmp do not).
    # Prefer the user's writable shared root over target_dir (which is a project
    # sub-path that may not exist yet or may be read-only).
    base = (config.remote_runtime_cwd or config.target_dir or "").strip()
    shared_root = _shared_root_from_target(base)
    default_host_key_dir = f"{shared_root}/.inspire/ssh/dropbear" if shared_root else None

    apt_mirror_url = config.apt_mirror_url
    if not apt_mirror_url:
        raw_remote_apt_mirror = config.remote_env.get("APT_MIRROR_URL")
        if raw_remote_apt_mirror is not None:
            apt_mirror_url = resolve_remote_env({"APT_MIRROR_URL": raw_remote_apt_mirror}).get(
                "APT_MIRROR_URL"
            )

    values: dict[str, Optional[str]] = {
        "rtunnel_bin": config.rtunnel_bin,
        "sshd_deb_dir": config.sshd_deb_dir,
        "dropbear_deb_dir": config.dropbear_deb_dir,
        "setup_script": config.setup_script,
        "rtunnel_download_url": config.rtunnel_download_url or DEFAULT_RTUNNEL_DOWNLOAD_URL,
        "apt_mirror_url": apt_mirror_url,
        "rtunnel_upload_policy": config.rtunnel_upload_policy,
        "host_key_dir": config.host_key_dir or default_host_key_dir,
    }

    if cli_overrides:
        for key in values:
            override = cli_overrides.get(key)
            if override is not None:
                values[key] = override

    download_url = values["rtunnel_download_url"] or DEFAULT_RTUNNEL_DOWNLOAD_URL
    upload_policy_value = values["rtunnel_upload_policy"]
    if upload_policy_value is None:
        upload_policy_value = "auto"
    # Re-validate: CLI overrides bypass TOML validation
    upload_policy = _parse_upload_policy(upload_policy_value)

    return SshRuntimeConfig(
        rtunnel_bin=values["rtunnel_bin"],
        sshd_deb_dir=values["sshd_deb_dir"],
        dropbear_deb_dir=values["dropbear_deb_dir"],
        setup_script=values["setup_script"],
        rtunnel_download_url=download_url,
        apt_mirror_url=values["apt_mirror_url"],
        rtunnel_upload_policy=upload_policy,
        host_key_dir=values["host_key_dir"],
        ssh_port=getattr(config, "ssh_port", 22222),
    )


__all__ = [
    "DEFAULT_RTUNNEL_DOWNLOAD_URL",
    "SshRuntimeConfig",
    "resolve_ssh_runtime_config",
]
