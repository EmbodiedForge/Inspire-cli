"""Shell commands for bootstrapping rtunnel + SSH access on a notebook."""

from __future__ import annotations

from dataclasses import dataclass
import posixpath
import shlex
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
SSHD_SENTINEL = "${HOME:-/root}/.inspire/ssh_state/.sshd_ready"
RTUNNEL_SENTINEL = "${HOME:-/root}/.inspire/ssh_state/.rtunnel_ready"
SETUP_DONE_MARKER = "INSPIRE_RTUNNEL_SETUP_DONE"
STEP_FAILED_PREFIX = "INSPIRE_STEP_FAILED="
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
            'elif [ -f "$RTUNNEL_BIN_PATH" ]; then rm -f "$RTUNNEL_BIN" && '
            'cp "$RTUNNEL_BIN_PATH" "$RTUNNEL_BIN" '
            '&& chmod +x "$RTUNNEL_BIN"; fi'
        )

    if contents_api_filename:
        safe_name = shlex.quote(contents_api_filename)
        lines.append(
            f'for _d in . "$HOME"; do '
            f'if [ ! -x "$RTUNNEL_BIN" ] && [ -f "$_d"/{safe_name} ]; then '
            f'rm -f "$RTUNNEL_BIN" && '
            f'cp "$_d"/{safe_name} "$RTUNNEL_BIN" && chmod +x "$RTUNNEL_BIN" '
            "&& break; fi; done"
        )
    return lines


def _step_block(step_id: str, body_lines: list[str]) -> list[str]:
    """Wrap bash lines with start/success markers."""
    return [
        f'__INSPIRE_STEP="{step_id}"',
        f'__inspire_mark START "{step_id}"',
        *body_lines,
        f'__inspire_mark OK "{step_id}"',
        '__INSPIRE_STEP="setup"',
    ]


def _step_block_checked(step_id: str, body_lines: list[str], check_cmd: str) -> list[str]:
    """Wrap bash lines with start/success markers and verify the step succeeded.

    After the body executes, *check_cmd* is run.  If it exits non-zero the step
    is reported as FAILED with the captured stderr.
    """
    return [
        f'__INSPIRE_STEP="{step_id}"',
        f'__inspire_mark START "{step_id}"',
        *body_lines,
        "set +e",
        f"_step_out=$({check_cmd} 2>&1)",
        "_step_rc=$?",
        "set -e",
        "if [ $_step_rc -ne 0 ]; then",
        f'  __inspire_fail $_step_rc $LINENO {shlex.quote(check_cmd)} "$_step_out"',
        "  exit $_step_rc",
        "else",
        f'  __inspire_mark OK "{step_id}"',
        "fi",
        '__INSPIRE_STEP="setup"',
    ]


def _step_cmd(step_id: str, cmd: str) -> list[str]:
    """Wrap a single command with error capture."""
    return [
        f'__INSPIRE_STEP="{step_id}"',
        f'__inspire_mark START "{step_id}"',
        "set +e",
        f"_step_out=$({cmd} 2>&1)",
        "_step_rc=$?",
        "set -e",
        "if [ $_step_rc -ne 0 ]; then",
        f'  __inspire_fail $_step_rc $LINENO {shlex.quote(cmd)} "$_step_out"',
        "  exit $_step_rc",
        "else",
        f'  __inspire_mark OK "{step_id}"',
        "fi",
        '__INSPIRE_STEP="setup"',
    ]


def _build_probe_inet_step() -> list[str]:
    return _step_block(
        "probe_inet",
        [
            "_INET=0",
            "timeout 3 \"$__INSPIRE_BASH\" -c 'exec 3<>/dev/tcp/archive.ubuntu.com/80' 2>/dev/null && _INET=1 || true",
        ],
    )


def _build_bootstrap_openssh_step(*, sshd_deb_dir: Optional[str]) -> list[str]:
    body = (
        'if { [ ! -f "$SSHD_SENTINEL" ] && [ ! -f "$BOOTSTRAP_SENTINEL" ]; } || [ ! -x /usr/sbin/sshd ]; then '
        "if [ ! -x /usr/sbin/sshd ]; then "
    )
    if sshd_deb_dir:
        body += (
            'if [ -n "${SSHD_DEB_DIR:-}" ] && ls "$SSHD_DEB_DIR"/*.deb >/dev/null 2>&1; then '
            'dpkg -i "$SSHD_DEB_DIR"/*.deb >/dev/null 2>&1 || true; '
            'elif [ -z "${SSHD_DEB_DIR:-}" ] && [ "$_INET" = 1 ]; then '
        )
    else:
        body += 'if [ "$_INET" = 1 ]; then '
    body += (
        "export DEBIAN_FRONTEND=noninteractive; "
        "timeout 30 apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=10 update -qq && "
        "timeout 30 apt-get install -y -qq openssh-server; fi; fi; "
        'if [ -x /usr/sbin/sshd ]; then mkdir -p "${SSHD_SENTINEL%/*}" && touch "$SSHD_SENTINEL"; fi; fi'
    )
    return _step_block_checked("bootstrap_openssh", [body], "test -x /usr/sbin/sshd")


def _build_ensure_rtunnel_step(*, skip_curl: bool) -> list[str]:
    body = (
        'if { [ ! -f "$RTUNNEL_SENTINEL" ] && [ ! -f "$BOOTSTRAP_SENTINEL" ]; } || [ ! -x "$RTUNNEL_BIN" ]; then '
        'if [ ! -x "$RTUNNEL_BIN" ] && [ -n "${RTUNNEL_BIN_PATH:-}" ] && [ -x "$RTUNNEL_BIN_PATH" ]; then RTUNNEL_BIN="$RTUNNEL_BIN_PATH"; fi; '
    )
    if skip_curl:
        body += (
            'if [ ! -x "$RTUNNEL_BIN" ]; then '
            'echo "ERROR: rtunnel binary not found at $RTUNNEL_BIN '
            '(no curl fallback for offline notebooks)" >&2; fi; '
        )
    else:
        body += (
            'if [ ! -x "$RTUNNEL_BIN" ] && [ "$_INET" = 1 ]; then curl -fsSL '
            "--connect-timeout 10 --max-time 30 "
            '"$RTUNNEL_DOWNLOAD_URL" -o "$RTUNNEL_BIN.tgz" && '
            'tar -xzf "$RTUNNEL_BIN.tgz" -C /tmp && chmod +x "$RTUNNEL_BIN" '
            "2>/dev/null; fi; "
        )
    body += 'if [ -x "$RTUNNEL_BIN" ]; then mkdir -p "${RTUNNEL_SENTINEL%/*}" && touch "$RTUNNEL_SENTINEL"; fi; fi'
    return _step_block_checked("ensure_rtunnel_bin", [body], 'test -x "$RTUNNEL_BIN"')


def _build_start_sshd_step() -> list[str]:
    ssh_listener_check = _build_ssh_listener_check()
    body = (
        f"if [ -x /usr/sbin/sshd ] && ! {ssh_listener_check}; then "
        "mkdir -p /run/sshd && chmod 0755 /run/sshd; "
        "ssh-keygen -A >/dev/null 2>&1 || true; "
        '/usr/sbin/sshd -p "$SSH_PORT" -o ListenAddress=127.0.0.1 -o PermitRootLogin=yes '
        "-o PasswordAuthentication=no -o PubkeyAuthentication=yes "
        ">/dev/null 2>&1 & fi"
    )
    return _step_block_checked("start_sshd", [body], ssh_listener_check)


def _build_start_dropbear_step(dropbear_host_key: str) -> list[str]:
    ssh_listener_check = _build_ssh_listener_check()
    body = (
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
        "if [ -x /usr/sbin/dropbear ] && /usr/sbin/dropbear -V >/dev/null 2>&1; then "
        "DB_BIN=/usr/sbin/dropbear; fi; fi; "
        'if [ -z "$DB_BIN" ] || [ ! -x "$DB_BIN" ]; then '
        "if [ -x /usr/sbin/dropbear ] && /usr/sbin/dropbear -V >/dev/null 2>&1; then "
        "DB_BIN=/usr/sbin/dropbear; fi; fi; "
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
        "dpkg --remove --force-remove-reinstreq dropbear-bin >/dev/null 2>&1 || true; "
        "timeout 60 apt-get install -y -qq dropbear-bin >/dev/null 2>&1 || true; "
        "for _f in /etc/apt/sources.list.bak /etc/apt/sources.list.d/*.list.bak "
        "/etc/apt/sources.list.d/*.sources.bak; do "
        '[ -f "$_f" ] && mv "$_f" "${_f%.bak}" 2>/dev/null; done; '
        "if [ -x /usr/sbin/dropbear ] && /usr/sbin/dropbear -V >/dev/null 2>&1; then "
        "DB_BIN=/usr/sbin/dropbear; fi; fi; "
        'if [ -n "$DB_BIN" ] && [ -x "$DB_BIN" ]; then '
        'DB_KEY=""; '
        '[ -n "${DROPBEAR_DEB_DIR:-}" ] && [ -x "$DROPBEAR_DEB_DIR/usr/bin/dropbearkey" ] '
        '&& BUNDLE_DB_KEY="$DROPBEAR_DEB_DIR/usr/bin/dropbearkey" || BUNDLE_DB_KEY=""; '
        "DB_KEY=$(command -v dropbearkey 2>/dev/null || true); "
        '[ -z "$DB_KEY" ] && DB_KEY="$BUNDLE_DB_KEY"; '
        f'mkdir -p "$(dirname "{dropbear_host_key}")" 2>/dev/null || true; '
        f'if [ ! -s "{dropbear_host_key}" ] && [ -n "$DB_KEY" ] && [ -x "$DB_KEY" ]; then '
        f'"$DB_KEY" -t ed25519 -f "{dropbear_host_key}" >/dev/null 2>&1 || true; fi; '
        f'if [ -s "{dropbear_host_key}" ]; then '
        '__inspire_start_daemon /tmp/dropbear.log "$DB_BIN" -E -s -g -p "127.0.0.1:$SSH_PORT" '
        f'-r "{dropbear_host_key}" -P /tmp/dropbear.pid; '
        "fi; fi; fi; fi"
    )
    return _step_block_checked("start_dropbear", [body], ssh_listener_check)


def _build_start_rtunnel_step() -> list[str]:
    rtunnel_process_check = _build_rtunnel_process_check()
    body = (
        f'if [ -x "$RTUNNEL_BIN" ] && ! {rtunnel_process_check}; then '
        '__inspire_start_daemon /tmp/rtunnel-server.log "$RTUNNEL_BIN" "$SSH_PORT" "$PORT"; '
        "fi"
    )
    return _step_block_checked(
        "start_rtunnel",
        [body],
        rtunnel_process_check,
    )


def _build_setup_script_step() -> list[str]:
    return [
        '__INSPIRE_STEP="run_setup_script"',
        '__inspire_mark START "run_setup_script"',
        'if [ -f "$SETUP_SCRIPT" ]; then',
        'if { [ ! -f "$SSHD_SENTINEL" ] && [ ! -f "$RTUNNEL_SENTINEL" ] && [ ! -f "$BOOTSTRAP_SENTINEL" ]; } || [ ! -x "$RTUNNEL_BIN" ]; then',
        "set +e",
        '"$__INSPIRE_BASH" "$SETUP_SCRIPT" "$DROPBEAR_DEB_DIR" "$RTUNNEL_BIN_PATH" "$SSH_PORT" "$PORT" >/tmp/setup_ssh.log 2>&1;',
        "_step_rc=$?",
        "set -e",
        'if [ $_step_rc -eq 0 ] && [ -x "$RTUNNEL_BIN" ]; then',
        'mkdir -p "${SSHD_SENTINEL%/*}" "${RTUNNEL_SENTINEL%/*}" && touch "$SSHD_SENTINEL" "$RTUNNEL_SENTINEL" "$BOOTSTRAP_SENTINEL";',
        '  __inspire_mark OK "run_setup_script"',
        "else",
        'rm -f "$SSHD_SENTINEL" "$RTUNNEL_SENTINEL" "$BOOTSTRAP_SENTINEL";',
        "  _step_out=$(tail -80 /tmp/setup_ssh.log 2>/dev/null || true)",
        '  [ -n "$_step_out" ] || _step_out="setup script exited with code $_step_rc"',
        "  __inspire_fail $_step_rc $LINENO "
        '\'"$__INSPIRE_BASH" "$SETUP_SCRIPT" "$DROPBEAR_DEB_DIR" "$RTUNNEL_BIN_PATH" "$SSH_PORT" "$PORT"\' '
        '"$_step_out"',
        "  exit $_step_rc",
        "fi; fi;",
        "else true; fi",
        '__INSPIRE_STEP="setup"',
    ]


def _build_ssh_listener_check() -> str:
    return (
        '{ ss -ltnp 2>/dev/null | grep -Eq "127\\\\.0\\\\.0\\\\.1:${SSH_PORT}[[:space:]]|'
        '\\[::1\\]:${SSH_PORT}[[:space:]]|[[:space:]]:${SSH_PORT}[[:space:]]"'
        ' || ps -efww | grep -Eq "[d]ropbear.*-p.*${SSH_PORT}([[:space:]]|$)|'
        "[s]shd: .*-p ${SSH_PORT}([[:space:]]|$)|"
        '[s]shd -p ${SSH_PORT}([[:space:]]|$)"; }'
    )


def _build_verify_ssh_listener_step(*, include_sshd_marker: bool) -> list[str]:
    status_check = _build_ssh_listener_check()
    body = ""
    if include_sshd_marker:
        body += f'if [ ! -x /usr/sbin/sshd ]; then echo "{SSHD_MISSING_MARKER}"; fi; '
    body += f'if {status_check}; then true; else echo "{SSH_SERVER_MISSING_MARKER}"; fi'
    return _step_block("verify_ssh_listener", [body])


def _build_rtunnel_process_check() -> str:
    return 'ps -efww | grep -Eq "[r]tunnel[^[:space:]]*[[:space:]].*([[:space:]]|:)$PORT([[:space:]]|$)"'


def _build_verify_rtunnel_process_step() -> list[str]:
    rtunnel_process_check = _build_rtunnel_process_check()
    body = (
        f"if {rtunnel_process_check}; then "
        'echo "INSPIRE_RTUNNEL_STATUS=running"; '
        "else "
        'echo "INSPIRE_RTUNNEL_STATUS=not_running"; fi'
    )
    return _step_block("verify_rtunnel_process", [body])


def build_rtunnel_setup_commands(
    *,
    port: int,
    ssh_port: int,
    ssh_public_key: Optional[str],
    ssh_runtime: Optional[SshRuntimeConfig] = None,
    contents_api_filename: Optional[str] = None,
) -> list[str]:
    if ssh_runtime is None:
        ssh_runtime = resolve_ssh_runtime_config()

    plan = resolve_rtunnel_setup_plan(
        ssh_runtime=ssh_runtime,
        contents_api_filename=contents_api_filename,
    )
    rtunnel_download_url = ssh_runtime.rtunnel_download_url or DEFAULT_RTUNNEL_DOWNLOAD_URL

    host_key_dir = str(getattr(ssh_runtime, "host_key_dir", "") or "").strip()
    dropbear_host_key = (
        posixpath.join(host_key_dir, "dropbear_ed25519_host_key")
        if host_key_dir
        else "/tmp/dropbear_ed25519_host_key"
    )

    cmd_lines = [
        "__INSPIRE_STEP=setup",
        "__INSPIRE_TRACE=/tmp/inspire-rtunnel-setup-trace.log",
        ': > "$__INSPIRE_TRACE" 2>/dev/null || true',
        'exec 9>>"$__INSPIRE_TRACE" 2>/dev/null || true',
        "if { : >&9; } 2>/dev/null; then BASH_XTRACEFD=9; fi",
        '__INSPIRE_BASH="$(if [ -x /bin/bash ]; then printf %s /bin/bash; else command -v bash 2>/dev/null || printf %s bash; fi)"',
        '__INSPIRE_BASE64="$(if [ -x /usr/bin/base64 ]; then printf %s /usr/bin/base64; else command -v base64 2>/dev/null || printf %s base64; fi)"',
        'export PATH="${PATH:-}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"',
        "PS4='+ inspire:${LINENO}:${__INSPIRE_STEP:-setup}: '",
        '__inspire_mark() { echo "INSPIRE_STEP_$1=$2"; }',
        (
            "__inspire_start_daemon() { "
            'local _log="$1"; shift; '
            "if command -v setsid >/dev/null 2>&1; then "
            'setsid nohup "$@" </dev/null >>"$_log" 2>&1 & disown; '
            'else nohup "$@" </dev/null >>"$_log" 2>&1 & disown; fi; }'
        ),
        '__inspire_b64_err() { printf "%s" "$1" | head -c 10000 | "$__INSPIRE_BASE64" -w0; }',
        '__inspire_trace_b64() { tail -80 "$__INSPIRE_TRACE" 2>/dev/null | head -c 20000 | "$__INSPIRE_BASE64" -w0; }',
        (
            "__inspire_fail() { "
            'local _rc="$1" _line="$2" _cmd="${3:-}" _err="${4:-}" '
            "_cmd_b64 _err_b64 _trace_b64; "
            "set +x; "
            '_cmd_b64=$(__inspire_b64_err "$_cmd"); '
            '_err_b64=$(__inspire_b64_err "$_err"); '
            "_trace_b64=$(__inspire_trace_b64); "
            'echo "INSPIRE_STEP_FAILED=${__INSPIRE_STEP:-setup}|line=$_line|rc=$_rc|cmd=$_cmd_b64|err=$_err_b64|trace=$_trace_b64"; '
            "}"
        ),
        (
            "__inspire_err_trap() { "
            'local _rc="$1" _line="$2" _cmd="$3"; '
            'trap - ERR; __inspire_fail "$_rc" "$_line" "$_cmd" ""; '
            'exit "$_rc"; '
            "}"
        ),
        'trap \'__inspire_err_trap "$?" "$LINENO" "$BASH_COMMAND"\' ERR',
        "set -Eeuo pipefail",
        "set -x",
        "",
        f"PORT={port}",
        f"SSH_PORT={ssh_port}",
        _build_key_setup_line(ssh_public_key),
        f"BOOTSTRAP_SENTINEL={BOOTSTRAP_SENTINEL}",
        f'SSHD_SENTINEL="{SSHD_SENTINEL}"',
        f'RTUNNEL_SENTINEL="{RTUNNEL_SENTINEL}"',
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

    cmd_lines.append('RTUNNEL_URL="$RTUNNEL_DOWNLOAD_URL"')
    cmd_lines.extend(_build_probe_inet_step())

    if plan.bootstrap_mode == "dropbear":
        setup_script = ssh_runtime.setup_script
        if setup_script:
            cmd_lines.append(f"SETUP_SCRIPT={shlex.quote(setup_script)}")
            cmd_lines.append(
                '[ -f "$SETUP_SCRIPT" ] || echo "WARN: setup script not found: $SETUP_SCRIPT '
                '(falling back to dropbear bootstrap)"'
            )
            cmd_lines.extend(_build_setup_script_step())
            cmd_lines.append("tail -40 /tmp/setup_ssh.log 2>/dev/null || true")
        else:
            cmd_lines.extend(_build_ensure_rtunnel_step(skip_curl=plan.skip_curl))
        cmd_lines.extend(_build_start_dropbear_step(dropbear_host_key))
        cmd_lines.extend(_build_start_rtunnel_step())
        cmd_lines.extend(_build_verify_ssh_listener_step(include_sshd_marker=False))
    else:
        cmd_lines.extend(_build_bootstrap_openssh_step(sshd_deb_dir=ssh_runtime.sshd_deb_dir))
        cmd_lines.extend(_build_ensure_rtunnel_step(skip_curl=plan.skip_curl))
        cmd_lines.extend(_build_start_sshd_step())
        cmd_lines.extend(_build_start_rtunnel_step())
        cmd_lines.extend(_build_verify_ssh_listener_step(include_sshd_marker=True))

    cmd_lines.extend(_build_verify_rtunnel_process_step())
    cmd_lines.append(f"echo {SETUP_DONE_MARKER}")

    return cmd_lines


__all__ = [
    "BOOTSTRAP_SENTINEL",
    "SSHD_SENTINEL",
    "RTUNNEL_SENTINEL",
    "SETUP_DONE_MARKER",
    "STEP_FAILED_PREFIX",
    "SSHD_MISSING_MARKER",
    "SSH_SERVER_MISSING_MARKER",
    "RtunnelSetupPlan",
    "build_rtunnel_setup_commands",
    "describe_rtunnel_setup_plan",
    "resolve_rtunnel_setup_plan",
]
