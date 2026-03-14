"""Shell commands for bootstrapping rtunnel + SSH access on a notebook."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from inspire.config.rtunnel_defaults import (
    default_rtunnel_download_url,
    rtunnel_download_url_shell_snippet,
)
from inspire.config.ssh_runtime import (
    DEFAULT_RTUNNEL_DOWNLOAD_URL,
    SshRuntimeConfig,
    resolve_ssh_runtime_config,
)

BOOTSTRAP_SENTINEL = "/tmp/.inspire_rtunnel_bootstrap_v1"
SETUP_DONE_MARKER = "INSPIRE_RTUNNEL_SETUP_DONE"
SSHD_MISSING_MARKER = "INSPIRE_SSHD_INSTALL_FAILED"
SSH_SERVER_MISSING_MARKER = "INSPIRE_SSH_SERVER_MISSING"


@dataclass(frozen=True)
class RtunnelSetupPlan:
    bootstrap_mode: str
    bootstrap_strategy: str
    legacy_bootstrap: bool
    rtunnel_source: str
    skip_curl: bool
    rtunnel_bin_configured: bool
    contents_api_filename: str
    contents_api_filename_set: bool
    sshd_deb_dir_set: bool
    dropbear_deb_dir_set: bool
    apt_mirror_url_set: bool
    setup_script_set: bool
    sshd_deb_dir_ignored: bool

    def as_trace_dict(self, *, upload_policy: str) -> dict[str, object]:
        return {
            "bootstrap_mode": self.bootstrap_mode,
            "bootstrap_strategy": self.bootstrap_strategy,
            "legacy_bootstrap": self.legacy_bootstrap,
            "rtunnel_source": self.rtunnel_source,
            "upload_policy": upload_policy,
            "rtunnel_bin_configured": self.rtunnel_bin_configured,
            "contents_api_filename": self.contents_api_filename,
            "contents_api_filename_set": self.contents_api_filename_set,
            "sshd_deb_dir_set": self.sshd_deb_dir_set,
            "dropbear_deb_dir_set": self.dropbear_deb_dir_set,
            "apt_mirror_url_set": self.apt_mirror_url_set,
            "setup_script_set": self.setup_script_set,
            "sshd_deb_dir_ignored": self.sshd_deb_dir_ignored,
            "skip_curl": self.skip_curl,
        }


def _resolve_rtunnel_source(
    *,
    rtunnel_bin: Optional[str],
    contents_api_filename: Optional[str],
) -> str:
    if rtunnel_bin:
        return "configured_bin"
    if contents_api_filename:
        return "contents_api"
    return "download_or_existing_tmp"


def resolve_rtunnel_setup_plan(
    *,
    ssh_runtime: Optional[SshRuntimeConfig] = None,
    contents_api_filename: Optional[str] = None,
) -> RtunnelSetupPlan:
    if ssh_runtime is None:
        ssh_runtime = resolve_ssh_runtime_config()

    rtunnel_bin = ssh_runtime.rtunnel_bin
    sshd_deb_dir = ssh_runtime.sshd_deb_dir
    dropbear_deb_dir = ssh_runtime.dropbear_deb_dir
    apt_mirror_url = ssh_runtime.apt_mirror_url
    setup_script = ssh_runtime.setup_script

    if setup_script:
        bootstrap_mode = "dropbear"
        bootstrap_strategy = "dropbear_setup_script"
    elif dropbear_deb_dir:
        bootstrap_mode = "dropbear"
        bootstrap_strategy = "dropbear_bundle"
    elif apt_mirror_url:
        bootstrap_mode = "dropbear"
        bootstrap_strategy = "dropbear_mirror"
    elif sshd_deb_dir:
        bootstrap_mode = "openssh"
        bootstrap_strategy = "openssh_legacy_debs"
    else:
        bootstrap_mode = "openssh"
        bootstrap_strategy = "openssh_legacy_apt"

    legacy_bootstrap = bootstrap_mode == "openssh"
    skip_curl = bool(contents_api_filename or bootstrap_mode == "dropbear")

    return RtunnelSetupPlan(
        bootstrap_mode=bootstrap_mode,
        bootstrap_strategy=bootstrap_strategy,
        legacy_bootstrap=legacy_bootstrap,
        rtunnel_source=_resolve_rtunnel_source(
            rtunnel_bin=rtunnel_bin,
            contents_api_filename=contents_api_filename,
        ),
        skip_curl=skip_curl,
        rtunnel_bin_configured=bool(rtunnel_bin),
        contents_api_filename=contents_api_filename or "",
        contents_api_filename_set=bool(contents_api_filename),
        sshd_deb_dir_set=bool(sshd_deb_dir),
        dropbear_deb_dir_set=bool(dropbear_deb_dir),
        apt_mirror_url_set=bool(apt_mirror_url),
        setup_script_set=bool(setup_script),
        sshd_deb_dir_ignored=bool(sshd_deb_dir and bootstrap_mode == "dropbear"),
    )


def describe_rtunnel_setup_plan(
    *,
    ssh_runtime: Optional[SshRuntimeConfig] = None,
    contents_api_filename: Optional[str] = None,
) -> dict[str, object]:
    if ssh_runtime is None:
        ssh_runtime = resolve_ssh_runtime_config()
    plan = resolve_rtunnel_setup_plan(
        ssh_runtime=ssh_runtime,
        contents_api_filename=contents_api_filename,
    )
    return plan.as_trace_dict(upload_policy=ssh_runtime.rtunnel_upload_policy)


def _build_key_setup_line(ssh_public_key: Optional[str]) -> str:
    if ssh_public_key:
        ssh_public_key_escaped = ssh_public_key.replace("'", "'\"'\"'")
        return (
            "mkdir -p /root/.ssh && chmod 700 /root/.ssh && echo "
            f"'{ssh_public_key_escaped}' >> /root/.ssh/authorized_keys && chmod 600 "
            "/root/.ssh/authorized_keys"
        )
    return "mkdir -p /root/.ssh && chmod 700 /root/.ssh"


def _build_rtunnel_bin_lines(
    *,
    rtunnel_bin: Optional[str],
    contents_api_filename: Optional[str],
) -> list[str]:
    import shlex

    lines = [
        f"RTUNNEL_BIN_PATH={shlex.quote(rtunnel_bin or '')}",
        'RTUNNEL_BIN="/tmp/rtunnel-$PORT"',
    ]
    if rtunnel_bin:
        lines.append(
            'if [ -x "$RTUNNEL_BIN_PATH" ]; then RTUNNEL_BIN="$RTUNNEL_BIN_PATH"; '
            'elif [ -f "$RTUNNEL_BIN_PATH" ]; then cp "$RTUNNEL_BIN_PATH" "$RTUNNEL_BIN" '
            '&& chmod +x "$RTUNNEL_BIN"; fi'
        )

    if contents_api_filename:
        safe_name = shlex.quote(contents_api_filename)
        lines.append(
            f'for _d in . "$HOME"; do '
            f'if [ ! -x "$RTUNNEL_BIN" ] && [ -f "$_d"/{safe_name} ]; then '
            f'cp "$_d"/{safe_name} "$RTUNNEL_BIN" && chmod +x "$RTUNNEL_BIN" '
            "&& break; fi; done"
        )
    return lines


def _build_curl_rtunnel_block(*, skip_curl: bool) -> str:
    if skip_curl:
        return (
            'if [ ! -x "$RTUNNEL_BIN" ]; then '
            'echo "ERROR: rtunnel binary not found at $RTUNNEL_BIN '
            '(no curl fallback for offline notebooks)" >&2; fi'
        )
    return (
        'if [ ! -x "$RTUNNEL_BIN" ] && [ "$_INET" = 1 ]; then curl -fsSL '
        "--connect-timeout 10 --max-time 30 "
        '"$RTUNNEL_DOWNLOAD_URL" -o "$RTUNNEL_BIN.tgz" && '
        'tar -xzf "$RTUNNEL_BIN.tgz" -C /tmp && chmod +x "$RTUNNEL_BIN" '
        "2>/dev/null; fi"
    )


def _inet_probe_command() -> str:
    return (
        "_INET=0; timeout 3 bash -c "
        "'exec 3<>/dev/tcp/archive.ubuntu.com/80' 2>/dev/null && _INET=1"
    )


def _build_openssh_bootstrap_cmd(*, curl_rtunnel_block: str) -> str:
    return (
        'if [ ! -f "$BOOTSTRAP_SENTINEL" ] || [ ! -x "$RTUNNEL_BIN" ] '
        "|| [ ! -x /usr/sbin/sshd ]; then "
        f"{_inet_probe_command()}; "
        "if [ ! -x /usr/sbin/sshd ]; then "
        'if [ -n "${SSHD_DEB_DIR:-}" ] && ls "$SSHD_DEB_DIR"/*.deb >/dev/null 2>&1; then '
        'dpkg -i "$SSHD_DEB_DIR"/*.deb >/dev/null 2>&1 || true; '
        'elif [ -z "${SSHD_DEB_DIR:-}" ] && [ "$_INET" = 1 ]; then '
        "export DEBIAN_FRONTEND=noninteractive; "
        "timeout 30 apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=10 "
        "update -qq && "
        "timeout 30 apt-get install -y -qq openssh-server; fi; fi; "
        f"{curl_rtunnel_block}; "
        'if [ -x /usr/sbin/sshd ] && [ -x "$RTUNNEL_BIN" ]; then '
        'touch "$BOOTSTRAP_SENTINEL"; else rm -f "$BOOTSTRAP_SENTINEL"; fi; fi'
    )


def _build_ensure_rtunnel_cmd(*, curl_rtunnel_block: str) -> str:
    return (
        'if [ ! -x "$RTUNNEL_BIN" ] && [ -n "${RTUNNEL_BIN_PATH:-}" ] '
        '&& [ -x "$RTUNNEL_BIN_PATH" ]; then RTUNNEL_BIN="$RTUNNEL_BIN_PATH"; fi; '
        f"{curl_rtunnel_block}"
    )


def _build_start_sshd_cmd() -> str:
    ssh_listener_check = _build_ssh_listener_check()
    return (
        f"if [ -x /usr/sbin/sshd ] && ! {ssh_listener_check}; then "
        "mkdir -p /run/sshd && chmod 0755 /run/sshd; "
        "ssh-keygen -A >/dev/null 2>&1 || true; "
        '/usr/sbin/sshd -p "$SSH_PORT" -o ListenAddress=127.0.0.1 -o PermitRootLogin=yes '
        "-o PasswordAuthentication=no -o PubkeyAuthentication=yes "
        ">/dev/null 2>&1 & fi"
    )


def _build_start_dropbear_cmd() -> str:
    ssh_listener_check = _build_ssh_listener_check()
    return (
        f"if ! {ssh_listener_check}; then "
        'if [ -n "${DROPBEAR_DEB_DIR:-}" ] || [ -n "${APT_MIRROR_URL:-}" ]; then '
        'DB_BIN=""; '
        'if [ -n "${DROPBEAR_DEB_DIR:-}" ] && [ -x "$DROPBEAR_DEB_DIR/usr/sbin/dropbear" ]; then '
        'DB_BIN="$DROPBEAR_DEB_DIR/usr/sbin/dropbear"; '
        "export LD_LIBRARY_PATH="
        '"$DROPBEAR_DEB_DIR/lib/x86_64-linux-gnu:'
        "$DROPBEAR_DEB_DIR/usr/lib/x86_64-linux-gnu:"
        '${LD_LIBRARY_PATH:-}"; '
        '"$DB_BIN" -V >/dev/null 2>&1 || DB_BIN=""; fi; '
        'if [ -z "$DB_BIN" ] && [ -n "${DROPBEAR_DEB_DIR:-}" ] && '
        'ls "$DROPBEAR_DEB_DIR"/*.deb >/dev/null 2>&1; then '
        'dpkg -i "$DROPBEAR_DEB_DIR"/*.deb >/dev/null 2>&1 || true; '
        "[ -x /usr/sbin/dropbear ] && DB_BIN=/usr/sbin/dropbear; fi; "
        'if [ -z "$DB_BIN" ] || [ ! -x "$DB_BIN" ]; then '
        "[ -x /usr/sbin/dropbear ] && DB_BIN=/usr/sbin/dropbear; fi; "
        'if { [ -z "$DB_BIN" ] || [ ! -x "$DB_BIN" ]; } && [ -n "${APT_MIRROR_URL:-}" ]; then '
        'DISTRO_ID=$(. /etc/os-release 2>/dev/null && echo "${ID:-}"); '
        'MIRROR_DISTRO="${DISTRO_ID:-ubuntu}"; '
        '[ "$MIRROR_DISTRO" = "debian" ] || MIRROR_DISTRO="ubuntu"; '
        'CODENAME=$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-}"); '
        '[ -z "$CODENAME" ] && CODENAME=$(lsb_release -cs 2>/dev/null || true); '
        '[ -z "$CODENAME" ] && CODENAME=jammy; '
        'MIRROR_COMPONENTS="main restricted universe multiverse"; '
        '[ "${DISTRO_ID:-}" = "debian" ] && MIRROR_COMPONENTS="main"; '
        'MIRROR_URL="${APT_MIRROR_URL%/}"; '
        'case "$MIRROR_URL" in '
        '*/repository) MIRROR_URL="$MIRROR_URL/$MIRROR_DISTRO" ;; '
        "*/repository/debian|*/repository/ubuntu) true ;; "
        "esac; "
        'MIRROR_URL="$MIRROR_URL/"; '
        "for _f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list "
        "/etc/apt/sources.list.d/*.sources; do "
        '[ -f "$_f" ] && mv "$_f" "$_f.bak" 2>/dev/null; done; '
        'echo "deb $MIRROR_URL $CODENAME $MIRROR_COMPONENTS" '
        "> /etc/apt/sources.list.d/inspire-mirror.list; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "timeout 60 apt-get update -qq >/dev/null 2>&1 || true; "
        "dpkg --remove --force-remove-reinstreq openssh-server >/dev/null 2>&1 || true; "
        "timeout 60 apt-get install -y -qq dropbear-bin >/dev/null 2>&1 || true; "
        "for _f in /etc/apt/sources.list.bak /etc/apt/sources.list.d/*.list.bak "
        "/etc/apt/sources.list.d/*.sources.bak; do "
        '[ -f "$_f" ] && mv "$_f" "${_f%.bak}" 2>/dev/null; done; '
        "[ -x /usr/sbin/dropbear ] && DB_BIN=/usr/sbin/dropbear; fi; "
        'if [ -n "$DB_BIN" ] && [ -x "$DB_BIN" ]; then '
        'DB_KEY=""; '
        '[ -n "${DROPBEAR_DEB_DIR:-}" ] && [ -x "$DROPBEAR_DEB_DIR/usr/bin/dropbearkey" ] '
        '&& DB_KEY="$DROPBEAR_DEB_DIR/usr/bin/dropbearkey"; '
        '[ -z "$DB_KEY" ] && DB_KEY=$(which dropbearkey 2>/dev/null || true); '
        'if [ ! -f /tmp/dropbear_ed25519_host_key ] && [ -n "$DB_KEY" ] && [ -x "$DB_KEY" ]; then '
        '"$DB_KEY" -t ed25519 -f /tmp/dropbear_ed25519_host_key >/dev/null 2>&1; fi; '
        "if [ -f /tmp/dropbear_ed25519_host_key ]; then "
        '"$DB_BIN" -E -s -g -p "127.0.0.1:$SSH_PORT" '
        "-r /tmp/dropbear_ed25519_host_key -P /tmp/dropbear.pid "
        "2>>/tmp/dropbear.log & fi; fi; fi; fi"
    )


def _build_start_rtunnel_cmd() -> str:
    return (
        'if [ -x "$RTUNNEL_BIN" ] && ! ps -ef | '
        'grep -Eq "[r]tunnel .*([[:space:]]|:)$PORT([[:space:]]|$)"; then '
        'nohup "$RTUNNEL_BIN" "$SSH_PORT" "$PORT" '
        ">/tmp/rtunnel-server.log 2>&1 & fi"
    )


def _build_ssh_listener_check() -> str:
    return (
        '{ ss -ltnp 2>/dev/null | grep -Eq "127\\\\.0\\\\.0\\\\.1:${SSH_PORT}[[:space:]]|'
        '\\[::1\\]:${SSH_PORT}[[:space:]]|[[:space:]]:${SSH_PORT}[[:space:]]"'
        ' || ps -efww | grep -Eq "[d]ropbear.*-p.*${SSH_PORT}([[:space:]]|$)|'
        "[s]shd: .*-p ${SSH_PORT}([[:space:]]|$)|"
        '[s]shd -p ${SSH_PORT}([[:space:]]|$)"; }'
    )


def _build_ssh_server_status_cmd(*, include_sshd_marker: bool) -> str:
    status_check = _build_ssh_listener_check()
    parts: list[str] = []
    if include_sshd_marker:
        parts.append(f'if [ ! -x /usr/sbin/sshd ]; then echo "{SSHD_MISSING_MARKER}"; fi')
    parts.append(f'if {status_check}; then true; else echo "{SSH_SERVER_MISSING_MARKER}"; fi')
    return "; ".join(parts)


def build_rtunnel_setup_commands(
    *,
    port: int,
    ssh_port: int,
    ssh_public_key: Optional[str],
    ssh_runtime: Optional[SshRuntimeConfig] = None,
    contents_api_filename: Optional[str] = None,
) -> list[str]:
    import shlex

    if ssh_runtime is None:
        ssh_runtime = resolve_ssh_runtime_config()

    plan = resolve_rtunnel_setup_plan(
        ssh_runtime=ssh_runtime,
        contents_api_filename=contents_api_filename,
    )
    rtunnel_download_url = ssh_runtime.rtunnel_download_url or DEFAULT_RTUNNEL_DOWNLOAD_URL

    cmd_lines = [
        f"PORT={port}",
        f"SSH_PORT={ssh_port}",
        _build_key_setup_line(ssh_public_key),
        f"BOOTSTRAP_SENTINEL={BOOTSTRAP_SENTINEL}",
    ]

    cmd_lines.append(rtunnel_download_url_shell_snippet())
    try:
        auto_url = default_rtunnel_download_url()
    except ValueError:
        auto_url = None
    if auto_url is not None and rtunnel_download_url != auto_url:
        cmd_lines.append(f"RTUNNEL_DOWNLOAD_URL={shlex.quote(rtunnel_download_url)}")

    cmd_lines.extend(
        _build_rtunnel_bin_lines(
            rtunnel_bin=ssh_runtime.rtunnel_bin,
            contents_api_filename=contents_api_filename,
        )
    )

    if plan.bootstrap_strategy == "openssh_legacy_debs":
        cmd_lines.append(f"SSHD_DEB_DIR={shlex.quote(ssh_runtime.sshd_deb_dir or '')}")
    if plan.bootstrap_mode == "dropbear" and ssh_runtime.dropbear_deb_dir:
        cmd_lines.append(f"DROPBEAR_DEB_DIR={shlex.quote(ssh_runtime.dropbear_deb_dir)}")
    if plan.bootstrap_mode == "dropbear" and ssh_runtime.apt_mirror_url:
        cmd_lines.append(f"APT_MIRROR_URL={shlex.quote(ssh_runtime.apt_mirror_url)}")

    curl_rtunnel_block = _build_curl_rtunnel_block(skip_curl=plan.skip_curl)
    openssh_bootstrap_cmd = _build_openssh_bootstrap_cmd(curl_rtunnel_block=curl_rtunnel_block)
    ensure_rtunnel_cmd = _build_ensure_rtunnel_cmd(curl_rtunnel_block=curl_rtunnel_block)
    start_sshd_cmd = _build_start_sshd_cmd()
    start_dropbear_cmd = _build_start_dropbear_cmd()
    start_rtunnel_cmd = _build_start_rtunnel_cmd()

    if plan.bootstrap_mode == "dropbear":
        setup_script = ssh_runtime.setup_script
        if setup_script:
            cmd_lines.append(f"SETUP_SCRIPT={shlex.quote(setup_script)}")
            cmd_lines.append('RTUNNEL_URL="$RTUNNEL_DOWNLOAD_URL"')
            cmd_lines.append(
                '[ -f "$SETUP_SCRIPT" ] || echo "WARN: setup script not found: $SETUP_SCRIPT '
                '(falling back to dropbear bootstrap)"'
            )
            cmd_lines.append(
                'if [ -f "$SETUP_SCRIPT" ]; then '
                'if [ ! -f "$BOOTSTRAP_SENTINEL" ] || [ ! -x "$RTUNNEL_BIN" ]; then '
                'bash "$SETUP_SCRIPT" "$DROPBEAR_DEB_DIR" "$RTUNNEL_BIN_PATH" '
                '"$SSH_PORT" "$PORT" >/tmp/setup_ssh.log 2>&1; '
                'if [ $? -eq 0 ] && [ -x "$RTUNNEL_BIN" ]; then touch "$BOOTSTRAP_SENTINEL"; '
                'else rm -f "$BOOTSTRAP_SENTINEL"; fi; fi; '
                "else true; fi"
            )
            cmd_lines.append("tail -40 /tmp/setup_ssh.log 2>/dev/null || true")
        else:
            cmd_lines.append('RTUNNEL_URL="$RTUNNEL_DOWNLOAD_URL"')
            cmd_lines.append(ensure_rtunnel_cmd)
        cmd_lines.append(start_dropbear_cmd)
        cmd_lines.append(start_rtunnel_cmd)
        cmd_lines.append(_build_ssh_server_status_cmd(include_sshd_marker=False))
    else:
        cmd_lines.extend(
            [
                'RTUNNEL_URL="$RTUNNEL_DOWNLOAD_URL"',
                openssh_bootstrap_cmd,
                start_sshd_cmd,
                start_rtunnel_cmd,
                _build_ssh_server_status_cmd(include_sshd_marker=True),
            ]
        )

    cmd_lines.append(
        'if ps -ef | grep -Eq "[r]tunnel .*([[:space:]]|:)$PORT([[:space:]]|$)"; then '
        'echo "INSPIRE_RTUNNEL_STATUS=running"; '
        'else echo "INSPIRE_RTUNNEL_STATUS=not_running"; fi'
    )
    cmd_lines.append(f"echo {SETUP_DONE_MARKER}")

    return cmd_lines


__all__ = [
    "BOOTSTRAP_SENTINEL",
    "SETUP_DONE_MARKER",
    "SSHD_MISSING_MARKER",
    "SSH_SERVER_MISSING_MARKER",
    "RtunnelSetupPlan",
    "build_rtunnel_setup_commands",
    "describe_rtunnel_setup_plan",
    "resolve_rtunnel_setup_plan",
]
