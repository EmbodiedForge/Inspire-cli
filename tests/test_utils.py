"""Tests for utility modules: job_cache, config, tunnel."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest

from inspire.cli.utils.job_cache import JobCache
from inspire.cli.utils.config import Config, ConfigError, _parse_remote_timeout, _parse_denylist, build_env_exports
from inspire.cli.utils.tunnel import (
    BridgeProfile,
    TunnelConfig,
    load_tunnel_config,
    save_tunnel_config,
    _get_proxy_command,
    get_ssh_command_args,
    TunnelNotAvailableError,
    BridgeNotFoundError,
)


# ===========================================================================
# JobCache tests
# ===========================================================================


class TestJobCache:
    """Tests for JobCache class."""

    def test_add_and_get_job(self, tmp_path: Path) -> None:
        """Test adding and retrieving a job."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-12345678-1234-1234-1234-123456789abc",
            name="test-job",
            resource="4xH200",
            command="python train.py",
            status="RUNNING",
            log_path="/train/logs/test.log",
        )

        job = cache.get_job("job-12345678-1234-1234-1234-123456789abc")
        assert job is not None
        assert job["job_id"] == "job-12345678-1234-1234-1234-123456789abc"
        assert job["name"] == "test-job"
        assert job["resource"] == "4xH200"
        assert job["command"] == "python train.py"
        assert job["status"] == "RUNNING"
        assert job["log_path"] == "/train/logs/test.log"

    def test_get_nonexistent_job(self, tmp_path: Path) -> None:
        """Test getting a job that doesn't exist."""
        cache = JobCache(str(tmp_path / "jobs.json"))
        job = cache.get_job("job-nonexistent-0000-0000-000000000000")
        assert job is None

    def test_update_status(self, tmp_path: Path) -> None:
        """Test updating job status."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-12345678-1234-1234-1234-123456789abc",
            name="test-job",
            resource="H200",
            command="echo test",
            status="PENDING",
        )

        cache.update_status("job-12345678-1234-1234-1234-123456789abc", "RUNNING")

        job = cache.get_job("job-12345678-1234-1234-1234-123456789abc")
        assert job is not None
        assert job["status"] == "RUNNING"

    def test_list_jobs_sorted_by_creation(self, tmp_path: Path) -> None:
        """Test that jobs are sorted by creation time (newest first)."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-aaaaaaa1-0000-0000-0000-000000000001",
            name="job-1",
            resource="H200",
            command="echo 1",
            status="RUNNING",
        )
        cache.add_job(
            job_id="job-aaaaaaa2-0000-0000-0000-000000000002",
            name="job-2",
            resource="H200",
            command="echo 2",
            status="PENDING",
        )

        jobs = cache.list_jobs(limit=10)
        assert len(jobs) == 2
        # Most recent should be first
        assert jobs[0]["name"] == "job-2"
        assert jobs[1]["name"] == "job-1"

    def test_list_jobs_with_status_filter(self, tmp_path: Path) -> None:
        """Test filtering jobs by status."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-aaaaaaa1-0000-0000-0000-000000000001",
            name="running-job",
            resource="H200",
            command="echo 1",
            status="RUNNING",
        )
        cache.add_job(
            job_id="job-aaaaaaa2-0000-0000-0000-000000000002",
            name="pending-job",
            resource="H200",
            command="echo 2",
            status="PENDING",
        )

        running_jobs = cache.list_jobs(status="RUNNING")
        assert len(running_jobs) == 1
        assert running_jobs[0]["name"] == "running-job"

    def test_list_jobs_with_exclude_statuses(self, tmp_path: Path) -> None:
        """Test excluding jobs by status."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-aaaaaaa1-0000-0000-0000-000000000001",
            name="running-job",
            resource="H200",
            command="echo 1",
            status="RUNNING",
        )
        cache.add_job(
            job_id="job-aaaaaaa2-0000-0000-0000-000000000002",
            name="failed-job",
            resource="H200",
            command="echo 2",
            status="FAILED",
        )

        active_jobs = cache.list_jobs(exclude_statuses={"FAILED", "CANCELLED"})
        assert len(active_jobs) == 1
        assert active_jobs[0]["name"] == "running-job"

    def test_list_jobs_with_limit(self, tmp_path: Path) -> None:
        """Test limiting number of returned jobs."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        for i in range(5):
            cache.add_job(
                job_id=f"job-aaaaaaa{i}-0000-0000-0000-00000000000{i}",
                name=f"job-{i}",
                resource="H200",
                command=f"echo {i}",
                status="RUNNING",
            )

        jobs = cache.list_jobs(limit=3)
        assert len(jobs) == 3

    def test_remove_job(self, tmp_path: Path) -> None:
        """Test removing a job from cache."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-12345678-1234-1234-1234-123456789abc",
            name="test-job",
            resource="H200",
            command="echo test",
            status="RUNNING",
        )

        assert cache.remove_job("job-12345678-1234-1234-1234-123456789abc") is True
        assert cache.get_job("job-12345678-1234-1234-1234-123456789abc") is None

        # Removing nonexistent job returns False
        assert cache.remove_job("job-nonexistent-0000-0000-000000000000") is False

    def test_clear_cache(self, tmp_path: Path) -> None:
        """Test clearing all jobs from cache."""
        cache = JobCache(str(tmp_path / "jobs.json"))

        cache.add_job(
            job_id="job-12345678-1234-1234-1234-123456789abc",
            name="test-job",
            resource="H200",
            command="echo test",
            status="RUNNING",
        )

        cache.clear()

        jobs = cache.list_jobs()
        assert len(jobs) == 0

    def test_log_offset_operations(self, tmp_path: Path) -> None:
        """Test log offset get/set/reset operations."""
        cache = JobCache(str(tmp_path / "jobs.json"))
        job_id = "job-12345678-1234-1234-1234-123456789abc"

        cache.add_job(
            job_id=job_id,
            name="test-job",
            resource="H200",
            command="echo test",
            status="RUNNING",
        )

        # Initial offset should be 0
        assert cache.get_log_offset(job_id) == 0

        # Set offset
        cache.set_log_offset(job_id, 1000)
        assert cache.get_log_offset(job_id) == 1000

        # Reset offset
        cache.reset_log_offset(job_id)
        assert cache.get_log_offset(job_id) == 0

    def test_default_cache_path(self) -> None:
        """Test that default cache path is in home directory."""
        cache = JobCache()
        expected_path = Path.home() / ".inspire" / "jobs.json"
        assert cache.cache_path == expected_path


# ===========================================================================
# Config tests
# ===========================================================================


class TestConfig:
    """Tests for Config class and helper functions."""

    def test_from_env_with_required_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading config from environment variables."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")

        config = Config.from_env()

        assert config.username == "testuser"
        assert config.password == "testpass"
        assert config.base_url == "https://api.example.com"

    def test_from_env_missing_username(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test error when username is missing."""
        monkeypatch.delenv("INSPIRE_USERNAME", raising=False)
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")

        with pytest.raises(ConfigError, match="Missing INSPIRE_USERNAME"):
            Config.from_env()

    def test_from_env_missing_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test error when password is missing."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.delenv("INSPIRE_PASSWORD", raising=False)

        with pytest.raises(ConfigError, match="Missing INSPIRE_PASSWORD"):
            Config.from_env()

    def test_from_env_require_target_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test error when target dir is required but missing."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.delenv("INSPIRE_TARGET_DIR", raising=False)

        with pytest.raises(ConfigError, match="Missing INSPIRE_TARGET_DIR"):
            Config.from_env(require_target_dir=True)

    def test_from_env_with_target_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading config with target dir."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setenv("INSPIRE_TARGET_DIR", "/shared/train")

        config = Config.from_env(require_target_dir=True)

        assert config.target_dir == "/shared/train"

    def test_from_env_with_api_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading config with custom API settings."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setenv("INSPIRE_TIMEOUT", "60")
        monkeypatch.setenv("INSPIRE_MAX_RETRIES", "5")
        monkeypatch.setenv("INSPIRE_RETRY_DELAY", "2.5")

        config = Config.from_env()

        assert config.timeout == 60
        assert config.max_retries == 5
        assert config.retry_delay == 2.5

    def test_from_env_invalid_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test error with invalid timeout value."""
        monkeypatch.setenv("INSPIRE_USERNAME", "testuser")
        monkeypatch.setenv("INSPIRE_PASSWORD", "testpass")
        monkeypatch.setenv("INSPIRE_TIMEOUT", "not-a-number")

        with pytest.raises(ConfigError, match="Invalid INSPIRE_TIMEOUT"):
            Config.from_env()

    def test_get_expanded_cache_path(self) -> None:
        """Test that cache path ~ is expanded."""
        config = Config(
            username="test",
            password="test",
            job_cache_path="~/.inspire/jobs.json",
        )

        expanded = config.get_expanded_cache_path()
        assert "~" not in expanded
        assert ".inspire/jobs.json" in expanded


class TestConfigHelpers:
    """Tests for config helper functions."""

    def test_parse_remote_timeout_valid(self) -> None:
        """Test parsing valid timeout values."""
        assert _parse_remote_timeout("90") == 90
        assert _parse_remote_timeout("300") == 300
        assert _parse_remote_timeout("5") == 5

    def test_parse_remote_timeout_invalid(self) -> None:
        """Test parsing invalid timeout values."""
        with pytest.raises(ConfigError, match="Invalid INSP_REMOTE_TIMEOUT"):
            _parse_remote_timeout("not-a-number")

    def test_parse_denylist_empty(self) -> None:
        """Test parsing empty denylist."""
        assert _parse_denylist(None) == []
        assert _parse_denylist("") == []

    def test_parse_denylist_comma_separated(self) -> None:
        """Test parsing comma-separated denylist."""
        result = _parse_denylist("*.pyc, *.pyo, __pycache__")
        assert result == ["*.pyc", "*.pyo", "__pycache__"]

    def test_parse_denylist_newline_separated(self) -> None:
        """Test parsing newline-separated denylist."""
        result = _parse_denylist("*.pyc\n*.pyo\n__pycache__")
        assert result == ["*.pyc", "*.pyo", "__pycache__"]

    def test_parse_denylist_mixed(self) -> None:
        """Test parsing mixed separator denylist."""
        result = _parse_denylist("*.pyc, *.pyo\n__pycache__")
        assert result == ["*.pyc", "*.pyo", "__pycache__"]

    def test_build_env_exports_empty(self) -> None:
        """Test building env exports with empty dict."""
        assert build_env_exports({}) == ""

    def test_build_env_exports_single(self) -> None:
        """Test building env exports with single var."""
        result = build_env_exports({"FOO": "bar"})
        assert result == 'export FOO="bar" && '

    def test_build_env_exports_multiple(self) -> None:
        """Test building env exports with multiple vars."""
        result = build_env_exports({"FOO": "bar", "BAZ": "qux"})
        # Order may vary due to dict iteration, so check both parts
        assert 'export FOO="bar"' in result
        assert 'export BAZ="qux"' in result
        assert result.endswith(" && ")
        assert " && " in result  # Separates the two exports


# ===========================================================================
# Tunnel tests
# ===========================================================================


class TestBridgeProfile:
    """Tests for BridgeProfile dataclass."""

    def test_to_dict(self) -> None:
        """Test converting profile to dict."""
        profile = BridgeProfile(
            name="test-bridge",
            proxy_url="https://proxy.example.com",
            ssh_user="admin",
            ssh_port=22222,
        )

        d = profile.to_dict()

        assert d["name"] == "test-bridge"
        assert d["proxy_url"] == "https://proxy.example.com"
        assert d["ssh_user"] == "admin"
        assert d["ssh_port"] == 22222

    def test_from_dict(self) -> None:
        """Test creating profile from dict."""
        d = {
            "name": "test-bridge",
            "proxy_url": "https://proxy.example.com",
            "ssh_user": "admin",
            "ssh_port": 22222,
        }

        profile = BridgeProfile.from_dict(d)

        assert profile.name == "test-bridge"
        assert profile.proxy_url == "https://proxy.example.com"
        assert profile.ssh_user == "admin"
        assert profile.ssh_port == 22222

    def test_from_dict_with_defaults(self) -> None:
        """Test creating profile from dict with default values."""
        d = {
            "name": "test-bridge",
            "proxy_url": "https://proxy.example.com",
        }

        profile = BridgeProfile.from_dict(d)

        assert profile.name == "test-bridge"
        assert profile.ssh_user == "root"  # default
        assert profile.ssh_port == 22222   # default


class TestTunnelConfig:
    """Tests for TunnelConfig class."""

    def test_add_bridge(self) -> None:
        """Test adding a bridge profile."""
        config = TunnelConfig()
        profile = BridgeProfile(
            name="test-bridge",
            proxy_url="https://proxy.example.com",
        )

        config.add_bridge(profile)

        assert "test-bridge" in config.bridges
        assert config.default_bridge == "test-bridge"

    def test_get_bridge_by_name(self) -> None:
        """Test getting bridge by name."""
        config = TunnelConfig()
        profile = BridgeProfile(
            name="test-bridge",
            proxy_url="https://proxy.example.com",
        )
        config.add_bridge(profile)

        retrieved = config.get_bridge("test-bridge")

        assert retrieved is not None
        assert retrieved.name == "test-bridge"

    def test_get_default_bridge(self) -> None:
        """Test getting default bridge."""
        config = TunnelConfig()
        profile = BridgeProfile(
            name="my-bridge",
            proxy_url="https://proxy.example.com",
        )
        config.add_bridge(profile)

        retrieved = config.get_bridge()  # No name = get default

        assert retrieved is not None
        assert retrieved.name == "my-bridge"

    def test_remove_bridge(self) -> None:
        """Test removing a bridge."""
        config = TunnelConfig()
        profile = BridgeProfile(
            name="test-bridge",
            proxy_url="https://proxy.example.com",
        )
        config.add_bridge(profile)

        result = config.remove_bridge("test-bridge")

        assert result is True
        assert "test-bridge" not in config.bridges
        assert config.default_bridge is None

    def test_list_bridges(self) -> None:
        """Test listing all bridges."""
        config = TunnelConfig()
        profile1 = BridgeProfile(name="bridge1", proxy_url="https://p1.example.com")
        profile2 = BridgeProfile(name="bridge2", proxy_url="https://p2.example.com")
        config.add_bridge(profile1)
        config.add_bridge(profile2)

        bridges = config.list_bridges()

        assert len(bridges) == 2
        names = {b.name for b in bridges}
        assert names == {"bridge1", "bridge2"}


class TestTunnelConfigPersistence:
    """Tests for tunnel config save/load."""

    def test_save_and_load(self, tmp_path: Path) -> None:
        """Test saving and loading tunnel config."""
        config = TunnelConfig(config_dir=tmp_path)
        profile = BridgeProfile(
            name="test-bridge",
            proxy_url="https://proxy.example.com",
            ssh_user="testuser",
            ssh_port=12345,
        )
        config.add_bridge(profile)

        save_tunnel_config(config)

        loaded = load_tunnel_config(tmp_path)

        assert "test-bridge" in loaded.bridges
        assert loaded.default_bridge == "test-bridge"
        bridge = loaded.bridges["test-bridge"]
        assert bridge.proxy_url == "https://proxy.example.com"
        assert bridge.ssh_user == "testuser"
        assert bridge.ssh_port == 12345


class TestProxyCommand:
    """Tests for SSH proxy command building."""

    def test_get_proxy_command_https_url(self, tmp_path: Path) -> None:
        """Test building proxy command from https URL."""
        bridge = BridgeProfile(
            name="test",
            proxy_url="https://proxy.example.com/tunnel",
        )
        rtunnel_bin = tmp_path / "rtunnel"

        cmd = _get_proxy_command(bridge, rtunnel_bin, quiet=False)

        # Should convert https to wss
        assert "wss://proxy.example.com/tunnel" in cmd
        assert str(rtunnel_bin) in cmd or "rtunnel" in cmd

    def test_get_proxy_command_with_quiet(self, tmp_path: Path) -> None:
        """Test building proxy command with quiet flag."""
        bridge = BridgeProfile(
            name="test",
            proxy_url="https://proxy.example.com/tunnel",
        )
        rtunnel_bin = tmp_path / "rtunnel"

        cmd = _get_proxy_command(bridge, rtunnel_bin, quiet=True)

        # Should include stderr redirect
        assert "2>/dev/null" in cmd
