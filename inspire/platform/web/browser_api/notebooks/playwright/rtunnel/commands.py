"""Shell command construction for notebook rtunnel setup."""

from __future__ import annotations

from typing import Optional

from inspire.config.ssh_runtime import (
    DEFAULT_RTUNNEL_DOWNLOAD_URL,
    SshRuntimeConfig,
    resolve_ssh_runtime_config,
)


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
    ]

    if rtunnel_bin:
        cmd_lines.append(f"RTUNNEL_BIN_PATH={shlex.quote(rtunnel_bin)}")
        cmd_lines.append(
            'if [ -f "$RTUNNEL_BIN_PATH" ]; then cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel '
            "&& chmod +x /tmp/rtunnel; fi"
        )

    if sshd_deb_dir:
        cmd_lines.append(f"SSHD_DEB_DIR={shlex.quote(sshd_deb_dir)}")
    if dropbear_deb_dir:
        cmd_lines.append(f"DROPBEAR_DEB_DIR={shlex.quote(dropbear_deb_dir)}")

    if dropbear_deb_dir:
        setup_script = ssh_runtime.setup_script
        if not setup_script:
            raise ValueError(
                "ssh.setup_script (or INSPIRE_SETUP_SCRIPT) is required when using "
                "ssh.dropbear_deb_dir."
            )
        rtunnel_bin_arg = rtunnel_bin or ""
        cmd_lines.append(
            f"bash {shlex.quote(setup_script)} {shlex.quote(dropbear_deb_dir)} "
            f'{shlex.quote(rtunnel_bin_arg)} "$SSH_PORT" "$PORT" '
            ">/tmp/setup_ssh.log 2>&1; tail -80 /tmp/setup_ssh.log; echo "
            "'>>> dropbear log'; tail -60 /tmp/dropbear.log 2>/dev/null || true; echo "
            "'>>> rtunnel log'; tail -60 /tmp/rtunnel-server.log 2>/dev/null || true"
        )
    else:
        cmd_lines.extend(
            [
                f"RTUNNEL_URL={rtunnel_download_url!r}",
                (
                    'if [ ! -x /usr/sbin/sshd ] && [ -z "${SSHD_DEB_DIR:-}" ]; then '
                    "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && "
                    "apt-get install -y -qq openssh-server; fi"
                ),
                "pkill -f 'sshd -p' 2>/dev/null || true",
                (
                    "if [ -x /usr/sbin/sshd ]; then mkdir -p /run/sshd && chmod 0755 "
                    "/run/sshd; ssh-keygen -A >/dev/null 2>&1 || true; /usr/sbin/sshd "
                    '-p "$SSH_PORT" -o ListenAddress=127.0.0.1 -o PermitRootLogin=yes '
                    "-o PasswordAuthentication=no -o PubkeyAuthentication=yes "
                    ">/dev/null 2>&1 & fi"
                ),
                "RTUNNEL_BIN=/tmp/rtunnel",
                (
                    'if [ -n "${RTUNNEL_BIN_PATH:-}" ] && [ -x "$RTUNNEL_BIN_PATH" ]; then '
                    'cp "$RTUNNEL_BIN_PATH" /tmp/rtunnel && chmod +x /tmp/rtunnel; fi'
                ),
                'pkill -f "rtunnel.*:$PORT" 2>/dev/null || true',
                (
                    f'if [ ! -x "$RTUNNEL_BIN" ]; then curl -fsSL '
                    f"'{rtunnel_download_url}' -o /tmp/rtunnel.tgz && tar -xzf "
                    "/tmp/rtunnel.tgz -C /tmp && chmod +x /tmp/rtunnel 2>/dev/null; fi"
                ),
                (
                    'nohup "$RTUNNEL_BIN" "127.0.0.1:$SSH_PORT" "0.0.0.0:$PORT" '
                    ">/tmp/rtunnel-server.log 2>&1 &"
                ),
            ]
        )

    return cmd_lines
