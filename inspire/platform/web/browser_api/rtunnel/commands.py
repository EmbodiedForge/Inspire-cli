"""Shell commands for bootstrapping rtunnel + sshd/dropbear on a notebook."""

from __future__ import annotations

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

    if ssh_public_key:
        ssh_public_key_escaped = ssh_public_key.replace("'", "'\"'\"'")
        key_line = (
            "mkdir -p /root/.ssh && chmod 700 /root/.ssh && echo "
            f"'{ssh_public_key_escaped}' >> /root/.ssh/authorized_keys && chmod 600 "
            "/root/.ssh/authorized_keys"
        )
    else:
        key_line = "mkdir -p /root/.ssh && chmod 700 /root/.ssh"

    rtunnel_bin = ssh_runtime.rtunnel_bin
    sshd_deb_dir = ssh_runtime.sshd_deb_dir
    dropbear_deb_dir = ssh_runtime.dropbear_deb_dir
    rtunnel_download_url = ssh_runtime.rtunnel_download_url or DEFAULT_RTUNNEL_DOWNLOAD_URL

    cmd_lines = [
        f"PORT={port}",
        f"SSH_PORT={ssh_port}",
        key_line,
        f"BOOTSTRAP_SENTINEL={BOOTSTRAP_SENTINEL}",
    ]

    # Set $RTUNNEL_DOWNLOAD_URL via uname for the remote container
    cmd_lines.append(rtunnel_download_url_shell_snippet())

    # If user explicitly configured a URL, override the dynamic detection
    try:
        auto_url = default_rtunnel_download_url()
    except ValueError:
        auto_url = None
    if auto_url is not None and rtunnel_download_url != auto_url:
        cmd_lines.append(f"RTUNNEL_DOWNLOAD_URL={shlex.quote(rtunnel_download_url)}")

    # Always set RTUNNEL_BIN_PATH (empty string if not configured)
    cmd_lines.append(f"RTUNNEL_BIN_PATH={shlex.quote(rtunnel_bin or '')}")
    if rtunnel_bin:
        cmd_lines.append(
            'if [ -f "$RTUNNEL_BIN_PATH" ]; then cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel '
            "&& chmod +x /tmp/rtunnel; fi"
        )

    if contents_api_filename:
        import shlex as _shlex_inner

        safe_name = _shlex_inner.quote(contents_api_filename)
        # The Jupyter Contents API uploads to the server root directory,
        # which is typically the notebook workdir — NOT necessarily $HOME.
        # Check CWD first (matches Jupyter root for fresh terminals),
        # then $HOME as fallback.
        cmd_lines.append(
            f'for _d in . "$HOME"; do '
            f'if [ ! -x /tmp/rtunnel ] && [ -f "$_d"/{safe_name} ]; then '
            f'cp "$_d"/{safe_name} /tmp/rtunnel && chmod +x /tmp/rtunnel; break; fi; done'
        )

    if sshd_deb_dir:
        cmd_lines.append(f"SSHD_DEB_DIR={shlex.quote(sshd_deb_dir)}")
    if dropbear_deb_dir:
        cmd_lines.append(f"DROPBEAR_DEB_DIR={shlex.quote(dropbear_deb_dir)}")
    apt_mirror_url = ssh_runtime.apt_mirror_url
    if apt_mirror_url:
        cmd_lines.append(f"APT_MIRROR_URL={shlex.quote(apt_mirror_url)}")

    # Skip curl fallback when rtunnel was delivered via Contents API or when
    # the notebook is known to have no internet (dropbear/apt-mirror config).
    skip_curl = bool(contents_api_filename or dropbear_deb_dir or apt_mirror_url)

    if skip_curl:
        curl_rtunnel_block = (
            'if [ ! -x "$RTUNNEL_BIN" ]; then '
            'echo "ERROR: rtunnel binary not found at /tmp/rtunnel '
            '(no curl fallback for offline notebooks)" >&2; fi'
        )
    else:
        curl_rtunnel_block = (
            'if [ ! -x "$RTUNNEL_BIN" ] && [ "$_INET" = 1 ]; then curl -fsSL '
            "--connect-timeout 10 --max-time 30 "
            '"$RTUNNEL_DOWNLOAD_URL" -o /tmp/rtunnel.tgz && '
            "tar -xzf /tmp/rtunnel.tgz -C /tmp && chmod +x /tmp/rtunnel "
            "2>/dev/null; fi"
        )

    # Quick TCP probe: set _INET=1 if archive.ubuntu.com:80 is reachable
    # within 3s, else _INET=0.  Gates apt-get and curl on no-internet
    # notebooks so they fail immediately instead of waiting for DNS timeout.
    inet_probe = (
        "_INET=0; timeout 3 bash -c "
        "'exec 3<>/dev/tcp/archive.ubuntu.com/80' 2>/dev/null && _INET=1"
    )

    openssh_bootstrap_cmd = (
        'if [ ! -f "$BOOTSTRAP_SENTINEL" ] || [ ! -x /tmp/rtunnel ] '
        "|| [ ! -x /usr/sbin/sshd ]; then "
        f"{inet_probe}; "
        "if [ ! -x /usr/sbin/sshd ]; then "
        'if [ -n "${SSHD_DEB_DIR:-}" ] && ls "$SSHD_DEB_DIR"/*.deb >/dev/null 2>&1; then '
        'dpkg -i "$SSHD_DEB_DIR"/*.deb >/dev/null 2>&1 || true; '
        'elif [ -z "${SSHD_DEB_DIR:-}" ] && [ "$_INET" = 1 ]; then '
        "export DEBIAN_FRONTEND=noninteractive; "
        "timeout 30 apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=10 "
        "update -qq && "
        "timeout 30 apt-get install -y -qq openssh-server; fi; fi; "
        "RTUNNEL_BIN=/tmp/rtunnel; "
        'if [ -n "${RTUNNEL_BIN_PATH:-}" ] && [ -x "$RTUNNEL_BIN_PATH" ]; then '
        'cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel && chmod +x /tmp/rtunnel; fi; '
        f"{curl_rtunnel_block}; "
        'if [ -x /usr/sbin/sshd ] && [ -x "$RTUNNEL_BIN" ]; then '
        'touch "$BOOTSTRAP_SENTINEL"; else rm -f "$BOOTSTRAP_SENTINEL"; fi; fi'
    )
    ensure_rtunnel_cmd = (
        "RTUNNEL_BIN=/tmp/rtunnel; "
        'if [ ! -x "$RTUNNEL_BIN" ] && [ -n "${RTUNNEL_BIN_PATH:-}" ] '
        '&& [ -x "$RTUNNEL_BIN_PATH" ]; then '
        'cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel && chmod +x /tmp/rtunnel; fi; '
        f"{curl_rtunnel_block}"
    )
    start_sshd_cmd = (
        'if [ -x /usr/sbin/sshd ] && ! ps -ef | grep -q "[s]shd -p $SSH_PORT"; then '
        "mkdir -p /run/sshd && chmod 0755 /run/sshd; "
        "ssh-keygen -A >/dev/null 2>&1 || true; "
        '/usr/sbin/sshd -p "$SSH_PORT" -o ListenAddress=127.0.0.1 -o PermitRootLogin=yes '
        "-o PasswordAuthentication=no -o PubkeyAuthentication=yes "
        ">/dev/null 2>&1 & fi"
    )
    start_dropbear_cmd = (
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
        'CODENAME=$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-}"); '
        '[ -z "$CODENAME" ] && CODENAME=$(lsb_release -cs 2>/dev/null || true); '
        '[ -z "$CODENAME" ] && CODENAME=jammy; '
        "for _f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do "
        '[ -f "$_f" ] && mv "$_f" "$_f.bak" 2>/dev/null; done; '
        'echo "deb $APT_MIRROR_URL $CODENAME main restricted universe multiverse" '
        "> /etc/apt/sources.list.d/inspire-mirror.list; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "timeout 60 apt-get update -qq >/dev/null 2>&1 && "
        "timeout 60 apt-get install -y -qq dropbear-bin >/dev/null 2>&1 || true; "
        "for _f in /etc/apt/sources.list.bak /etc/apt/sources.list.d/*.list.bak; do "
        '[ -f "$_f" ] && mv "$_f" "${_f%.bak}" 2>/dev/null; done; '
        "[ -x /usr/sbin/dropbear ] && DB_BIN=/usr/sbin/dropbear; fi; "
        'if [ -n "$DB_BIN" ] && [ -x "$DB_BIN" ] && ! ps -ef | grep -q "[d]ropbear.*-p.*$SSH_PORT"; then '
        'DB_KEY=""; '
        '[ -n "${DROPBEAR_DEB_DIR:-}" ] && [ -x "$DROPBEAR_DEB_DIR/usr/bin/dropbearkey" ] '
        '&& DB_KEY="$DROPBEAR_DEB_DIR/usr/bin/dropbearkey"; '
        '[ -z "$DB_KEY" ] && DB_KEY=$(which dropbearkey 2>/dev/null || true); '
        'if [ ! -f /tmp/dropbear_ed25519_host_key ] && [ -n "$DB_KEY" ] && [ -x "$DB_KEY" ]; then '
        '"$DB_KEY" -t ed25519 -f /tmp/dropbear_ed25519_host_key >/dev/null 2>&1; fi; '
        "if [ -f /tmp/dropbear_ed25519_host_key ]; then "
        '"$DB_BIN" -E -s -g -p "127.0.0.1:$SSH_PORT" '
        "-r /tmp/dropbear_ed25519_host_key -P /tmp/dropbear.pid "
        "2>>/tmp/dropbear.log; fi; fi; fi"
    )
    start_rtunnel_cmd = (
        "if [ -x /tmp/rtunnel ] && ! ps -ef | "
        'grep -Eq "[r]tunnel .*([[:space:]]|:)$PORT([[:space:]]|$)"; then '
        'nohup /tmp/rtunnel "$SSH_PORT" "$PORT" '
        ">/tmp/rtunnel-server.log 2>&1 & fi"
    )

    if dropbear_deb_dir or apt_mirror_url:
        setup_script = ssh_runtime.setup_script
        if setup_script:
            cmd_lines.append(f"SETUP_SCRIPT={shlex.quote(setup_script)}")
            cmd_lines.append('RTUNNEL_URL="$RTUNNEL_DOWNLOAD_URL"')
            cmd_lines.append(
                '[ -f "$SETUP_SCRIPT" ] || echo "WARN: setup script not found: $SETUP_SCRIPT '
                '(falling back to openssh bootstrap)"'
            )
            cmd_lines.append(
                'if [ -f "$SETUP_SCRIPT" ]; then '
                'if [ ! -f "$BOOTSTRAP_SENTINEL" ] || [ ! -x /tmp/rtunnel ]; then '
                'bash "$SETUP_SCRIPT" "$DROPBEAR_DEB_DIR" "$RTUNNEL_BIN_PATH" '
                '"$SSH_PORT" "$PORT" >/tmp/setup_ssh.log 2>&1; '
                'if [ $? -eq 0 ] && [ -x /tmp/rtunnel ]; then touch "$BOOTSTRAP_SENTINEL"; '
                'else rm -f "$BOOTSTRAP_SENTINEL"; fi; fi; '
                f"else {openssh_bootstrap_cmd}; fi"
            )
            cmd_lines.append("tail -40 /tmp/setup_ssh.log 2>/dev/null || true")
        else:
            cmd_lines.append('RTUNNEL_URL="$RTUNNEL_DOWNLOAD_URL"')
            cmd_lines.append(ensure_rtunnel_cmd)
        cmd_lines.append(start_dropbear_cmd)
        cmd_lines.append(start_sshd_cmd)
        cmd_lines.append(start_rtunnel_cmd)
    else:
        sshd_missing_check = f'if [ ! -x /usr/sbin/sshd ]; then echo "{SSHD_MISSING_MARKER}"; fi'
        cmd_lines.extend(
            [
                'RTUNNEL_URL="$RTUNNEL_DOWNLOAD_URL"',
                openssh_bootstrap_cmd,
                sshd_missing_check,
                start_sshd_cmd,
                start_rtunnel_cmd,
            ]
        )

    cmd_lines.append(
        'if ps -ef | grep -Eq "[r]tunnel .*([[:space:]]|:)$PORT([[:space:]]|$)"; then '
        'echo "INSPIRE_RTUNNEL_STATUS=running"; '
        'else echo "INSPIRE_RTUNNEL_STATUS=not_running"; fi'
    )
    cmd_lines.append(f"echo {SETUP_DONE_MARKER}")

    return cmd_lines
