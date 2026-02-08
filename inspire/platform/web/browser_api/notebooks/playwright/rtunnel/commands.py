"""Shell command construction for notebook rtunnel setup."""

from __future__ import annotations

from typing import Optional

from inspire.config.ssh_runtime import (
    DEFAULT_RTUNNEL_DOWNLOAD_URL,
    SshRuntimeConfig,
    resolve_ssh_runtime_config,
)

BOOTSTRAP_SENTINEL = "/tmp/.inspire_rtunnel_bootstrap_v1"


def build_rtunnel_setup_commands(
    *,
    port: int,
    ssh_port: int,
    ssh_public_key: Optional[str],
    ssh_runtime: Optional[SshRuntimeConfig] = None,
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

    # Always set RTUNNEL_BIN_PATH (empty string if not configured)
    cmd_lines.append(f"RTUNNEL_BIN_PATH={shlex.quote(rtunnel_bin or '')}")
    if rtunnel_bin:
        cmd_lines.append(
            'if [ -f "$RTUNNEL_BIN_PATH" ]; then cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel '
            "&& chmod +x /tmp/rtunnel; fi"
        )

    if sshd_deb_dir:
        cmd_lines.append(f"SSHD_DEB_DIR={shlex.quote(sshd_deb_dir)}")
    if dropbear_deb_dir:
        cmd_lines.append(f"DROPBEAR_DEB_DIR={shlex.quote(dropbear_deb_dir)}")

    openssh_bootstrap_cmd = (
        'if [ ! -f "$BOOTSTRAP_SENTINEL" ] || [ ! -x /tmp/rtunnel ] '
        '|| [ ! -x /usr/sbin/sshd ]; then '
        'if [ ! -x /usr/sbin/sshd ] && [ -z "${SSHD_DEB_DIR:-}" ]; then '
        "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && "
        "apt-get install -y -qq openssh-server; fi; "
        "RTUNNEL_BIN=/tmp/rtunnel; "
        'if [ -n "${RTUNNEL_BIN_PATH:-}" ] && [ -x "$RTUNNEL_BIN_PATH" ]; then '
        'cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel && chmod +x /tmp/rtunnel; fi; '
        'if [ ! -x "$RTUNNEL_BIN" ]; then curl -fsSL '
        f"'{rtunnel_download_url}' -o /tmp/rtunnel.tgz && "
        'tar -xzf /tmp/rtunnel.tgz -C /tmp && chmod +x /tmp/rtunnel '
        '2>/dev/null; fi; '
        'if [ -x /usr/sbin/sshd ] && [ -x "$RTUNNEL_BIN" ]; then '
        'touch "$BOOTSTRAP_SENTINEL"; else rm -f "$BOOTSTRAP_SENTINEL"; fi; fi'
    )
    start_sshd_cmd = (
        'if [ -x /usr/sbin/sshd ] && ! ps -ef | grep -q "[s]shd -p $SSH_PORT"; then '
        "mkdir -p /run/sshd && chmod 0755 /run/sshd; "
        "ssh-keygen -A >/dev/null 2>&1 || true; "
        '/usr/sbin/sshd -p "$SSH_PORT" -o ListenAddress=127.0.0.1 -o PermitRootLogin=yes '
        "-o PasswordAuthentication=no -o PubkeyAuthentication=yes "
        ">/dev/null 2>&1 & fi"
    )
    start_rtunnel_cmd = (
        'if [ -x /tmp/rtunnel ] && ! ps -ef | grep -q "[r]tunnel .*:$PORT"; then '
        'nohup /tmp/rtunnel "127.0.0.1:$SSH_PORT" "0.0.0.0:$PORT" '
        '>/tmp/rtunnel-server.log 2>&1 & fi'
    )

    if dropbear_deb_dir:
        setup_script = ssh_runtime.setup_script
        if not setup_script:
            raise ValueError(
                "ssh.setup_script (or INSPIRE_SETUP_SCRIPT) is required when using "
                "ssh.dropbear_deb_dir."
            )
        cmd_lines.append(f"SETUP_SCRIPT={shlex.quote(setup_script)}")
        cmd_lines.append(f"RTUNNEL_URL={rtunnel_download_url!r}")
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
        cmd_lines.append(start_sshd_cmd)
        cmd_lines.append(start_rtunnel_cmd)
    else:
        cmd_lines.extend(
            [
                f"RTUNNEL_URL={rtunnel_download_url!r}",
                openssh_bootstrap_cmd,
                start_sshd_cmd,
                start_rtunnel_cmd,
            ]
        )

    return cmd_lines
