"""Tests for platform-aware rtunnel download URL defaults."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from inspire.config.rtunnel_defaults import (
    RTUNNEL_RELEASE_BASE_URL,
    _normalize_arch,
    _normalize_os,
    default_rtunnel_download_url,
    rtunnel_download_url_shell_snippet,
)


# ---------------------------------------------------------------------------
# _normalize_arch
# ---------------------------------------------------------------------------


class TestNormalizeArch:
    def test_arm64(self):
        assert _normalize_arch("arm64") == "arm64"

    def test_aarch64(self):
        assert _normalize_arch("aarch64") == "arm64"

    def test_x86_64(self):
        assert _normalize_arch("x86_64") == "amd64"

    def test_amd64(self):
        assert _normalize_arch("AMD64") == "amd64"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unsupported architecture"):
            _normalize_arch("mips64")


# ---------------------------------------------------------------------------
# _normalize_os
# ---------------------------------------------------------------------------


class TestNormalizeOs:
    def test_darwin(self):
        assert _normalize_os("Darwin") == "darwin"

    def test_linux(self):
        assert _normalize_os("Linux") == "linux"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unsupported OS"):
            _normalize_os("Windows")


# ---------------------------------------------------------------------------
# default_rtunnel_download_url
# ---------------------------------------------------------------------------


class TestDefaultRtunnelDownloadUrl:
    def test_linux_amd64(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("inspire.config.rtunnel_defaults.platform.system", lambda: "Linux")
        monkeypatch.setattr("inspire.config.rtunnel_defaults.platform.machine", lambda: "x86_64")
        url = default_rtunnel_download_url()
        assert url == f"{RTUNNEL_RELEASE_BASE_URL}/rtunnel-linux-amd64.tar.gz"

    def test_darwin_arm64(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("inspire.config.rtunnel_defaults.platform.system", lambda: "Darwin")
        monkeypatch.setattr("inspire.config.rtunnel_defaults.platform.machine", lambda: "arm64")
        url = default_rtunnel_download_url()
        assert url == f"{RTUNNEL_RELEASE_BASE_URL}/rtunnel-darwin-arm64.tar.gz"

    def test_linux_arm64(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("inspire.config.rtunnel_defaults.platform.system", lambda: "Linux")
        monkeypatch.setattr("inspire.config.rtunnel_defaults.platform.machine", lambda: "aarch64")
        url = default_rtunnel_download_url()
        assert url == f"{RTUNNEL_RELEASE_BASE_URL}/rtunnel-linux-arm64.tar.gz"

    def test_unsupported_os_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("inspire.config.rtunnel_defaults.platform.system", lambda: "Windows")
        monkeypatch.setattr("inspire.config.rtunnel_defaults.platform.machine", lambda: "x86_64")
        with pytest.raises(ValueError, match="Unsupported OS"):
            default_rtunnel_download_url()


# ---------------------------------------------------------------------------
# rtunnel_download_url_shell_snippet
# ---------------------------------------------------------------------------


class TestShellSnippet:
    def test_contains_uname(self):
        snippet = rtunnel_download_url_shell_snippet()
        assert "uname -s" in snippet
        assert "uname -m" in snippet

    def test_sets_rtunnel_download_url(self):
        snippet = rtunnel_download_url_shell_snippet()
        assert "RTUNNEL_DOWNLOAD_URL=" in snippet

    def test_contains_base_url(self):
        snippet = rtunnel_download_url_shell_snippet()
        assert RTUNNEL_RELEASE_BASE_URL in snippet

    def test_handles_arch_mapping(self):
        snippet = rtunnel_download_url_shell_snippet()
        assert "x86_64" in snippet
        assert "amd64" in snippet
        assert "arm64" in snippet
        assert "aarch64" in snippet


# ---------------------------------------------------------------------------
# _is_rtunnel_binary_usable (from bridge/tunnel/rtunnel.py)
# ---------------------------------------------------------------------------


class TestIsRtunnelBinaryUsable:
    def test_nonexistent_path(self, tmp_path: Path):
        from inspire.bridge.tunnel.rtunnel import _is_rtunnel_binary_usable

        assert _is_rtunnel_binary_usable(tmp_path / "no-such-file") is False

    def test_not_executable(self, tmp_path: Path):
        from inspire.bridge.tunnel.rtunnel import _is_rtunnel_binary_usable

        fake = tmp_path / "rtunnel"
        fake.write_text("not a binary")
        fake.chmod(0o644)
        assert _is_rtunnel_binary_usable(fake) is False

    def test_invalid_binary(self, tmp_path: Path):
        from inspire.bridge.tunnel.rtunnel import _is_rtunnel_binary_usable

        fake = tmp_path / "rtunnel"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        assert _is_rtunnel_binary_usable(fake) is False


# ---------------------------------------------------------------------------
# URL liveness (integration only)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_rtunnel_download_url_is_reachable():
    """Verify the default download URL returns HTTP 200 (HEAD request)."""
    import urllib.request

    url = default_rtunnel_download_url()
    req = urllib.request.Request(url, method="HEAD")
    resp = urllib.request.urlopen(req, timeout=10)
    assert resp.status == 200


def test_importing_config_package_does_not_crash_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
):
    """Importing inspire.config should not fail on unsupported hosts.

    The full import chain (inspire.config → schema → options/infra →
    rtunnel_defaults) triggers DEFAULT_RTUNNEL_DOWNLOAD_URL computation.
    On unsupported platforms this must fall back gracefully.
    """
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")

    # Clear the full package and all submodules that cache platform-dependent state.
    modules_to_pop = [
        name
        for name in sys.modules
        if name == "inspire.config" or name.startswith("inspire.config.")
    ]
    saved = {name: sys.modules[name] for name in modules_to_pop}
    for name in modules_to_pop:
        del sys.modules[name]

    try:
        config_pkg = importlib.import_module("inspire.config")
        assert hasattr(config_pkg, "Config")
        # The constant should fall back to linux-amd64 on unsupported platforms.
        from inspire.config.rtunnel_defaults import DEFAULT_RTUNNEL_DOWNLOAD_URL

        assert "linux-amd64" in DEFAULT_RTUNNEL_DOWNLOAD_URL

        # Config() construction must also succeed (default_factory fallback).
        Config = config_pkg.Config
        cfg = Config(username="test", password="test")
        assert "linux-amd64" in cfg.rtunnel_download_url
    finally:
        # Remove any modules created during the test, then restore originals.
        for name in list(sys.modules):
            if name == "inspire.config" or name.startswith("inspire.config."):
                del sys.modules[name]
        sys.modules.update(saved)


def test_importing_config_package_preserves_arch_on_unsupported_os(
    monkeypatch: pytest.MonkeyPatch,
):
    """Unsupported host OS fallback should keep a recognized architecture."""
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr("platform.machine", lambda: "ARM64")

    modules_to_pop = [
        name
        for name in sys.modules
        if name == "inspire.config" or name.startswith("inspire.config.")
    ]
    saved = {name: sys.modules[name] for name in modules_to_pop}
    for name in modules_to_pop:
        del sys.modules[name]

    try:
        config_pkg = importlib.import_module("inspire.config")
        assert hasattr(config_pkg, "Config")

        from inspire.config.rtunnel_defaults import DEFAULT_RTUNNEL_DOWNLOAD_URL

        assert "linux-arm64" in DEFAULT_RTUNNEL_DOWNLOAD_URL

        Config = config_pkg.Config
        cfg = Config(username="test", password="test")
        assert "linux-arm64" in cfg.rtunnel_download_url
    finally:
        for name in list(sys.modules):
            if name == "inspire.config" or name.startswith("inspire.config."):
                del sys.modules[name]
        sys.modules.update(saved)
