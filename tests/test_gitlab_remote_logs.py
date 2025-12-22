"""Tests for GitLab remote log retrieval functionality."""

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import Mock, patch, MagicMock
from io import BytesIO
import zipfile

import pytest

from inspire.cli.utils.gitlab import (
    GitLabAuthError,
    GitLabError,
    _artifact_name,
    _GitLabClient,
    _resolve_gitlab_token,
    _get_project_id,
    _prune_old_logs,
    trigger_log_retrieval_pipeline,
    wait_for_log_artifact,
    fetch_remote_log_via_bridge,
)
from inspire.cli.utils.config import Config


# ============================================================================
# Unit tests for _GitLabClient
# ============================================================================


class Test_GitLabClient_request_json:
    """Test _GitLabClient.request_json with retry logic."""

    def test_success_on_first_attempt(self):
        """Successful JSON request returns parsed response."""
        client = _GitLabClient(token="test-token")

        response_data = {"foo": "bar"}

        with patch("inspire.cli.utils.gitlab.urlrequest.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.headers.get_content_charset.return_value = "utf-8"
            mock_resp.read.return_value = json.dumps(response_data).encode("utf-8")
            mock_open.return_value.__enter__.return_value = mock_resp

            result = client.request_json("GET", "https://gitlab.com/api/v4/test", data=None)

            assert result == response_data
            assert mock_open.call_count == 1

    def test_success_with_retry_on_5xx(self):
        """Retries on 5xx errors and eventually succeeds."""
        client = _GitLabClient(token="test-token")

        response_data = {"success": True}

        with patch("inspire.cli.utils.gitlab.urlrequest.urlopen") as mock_open:
            # First call: 500 error; second call: success
            error_response = MagicMock()
            error_response.read.return_value = b'{"message": "Server error"}'

            success_response = MagicMock()
            success_response.headers.get_content_charset.return_value = "utf-8"
            success_response.read.return_value = json.dumps(response_data).encode("utf-8")

            from urllib.error import HTTPError

            # First call raises 500, second call succeeds
            mock_open.side_effect = [
                HTTPError("url", 500, "Server Error", {}, None),
                MagicMock(__enter__=lambda s: success_response, __exit__=lambda s, *a: None)
            ]

            with patch("inspire.cli.utils.gitlab.time.sleep"):
                result = client.request_json("GET", "https://gitlab.com/api/v4/test")

                assert result == response_data
                assert mock_open.call_count == 2

    def test_fails_on_4xx_without_retry(self):
        """Does not retry on 4xx errors."""
        client = _GitLabClient(token="test-token")

        from urllib.error import HTTPError

        with patch("inspire.cli.utils.gitlab.urlrequest.urlopen") as mock_open:
            error_response = MagicMock()
            error_response.read.return_value = b'{"message": "Not found"}'

            mock_open.side_effect = HTTPError("url", 404, "Not Found", {}, error_response)

            with pytest.raises(GitLabError) as exc_info:
                client.request_json("GET", "https://gitlab.com/api/v4/test")

            assert "404" in str(exc_info.value)
            assert mock_open.call_count == 1  # No retry

    def test_retries_on_network_error(self):
        """Retries on URLError and eventually succeeds."""
        client = _GitLabClient(token="test-token")

        response_data = {"ok": True}

        from urllib.error import URLError

        with patch("inspire.cli.utils.gitlab.urlrequest.urlopen") as mock_open:
            success_response = MagicMock()
            success_response.headers.get_content_charset.return_value = "utf-8"
            success_response.read.return_value = json.dumps(response_data).encode("utf-8")

            mock_open.side_effect = [
                URLError("Network error"),
                MagicMock(__enter__=lambda s: success_response, __exit__=lambda s, *a: None)
            ]

            with patch("inspire.cli.utils.gitlab.time.sleep"):
                result = client.request_json("GET", "https://gitlab.com/api/v4/test")

                assert result == response_data
                assert mock_open.call_count == 2


# ============================================================================
# Unit tests for helper functions
# ============================================================================


class TestArtifactName:
    """Test artifact naming helper."""

    def test_artifact_name_format(self):
        """Artifact name follows expected format."""
        name = _artifact_name("job-123", "1234567890-5678")
        assert name == "job-job-123-log-1234567890-5678"


class TestResolveGitLabToken:
    """Test GitLab token resolution."""

    def test_uses_env_token_first(self):
        """Uses INSP_GITLAB_TOKEN if set."""
        config = Config(
            username="user",
            password="pass",
            gitlab_token="env-token-123",
        )

        token = _resolve_gitlab_token(config)
        assert token == "env-token-123"

    def test_falls_back_to_glab_cli(self):
        """Falls back to `glab auth status` if env token not set."""
        config = Config(
            username="user",
            password="pass",
            gitlab_token=None,
        )

        with patch("inspire.cli.utils.gitlab.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.stderr = "Token: glpat-test-token-456\n"
            mock_run.return_value = mock_proc

            token = _resolve_gitlab_token(config)

            assert token == "glpat-test-token-456"
            mock_run.assert_called_once()

    def test_raises_error_when_no_token_available(self):
        """Raises GitLabAuthError when no token found."""
        config = Config(
            username="user",
            password="pass",
            gitlab_token=None,
        )

        from subprocess import CalledProcessError

        with patch("inspire.cli.utils.gitlab.subprocess.run") as mock_run:
            mock_run.side_effect = CalledProcessError(1, "glab")

            with pytest.raises(GitLabAuthError) as exc_info:
                _resolve_gitlab_token(config)

            assert "GitLab authentication" in str(exc_info.value)


class TestGetProjectId:
    """Test GitLab project ID resolution."""

    def test_valid_project_format(self):
        """Returns URL-encoded project when INSP_GITLAB_PROJECT is set correctly."""
        config = Config(
            username="user",
            password="pass",
            gitlab_project="my-org/my-repo",
        )

        project_id = _get_project_id(config)
        assert project_id == "my-org%2Fmy-repo"

    def test_raises_error_when_project_missing(self):
        """Raises GitLabAuthError when INSP_GITLAB_PROJECT not set."""
        config = Config(
            username="user",
            password="pass",
            gitlab_project=None,
        )

        with pytest.raises(GitLabAuthError) as exc_info:
            _get_project_id(config)

        assert "INSP_GITLAB_PROJECT" in str(exc_info.value)


# ============================================================================
# Tests for cache pruning
# ============================================================================


class TestPruneOldLogs:
    """Test log cache pruning."""

    def test_removes_logs_older_than_7_days(self, tmp_path: Path):
        """Deletes log files older than max_age_days."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        # Create old log (8 days old)
        old_log = cache_dir / "old.log"
        old_log.write_text("old")
        old_log.touch()
        old_mtime = time.time() - (8 * 24 * 3600)
        old_log.stat()  # Ensure it's created first
        import os
        os.utime(old_log, (old_mtime, old_mtime))

        # Create new log (1 day old)
        new_log = cache_dir / "new.log"
        new_log.write_text("new")
        new_mtime = time.time() - (1 * 24 * 3600)
        os.utime(new_log, (new_mtime, new_mtime))

        _prune_old_logs(cache_dir, max_age_days=7)

        assert not old_log.exists()
        assert new_log.exists()

    def test_handles_missing_cache_dir_gracefully(self):
        """Silently handles missing cache directory."""
        cache_dir = Path("/nonexistent/path/cache")

        # Should not raise
        _prune_old_logs(cache_dir)


# ============================================================================
# Tests for fetch_remote_log_via_bridge
# ============================================================================


class TestFetchRemoteLogViaBridge:
    """Test high-level remote log fetch orchestration."""

    def test_returns_cached_file_if_exists(self, tmp_path: Path):
        """Returns existing cached file without re-fetching."""
        config = Config(
            username="user",
            password="pass",
            gitlab_project="org/repo",
            gitlab_token="token",
        )

        cache_path = tmp_path / "job-123.log"
        cache_path.write_text("cached content")

        result = fetch_remote_log_via_bridge(
            config=config,
            job_id="job-123",
            remote_log_path="/path/to/log",
            cache_path=cache_path,
            refresh=False,
        )

        assert result == cache_path
        assert result.read_text() == "cached content"
