from __future__ import annotations

import subprocess

from inspire.bridge.tunnel.models import BridgeProfile, TunnelConfig
from inspire.bridge.tunnel.ssh import _test_ssh_connection


def _make_config() -> tuple[TunnelConfig, BridgeProfile]:
    bridge = BridgeProfile(
        name="cpu",
        proxy_url="https://example/ws/proxy/31337/",
        ssh_user="root",
        ssh_port=22222,
        has_internet=True,
    )
    config = TunnelConfig(bridges={"cpu": bridge}, default_bridge="cpu")
    return config, bridge


def test_health_probe_falls_back_when_proxycommand_probe_is_false_negative(monkeypatch) -> None:
    config, bridge = _make_config()

    monkeypatch.setattr("inspire.bridge.tunnel.ssh._ensure_rtunnel_binary", lambda _cfg: None)
    monkeypatch.setattr("inspire.bridge.tunnel.ssh_exec._ensure_rtunnel_binary", lambda _cfg: None)

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        if cmd and cmd[0] == "ssh":
            return subprocess.CompletedProcess(cmd, 255, "", "proxy probe failed")
        if cmd and cmd[0] == "bash":
            return subprocess.CompletedProcess(cmd, 0, "ok\n", "")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("inspire.bridge.tunnel.ssh.subprocess.run", fake_run)

    assert _test_ssh_connection(bridge, config, timeout=5) is True
    assert calls[0][0] == "ssh"
    assert calls[1][0] == "bash"


def test_health_probe_returns_false_when_both_probe_paths_fail(monkeypatch) -> None:
    config, bridge = _make_config()

    monkeypatch.setattr("inspire.bridge.tunnel.ssh._ensure_rtunnel_binary", lambda _cfg: None)
    monkeypatch.setattr("inspire.bridge.tunnel.ssh_exec._ensure_rtunnel_binary", lambda _cfg: None)

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if cmd and cmd[0] == "ssh":
            return subprocess.CompletedProcess(cmd, 255, "", "proxy probe failed")
        if cmd and cmd[0] == "bash":
            return subprocess.CompletedProcess(cmd, 255, "", "fallback probe failed")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("inspire.bridge.tunnel.ssh.subprocess.run", fake_run)

    assert _test_ssh_connection(bridge, config, timeout=5) is False
