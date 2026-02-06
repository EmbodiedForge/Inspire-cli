"""Tests for notebook rtunnel shell command construction."""

from __future__ import annotations

import pytest

from inspire.config.ssh_runtime import SshRuntimeConfig
from inspire.platform.web.browser_api.notebooks.playwright.rtunnel.commands import (
    build_rtunnel_setup_commands,
)


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
    assert "SSHD_DEB_DIR=/project/sshd" in joined
    assert "https://project.example/rtunnel.tgz" in joined
    assert "/env/rtunnel" not in joined
    assert "/env/sshd" not in joined


def test_dropbear_requires_setup_script() -> None:
    runtime = SshRuntimeConfig(
        dropbear_deb_dir="/project/dropbear",
        setup_script=None,
    )

    with pytest.raises(ValueError, match="setup_script"):
        build_rtunnel_setup_commands(
            port=31337,
            ssh_port=22222,
            ssh_public_key=None,
            ssh_runtime=runtime,
        )


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

    assert any(line.startswith("DROPBEAR_DEB_DIR=/project/dropbear") for line in commands)
    assert any(
        "bash /project/setup_ssh.sh /project/dropbear /project/rtunnel" in line for line in commands
    )
