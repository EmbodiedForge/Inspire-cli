"""Tests for GitHub remote log retrieval functionality."""

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import Mock, patch, MagicMock
from io import BytesIO
import zipfile

import pytest

from inspire.cli.utils.github import (
    GitHubAuthError,
    GitHubError,
    _artifact_name,
    _GitHubClient,
    _resolve_github_token,
    _get_repo,
    _prune_old_logs,
    trigger_log_retrieval_workflow,
    wait_for_log_artifact,
    fetch_remote_log_via_bridge,
)
from inspire.cli.utils.config import Config


# ============================================================================
# Unit tests for _GitHubClient
# ============================================================================


class Test_GitHubClient_request_json:
    """Test _GitHubClient.request_json with retry logic."""

    def test_success_on_first_attempt(self):
        """Successful JSON request returns parsed response."""
        client = _GitHubClient(token="test-token")

        response_data = {"foo": "bar"}

        with patch("inspire.cli.utils.github.urlrequest.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.headers.get_content_charset.return_value = "utf-8"
            mock_resp.read.return_value = json.dumps(response_data).encode("utf-8")
            mock_open.return_value.__enter__.return_value = mock_resp

            result = client.request_json("GET", "https://api.github.com/test", data=None)

            assert result == response_data
            assert mock_open.call_count == 1

    def test_success_with_retry_on_5xx(self):
        """Retries on 5xx errors and eventually succeeds."""
        client = _GitHubClient(token="test-token")

        response_data = {"success": True}

        with patch("inspire.cli.utils.github.urlrequest.urlopen") as mock_open:
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

            with patch("inspire.cli.utils.github.time.sleep"):
                result = client.request_json("GET", "https://api.github.com/test")

                assert result == response_data
                assert mock_open.call_count == 2

    def test_fails_on_4xx_without_retry(self):
        """Does not retry on 4xx errors."""
        client = _GitHubClient(token="test-token")

        from urllib.error import HTTPError

        with patch("inspire.cli.utils.github.urlrequest.urlopen") as mock_open:
            error_response = MagicMock()
            error_response.read.return_value = b'{"message": "Not found"}'

            mock_open.side_effect = HTTPError("url", 404, "Not Found", {}, error_response)

            with pytest.raises(GitHubError) as exc_info:
                client.request_json("GET", "https://api.github.com/test")

            assert "404" in str(exc_info.value)
            assert mock_open.call_count == 1  # No retry

    def test_retries_on_network_error(self):
        """Retries on URLError and eventually succeeds."""
        client = _GitHubClient(token="test-token")

        response_data = {"ok": True}

        from urllib.error import URLError

        with patch("inspire.cli.utils.github.urlrequest.urlopen") as mock_open:
            success_response = MagicMock()
            success_response.headers.get_content_charset.return_value = "utf-8"
            success_response.read.return_value = json.dumps(response_data).encode("utf-8")

            mock_open.side_effect = [
                URLError("Network error"),
                MagicMock(__enter__=lambda s: success_response, __exit__=lambda s, *a: None)
            ]

            with patch("inspire.cli.utils.github.time.sleep"):
                result = client.request_json("GET", "https://api.github.com/test")

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


class TestResolveGitHubToken:
    """Test GitHub token resolution."""

    def test_uses_env_token_first(self):
        """Uses INSP_GITHUB_TOKEN if set."""
        config = Config(
            username="user",
            password="pass",
            github_token="env-token-123",
        )

        token = _resolve_github_token(config)
        assert token == "env-token-123"

    def test_falls_back_to_gh_cli(self):
        """Falls back to `gh auth token` if env token not set."""
        config = Config(
            username="user",
            password="pass",
            github_token=None,
        )

        with patch("inspire.cli.utils.github.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.stdout = "gh-cli-token-456\n"
            mock_run.return_value = mock_proc

            token = _resolve_github_token(config)

            assert token == "gh-cli-token-456"
            mock_run.assert_called_once()

    def test_raises_error_when_no_token_available(self):
        """Raises GitHubAuthError when no token found."""
        config = Config(
            username="user",
            password="pass",
            github_token=None,
        )

        from subprocess import CalledProcessError

        with patch("inspire.cli.utils.github.subprocess.run") as mock_run:
            mock_run.side_effect = CalledProcessError(1, "gh")

            with pytest.raises(GitHubAuthError) as exc_info:
                _resolve_github_token(config)

            assert "GitHub authentication" in str(exc_info.value)


class TestGetRepo:
    """Test GitHub repo resolution."""

    def test_valid_repo_format(self):
        """Returns valid repo when INSP_GITHUB_REPO is set correctly."""
        config = Config(
            username="user",
            password="pass",
            github_repo="my-org/my-repo",
        )

        repo = _get_repo(config)
        assert repo == "my-org/my-repo"

    def test_raises_error_when_repo_missing(self):
        """Raises GitHubAuthError when INSP_GITHUB_REPO not set."""
        config = Config(
            username="user",
            password="pass",
            github_repo=None,
        )

        with pytest.raises(GitHubAuthError) as exc_info:
            _get_repo(config)

        assert "INSP_GITHUB_REPO" in str(exc_info.value)

    def test_raises_error_on_invalid_format(self):
        """Raises GitHubAuthError when repo format is invalid."""
        config = Config(
            username="user",
            password="pass",
            github_repo="invalid-format-no-slash",
        )

        with pytest.raises(GitHubAuthError) as exc_info:
            _get_repo(config)

        assert "owner/repo" in str(exc_info.value)


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
# Tests for wait_for_log_artifact
# ============================================================================


class TestWaitForLogArtifact:
    """Test artifact polling and download."""

    def test_timeout_after_max_wait(self):
        """Raises TimeoutError when artifact not found within timeout."""
        config = Config(
            username="user",
            password="pass",
            github_repo="org/repo",
            github_token="token",
            remote_timeout=1,  # 1 second timeout
        )

        with patch("inspire.cli.utils.github._get_repo") as mock_get_repo:
            with patch("inspire.cli.utils.github._get_client") as mock_get_client:
                mock_get_repo.return_value = "org/repo"
                mock_client = MagicMock()
                mock_client.request_json.return_value = {"artifacts": []}  # No artifacts
                mock_get_client.return_value = mock_client

                cache_path = Path("/tmp/log.log")

                with pytest.raises(TimeoutError) as exc_info:
                    wait_for_log_artifact(config, "job-123", "req-456", cache_path)

                assert "timed out" in str(exc_info.value).lower()
                assert "INSP_REMOTE_TIMEOUT" in str(exc_info.value)

    def test_downloads_artifact_when_found(self, tmp_path: Path):
        """Successfully downloads artifact when found."""
        config = Config(
            username="user",
            password="pass",
            github_repo="org/repo",
            github_token="token",
            remote_timeout=30,
        )

        cache_path = tmp_path / "job-123.log"

        # Create a fake zip artifact
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("job-123.log", "log content here")
        zip_data = zip_buffer.getvalue()

        with patch("inspire.cli.utils.github._get_repo") as mock_get_repo:
            with patch("inspire.cli.utils.github._get_client") as mock_get_client:
                with patch("inspire.cli.utils.github.time.sleep"):
                    mock_get_repo.return_value = "org/repo"
                    mock_client = MagicMock()

                    # Artifact found on first poll
                    mock_client.request_json.return_value = {
                        "artifacts": [
                            {
                                "name": "job-job-123-log-req-456",
                                "id": "artifact-123",
                                "expired": False,
                            }
                        ]
                    }
                    mock_client.request_bytes.return_value = zip_data
                    mock_get_client.return_value = mock_client

                    wait_for_log_artifact(config, "job-123", "req-456", cache_path)

                    assert cache_path.exists()
                    content = cache_path.read_text()
                    assert content == "log content here"


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
            github_repo="org/repo",
            github_token="token",
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

    def test_fetches_and_caches_remote_log(self, tmp_path: Path):
        """Fetches remote log and caches it locally."""
        config = Config(
            username="user",
            password="pass",
            github_repo="org/repo",
            github_token="token",
            remote_timeout=30,
        )

        cache_path = tmp_path / "job-123.log"

        # Create a fake zip artifact
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("job-123.log", "remote log content")
        zip_data = zip_buffer.getvalue()

        with patch("inspire.cli.utils.github.trigger_log_retrieval_workflow") as mock_trigger:
            with patch("inspire.cli.utils.github._get_repo") as mock_get_repo:
                with patch("inspire.cli.utils.github._get_client") as mock_get_client:
                    with patch("inspire.cli.utils.github.time.sleep"):
                        with patch("inspire.cli.utils.github._prune_old_logs") as mock_prune:
                            with patch("inspire.cli.utils.github._artifact_name") as mock_artifact_name:
                                mock_get_repo.return_value = "org/repo"
                                mock_artifact_name.return_value = "job-job-123-log-1234567890-9999"

                                mock_client = MagicMock()
                                mock_client.request_json.return_value = {
                                    "artifacts": [
                                        {
                                            "name": "job-job-123-log-1234567890-9999",
                                            "id": "artifact-123",
                                            "expired": False,
                                        }
                                    ]
                                }
                                mock_client.request_bytes.return_value = zip_data
                                mock_get_client.return_value = mock_client

                                result = fetch_remote_log_via_bridge(
                                    config=config,
                                    job_id="job-123",
                                    remote_log_path="/train/logs/job-123.log",
                                    cache_path=cache_path,
                                    refresh=True,
                                )

                                assert result == cache_path
                                assert cache_path.exists()
                                assert cache_path.read_text() == "remote log content"

                                # Verify pruning was called
                                mock_prune.assert_called_once()
                                assert mock_prune.call_args[0][0] == cache_path.parent
                                assert mock_prune.call_args[1]["max_age_days"] == 7
