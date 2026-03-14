"""Tests for notebook rtunnel shell command construction."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from inspire.config.ssh_runtime import SshRuntimeConfig
from inspire.platform.web.browser_api.rtunnel import (
    BOOTSTRAP_SENTINEL,
    SSH_SERVER_MISSING_MARKER,
    SSHD_MISSING_MARKER,
    build_rtunnel_setup_commands,
    resolve_rtunnel_setup_plan,
)


def _render_setup_script(
    *,
    runtime: SshRuntimeConfig,
    contents_api_filename: str | None = None,
) -> str:
    commands = build_rtunnel_setup_commands(
        port=39017,
        ssh_port=22222,
        ssh_public_key="ssh-ed25519 AAAA... test@example",
        ssh_runtime=runtime,
        contents_api_filename=contents_api_filename,
    )
    return "\n".join(commands) + "\n"


def _assert_valid_bash_syntax(script: str) -> None:
    result = subprocess.run(
        ["bash", "-n"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def _all_bootstrap_shell_variants() -> list[tuple[str, SshRuntimeConfig, str | None]]:
    return [
        ("openssh_legacy_apt", SshRuntimeConfig(), None),
        (
            "openssh_legacy_debs",
            SshRuntimeConfig(
                sshd_deb_dir="/shared/sshd-debs",
                rtunnel_bin="/shared/bin/rtunnel",
            ),
            None,
        ),
        (
            "dropbear_mirror",
            SshRuntimeConfig(
                apt_mirror_url="http://nexus.example/repository/",
                rtunnel_bin="/shared/bin/rtunnel",
            ),
            None,
        ),
        (
            "dropbear_bundle",
            SshRuntimeConfig(
                dropbear_deb_dir="/shared/dropbear",
                rtunnel_bin="/shared/bin/rtunnel",
            ),
            None,
        ),
        (
            "dropbear_setup_script",
            SshRuntimeConfig(
                dropbear_deb_dir="/shared/dropbear",
                setup_script="/shared/setup_ssh.sh",
                rtunnel_bin="/shared/bin/rtunnel",
            ),
            None,
        ),
        (
            "openssh_contents_api",
            SshRuntimeConfig(),
            ".inspire_rtunnel_bin",
        ),
    ]


def test_build_commands_uses_explicit_runtime_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSPIRE_RTUNNEL_BIN", "/env/rtunnel")
    monkeypatch.setenv("INSPIRE_SSHD_DEB_DIR", "/env/sshd")

    runtime = SshRuntimeConfig(
        rtunnel_bin="/project/rtunnel",
        sshd_deb_dir="/project/sshd",
        rtunnel_download_url="https://project.example/rtunnel.tgz",
    )
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    assert "RTUNNEL_BIN_PATH=/project/rtunnel" in joined
    assert 'RTUNNEL_BIN="/tmp/rtunnel-$PORT"' in joined
    assert "SSHD_DEB_DIR=/project/sshd" in joined
    assert "/env/rtunnel" not in joined
    assert "/env/sshd" not in joined
    # With sshd_deb_dir set, dpkg -i should be used for .deb installation
    assert "dpkg -i" in joined
    # Shell snippet sets RTUNNEL_DOWNLOAD_URL dynamically
    assert "RTUNNEL_DOWNLOAD_URL=" in joined
    # RTUNNEL_URL compat alias references RTUNNEL_DOWNLOAD_URL
    assert 'RTUNNEL_URL="$RTUNNEL_DOWNLOAD_URL"' in joined

    plan = resolve_rtunnel_setup_plan(ssh_runtime=runtime)
    assert plan.bootstrap_strategy == "openssh_legacy_debs"
    assert plan.legacy_bootstrap is True


def test_dropbear_without_setup_script_uses_dpkg() -> None:
    """When dropbear_deb_dir is set but setup_script is not, the internal
    dpkg-based installation should be used instead of raising ValueError."""
    runtime = SshRuntimeConfig(
        dropbear_deb_dir="/project/dropbear",
        setup_script=None,
    )

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    # Should contain the DROPBEAR_DEB_DIR variable
    assert "DROPBEAR_DEB_DIR=" in joined
    # Should contain dpkg -i fallback for raw .deb packages
    assert "dpkg -i" in joined
    # Should NOT curl rtunnel binary (offline notebook with dropbear config)
    assert "curl -fsSL" not in joined
    # Should emit error message when rtunnel binary not found
    assert "no curl fallback for offline notebooks" in joined
    # Should NOT contain SETUP_SCRIPT (no external script)
    assert "SETUP_SCRIPT=" not in joined


def test_dropbear_apt_mirror_fallback() -> None:
    """When apt_mirror_url is set, the bootstrap should fall back to
    apt-get install dropbear-bin if dpkg fails."""
    runtime = SshRuntimeConfig(
        dropbear_deb_dir="/project/dropbear",
        apt_mirror_url="http://nexus.example/repository/ubuntu/",
    )

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    assert "APT_MIRROR_URL=" in joined
    assert "apt-get install -y -qq dropbear-bin" in joined
    assert "inspire-mirror.list" in joined
    # dpkg path should still be tried first
    assert "dpkg -i" in joined
    # Codename detection via /etc/os-release (primary) then lsb_release (fallback)
    assert "DISTRO_ID=$(. /etc/os-release" in joined
    assert "/etc/os-release" in joined
    assert "VERSION_CODENAME" in joined
    assert "lsb_release" in joined
    assert 'MIRROR_COMPONENTS="main restricted universe multiverse"' in joined
    assert 'MIRROR_COMPONENTS="main"' in joined
    # Existing sources moved aside to avoid timeout on unreachable mirrors
    assert "sources.list.bak" in joined
    assert ".sources.bak" in joined
    assert "force-remove-reinstreq openssh-server" in joined
    assert 'MIRROR_COMPONENTS="main"' in joined
    # Dropbear launch should be guarded by host key existence
    assert "[ -f /tmp/dropbear_ed25519_host_key ]" in joined


def flowless_ssh_listener_check() -> str:
    return (
        '{ ss -ltnp 2>/dev/null | grep -Eq "127\\\\.0\\\\.0\\\\.1:${SSH_PORT}[[:space:]]|'
        '\\[::1\\]:${SSH_PORT}[[:space:]]|[[:space:]]:${SSH_PORT}[[:space:]]"'
        ' || ps -efww | grep -Eq "[d]ropbear.*-p.*${SSH_PORT}([[:space:]]|$)|'
        "[s]shd: .*-p ${SSH_PORT}([[:space:]]|$)|"
        '[s]shd -p ${SSH_PORT}([[:space:]]|$)"; }'
    )


def test_dropbear_path_skips_install_work_when_ssh_listener_already_exists() -> None:
    runtime = SshRuntimeConfig(
        apt_mirror_url="http://nexus.example/repository/ubuntu/",
    )

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)
    listener_check = flowless_ssh_listener_check()

    assert (
        f'if ! {listener_check}; then if [ -n "${{DROPBEAR_DEB_DIR:-}}" ] || [ -n "${{APT_MIRROR_URL:-}}" ]; then '
        in joined
    )
    assert 'if [ -n "$DB_BIN" ] && [ -x "$DB_BIN" ]; then ' in joined


def test_apt_mirror_only_without_dropbear_deb_dir() -> None:
    """When only apt_mirror_url is set (no dropbear_deb_dir), the dropbear
    path should still be entered and apt install should run."""
    runtime = SshRuntimeConfig(
        apt_mirror_url="http://nexus.example/repository/ubuntu/",
    )

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    assert "APT_MIRROR_URL=" in joined
    assert "apt-get install -y -qq dropbear-bin" in joined
    # Should use dropbear path (not openssh)
    assert "dropbear" in joined
    # Should NOT have DROPBEAR_DEB_DIR set
    assert "DROPBEAR_DEB_DIR=" not in joined
    assert SSH_SERVER_MISSING_MARKER in joined


def test_apt_mirror_repository_root_is_normalized_on_notebook() -> None:
    runtime = SshRuntimeConfig(
        apt_mirror_url="http://nexus.example/repository/",
    )

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    assert "APT_MIRROR_URL=http://nexus.example/repository/" in joined
    assert 'MIRROR_DISTRO="${DISTRO_ID:-ubuntu}"' in joined
    assert '[ "$MIRROR_DISTRO" = "debian" ] || MIRROR_DISTRO="ubuntu"' in joined
    assert 'MIRROR_URL="${APT_MIRROR_URL%/}"' in joined
    assert '*/repository) MIRROR_URL="$MIRROR_URL/$MIRROR_DISTRO" ;;' in joined
    assert 'echo "deb $MIRROR_URL $CODENAME $MIRROR_COMPONENTS" ' in joined


@pytest.mark.parametrize(
    ("strategy_name", "runtime", "contents_api_filename"),
    _all_bootstrap_shell_variants(),
)
def test_rendered_setup_script_is_valid_bash_syntax(
    strategy_name: str,
    runtime: SshRuntimeConfig,
    contents_api_filename: str | None,
) -> None:
    script = _render_setup_script(
        runtime=runtime,
        contents_api_filename=contents_api_filename,
    )

    _assert_valid_bash_syntax(script)
    assert "INSPIRE_RTUNNEL_SETUP_DONE" in script, strategy_name
    assert "INSPIRE_RTUNNEL_STATUS=" in script, strategy_name


def test_rendered_setup_scripts_pass_shellcheck_when_available(tmp_path: Path) -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed")

    script_paths: list[str] = []
    for strategy_name, runtime, contents_api_filename in _all_bootstrap_shell_variants():
        script = _render_setup_script(
            runtime=runtime,
            contents_api_filename=contents_api_filename,
        )
        path = tmp_path / f"{strategy_name}.sh"
        path.write_text(f"#!/usr/bin/env bash\n{script}", encoding="utf-8")
        script_paths.append(str(path))

    result = subprocess.run(
        [
            shellcheck,
            "-s",
            "bash",
            "-S",
            "error",
            "-e",
            "SC1091",
            *script_paths,
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_dropbear_command_contains_setup_script_and_args() -> None:
    runtime = SshRuntimeConfig(
        rtunnel_bin="/project/rtunnel",
        dropbear_deb_dir="/project/dropbear",
        setup_script="/project/setup_ssh.sh",
    )

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key="ssh-ed25519 AAAA... test@example",
        ssh_runtime=runtime,
    )

    joined = "\n".join(commands)
    assert any(line.startswith("DROPBEAR_DEB_DIR=/project/dropbear") for line in commands)
    assert any(line.startswith("SETUP_SCRIPT=/project/setup_ssh.sh") for line in commands)
    assert "falling back to dropbear bootstrap" in joined
    assert "RTUNNEL_URL=" in joined
    assert 'RTUNNEL_URL="$RTUNNEL_DOWNLOAD_URL"' in joined
    assert 'if [ ! -f "$BOOTSTRAP_SENTINEL" ] || [ ! -x "$RTUNNEL_BIN" ]; then ' in joined
    assert 'bash "$SETUP_SCRIPT" "$DROPBEAR_DEB_DIR" "$RTUNNEL_BIN_PATH"' in joined
    assert "dropbear" in joined
    assert 'ps -efww | grep -Eq "[d]ropbear.*-p.*${SSH_PORT}([[:space:]]|$)|' in joined
    assert 'rm -f "$BOOTSTRAP_SENTINEL"' in joined
    # Verify the long single-line command is gone — setup invocation should be its own line
    assert not any(
        ">/tmp/setup_ssh.log 2>&1; tail" in line for line in commands
    ), "setup + tail should be separate commands, not chained with ;"


def test_non_dropbear_uses_bootstrap_sentinel_and_start_only_commands() -> None:
    runtime = SshRuntimeConfig(
        rtunnel_bin="/project/rtunnel",
        dropbear_deb_dir=None,
    )

    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    assert f"BOOTSTRAP_SENTINEL={BOOTSTRAP_SENTINEL}" in joined
    assert 'if [ ! -f "$BOOTSTRAP_SENTINEL" ] || [ ! -x "$RTUNNEL_BIN" ] ' in joined
    assert "apt-get install -y -qq openssh-server" in joined
    assert 'touch "$BOOTSTRAP_SENTINEL"' in joined
    assert 'rm -f "$BOOTSTRAP_SENTINEL"' in joined
    assert "pkill -f 'sshd -p'" not in joined
    assert 'pkill -f "rtunnel.*:$PORT"' not in joined
    assert 'ps -efww | grep -Eq "[d]ropbear.*-p.*${SSH_PORT}([[:space:]]|$)|' in joined
    assert "[s]shd: .*-p ${SSH_PORT}([[:space:]]|$)|" in joined
    assert '[s]shd -p ${SSH_PORT}([[:space:]]|$)"; }' in joined
    assert (
        'ss -ltnp 2>/dev/null | grep -Eq "127\\\\.0\\\\.0\\\\.1:${SSH_PORT}[[:space:]]|' in joined
    )
    assert 'grep -Eq "[r]tunnel .*([[:space:]]|:)$PORT([[:space:]]|$)"' in joined
    # Shell snippet sets RTUNNEL_DOWNLOAD_URL dynamically
    assert "RTUNNEL_DOWNLOAD_URL=" in joined
    # RTUNNEL_URL compat alias
    assert 'RTUNNEL_URL="$RTUNNEL_DOWNLOAD_URL"' in joined
    # Curl block uses $RTUNNEL_DOWNLOAD_URL (not a literal URL)
    assert '"$RTUNNEL_DOWNLOAD_URL" -o "$RTUNNEL_BIN.tgz"' in joined


# ---------------------------------------------------------------------------
# contents_api_filename parameter
# ---------------------------------------------------------------------------


def test_contents_api_filename_inserts_copy_command() -> None:
    runtime = SshRuntimeConfig()
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
        contents_api_filename=".inspire_rtunnel_bin",
    )
    joined = "\n".join(commands)

    assert ".inspire_rtunnel_bin" in joined
    assert 'cp "$_d"/' in joined
    assert 'chmod +x "$RTUNNEL_BIN"' in joined
    assert '[ ! -x "$RTUNNEL_BIN" ]' in joined
    # Checks CWD first, then $HOME as fallback
    assert 'for _d in . "$HOME"' in joined


def test_contents_api_filename_none_has_no_move_command() -> None:
    runtime = SshRuntimeConfig()
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
        contents_api_filename=None,
    )
    joined = "\n".join(commands)

    assert ".inspire_rtunnel_bin" not in joined


def test_contents_api_filename_does_not_override_rtunnel_bin_path() -> None:
    runtime = SshRuntimeConfig(
        rtunnel_bin="/project/rtunnel",
    )
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
        contents_api_filename=".inspire_rtunnel_bin",
    )

    # Find first indices of the RTUNNEL_BIN_PATH copy and the contents API move
    bin_path_idx = None
    contents_api_idx = None
    for i, line in enumerate(commands):
        if (
            bin_path_idx is None
            and 'if [ -x "$RTUNNEL_BIN_PATH" ]; then RTUNNEL_BIN="$RTUNNEL_BIN_PATH"; ' in line
        ):
            bin_path_idx = i
        if contents_api_idx is None and ".inspire_rtunnel_bin" in line and "cp" in line:
            contents_api_idx = i

    assert bin_path_idx is not None, "RTUNNEL_BIN_PATH copy line not found"
    assert contents_api_idx is not None, "Contents API copy line not found"
    assert (
        bin_path_idx < contents_api_idx
    ), "RTUNNEL_BIN_PATH copy must come before contents API copy"


# ---------------------------------------------------------------------------
# SSHD_MISSING_MARKER detection
# ---------------------------------------------------------------------------


def test_default_path_includes_sshd_missing_marker() -> None:
    """Default openssh path (no dropbear) should contain the sshd missing marker."""
    runtime = SshRuntimeConfig()
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    assert SSHD_MISSING_MARKER in joined
    assert f'echo "{SSHD_MISSING_MARKER}"' in joined
    assert "[ ! -x /usr/sbin/sshd ]" in joined
    assert SSH_SERVER_MISSING_MARKER in joined


def test_dropbear_path_omits_sshd_missing_marker() -> None:
    """Dropbear path should NOT contain the sshd missing marker."""
    runtime = SshRuntimeConfig(
        dropbear_deb_dir="/project/dropbear",
    )
    commands = build_rtunnel_setup_commands(
        port=31337,
        ssh_port=22222,
        ssh_public_key=None,
        ssh_runtime=runtime,
    )
    joined = "\n".join(commands)

    assert SSHD_MISSING_MARKER not in joined
    assert SSH_SERVER_MISSING_MARKER in joined


def test_setup_plan_prefers_dropbear_and_ignores_sshd_deb_dir() -> None:
    runtime = SshRuntimeConfig(
        apt_mirror_url="http://mirror.example/debian/",
        sshd_deb_dir="/legacy/sshd",
    )

    plan = resolve_rtunnel_setup_plan(ssh_runtime=runtime)

    assert plan.bootstrap_mode == "dropbear"
    assert plan.bootstrap_strategy == "dropbear_mirror"
    assert plan.legacy_bootstrap is False
    assert plan.sshd_deb_dir_ignored is True
