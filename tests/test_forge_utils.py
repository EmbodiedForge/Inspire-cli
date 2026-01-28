"""Tests for forge utilities (GitHub and Gitea)."""

import json
import pytest

from inspire.cli.utils import forge as forge_module
from inspire.cli.utils.config import Config
from inspire.cli.utils.forge import (
    GitPlatform,
    GiteaClient,
    GitHubClient,
    create_forge_client,
    _resolve_platform,
    _sanitize_token,
)


class DummyClient:
    """Dummy client for testing."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.calls = []

    def get_api_base(self, repo: str) -> str:  # noqa: ANN001
        return f"https://example.test/repos/{repo}/actions"

    def get_pagination_params(self, limit: int, page: int) -> str:  # noqa: ANN001
        return f"limit={limit}&page={page}"

    def request_json(self, method: str, url: str):  # noqa: ANN001
        self.calls.append(url)
        if "page=1" in url:
            return {"total_count": 25, "workflow_runs": []}
        if "page=2" in url:
            payload = json.dumps({"inputs": {"request_id": self.request_id}})
            return {
                "workflow_runs": [
                    {
                        "event_payload": payload,
                        "status": "success",
                        "conclusion": "success",
                        "id": 42,
                        "html_url": "https://example.test/run/42",
                    }
                ]
            }
        return {"workflow_runs": []}


def test_wait_for_bridge_action_completion_checks_last_page(monkeypatch: pytest.MonkeyPatch):
    """Test that wait_for_bridge_action_completion checks the last page when total_count > limit."""
    request_id = "req-123"
    client = DummyClient(request_id=request_id)

    def mock_get_active_repo(config):
        return "org/repo"

    monkeypatch.setattr(forge_module, "create_forge_client", lambda config: client)
    monkeypatch.setattr(forge_module, "_get_active_repo", mock_get_active_repo)
    monkeypatch.setattr(forge_module.time, "time", lambda: 0)
    monkeypatch.setattr(forge_module.time, "sleep", lambda *_args, **_kwargs: None)

    # Config needs gitea_repo set to avoid errors in _get_active_repo
    config = Config(username="user", password="pass", gitea_repo="org/repo")

    result = forge_module.wait_for_bridge_action_completion(
        config=config,
        request_id=request_id,
        timeout=10,
    )

    assert result["run_id"] == 42
    assert result["conclusion"] == "success"
    assert any("page=2" in call for call in client.calls)


class TestGiteaClient:
    """Tests for GiteaClient."""

    def test_auth_header(self):
        """Test that GiteaClient uses 'token' auth header."""
        client = GiteaClient(token="test-token", server_url="https://codeberg.org")
        assert client.get_auth_header() == "token test-token"

    def test_api_base(self):
        """Test that GiteaClient uses correct API base path."""
        client = GiteaClient(token="test-token", server_url="https://codeberg.org")
        assert client.get_api_base("owner/repo") == "https://codeberg.org/api/v1/repos/owner/repo/actions"

    def test_raw_file_url(self):
        """Test that GiteaClient uses correct raw file URL format."""
        client = GiteaClient(token="test-token", server_url="https://codeberg.org")
        assert client.get_raw_file_url("owner/repo", "main", "test.txt") == (
            "https://codeberg.org/api/v1/repos/owner/repo/raw/main/test.txt"
        )

    def test_pagination_params(self):
        """Test that GiteaClient uses limit for pagination."""
        client = GiteaClient(token="test-token", server_url="https://codeberg.org")
        assert client.get_pagination_params(20, 1) == "limit=20&page=1"
        assert client.get_pagination_params(50, 3) == "limit=50&page=3"


class TestGitHubClient:
    """Tests for GitHubClient."""

    def test_auth_header(self):
        """Test that GitHubClient uses 'Bearer' auth header."""
        client = GitHubClient(token="ghp_test-token", server_url="https://github.com")
        assert client.get_auth_header() == "Bearer ghp_test-token"

    def test_api_base_github_com(self):
        """Test that GitHubClient uses api.github.com for github.com."""
        client = GitHubClient(token="test-token", server_url="https://github.com")
        assert client.get_api_base("owner/repo") == "https://api.github.com/repos/owner/repo/actions"

    def test_api_base_github_enterprise(self):
        """Test that GitHubClient uses /api/v3/ for GitHub Enterprise."""
        client = GitHubClient(token="test-token", server_url="https://github.example.com")
        assert client.get_api_base("owner/repo") == "https://github.example.com/api/v3/repos/owner/repo/actions"

    def test_raw_file_url_github_com(self):
        """Test that GitHubClient uses raw.githubusercontent.com for github.com."""
        client = GitHubClient(token="test-token", server_url="https://github.com")
        assert client.get_raw_file_url("owner/repo", "main", "test.txt") == (
            "https://raw.githubusercontent.com/owner/repo/main/test.txt"
        )

    def test_raw_file_url_github_enterprise(self):
        """Test that GitHubClient handles GitHub Enterprise URLs."""
        client = GitHubClient(token="test-token", server_url="https://github.example.com")
        assert client.get_raw_file_url("owner/repo", "main", "test.txt") == (
            "https://raw.github.example.com/owner/repo/main/test.txt"
        )

    def test_pagination_params(self):
        """Test that GitHubClient uses per_page for pagination."""
        client = GitHubClient(token="test-token", server_url="https://github.com")
        assert client.get_pagination_params(20, 1) == "per_page=20&page=1"
        assert client.get_pagination_params(50, 3) == "per_page=50&page=3"


class TestTokenSanitization:
    """Tests for token sanitization."""

    @pytest.mark.parametrize(
        ("input_token", "expected"),
        [
            ("simple-token", "simple-token"),
            ("token simple-token", "simple-token"),
            ("bearer simple-token", "simple-token"),
            ("Bearer simple-token", "simple-token"),
            ("  token-with-spaces  ", "token-with-spaces"),
        ],
    )
    def test_sanitize_token(self, input_token: str, expected: str):
        """Test token sanitization removes common prefixes."""
        assert _sanitize_token(input_token) == expected


class TestPlatformResolution:
    """Tests for platform resolution logic."""

    def test_default_platform_is_gitea(self):
        """Test that default platform is Gitea when nothing is configured."""
        config = Config(username="user", password="pass")
        assert _resolve_platform(config) == GitPlatform.GITEA

    def test_explicit_gitea_platform(self):
        """Test explicit Gitea platform selection."""
        config = Config(username="user", password="pass", git_platform="gitea")
        assert _resolve_platform(config) == GitPlatform.GITEA

    def test_explicit_github_platform(self):
        """Test explicit GitHub platform selection."""
        config = Config(username="user", password="pass", git_platform="github")
        assert _resolve_platform(config) == GitPlatform.GITHUB

    def test_auto_detect_github_from_repo(self):
        """Test that GitHub is auto-detected when github_repo is set."""
        config = Config(
            username="user",
            password="pass",
            github_repo="owner/repo",
            github_token="ghp_test",  # Token is also required for practical use
        )
        assert _resolve_platform(config) == GitPlatform.GITHUB

    def test_auto_detect_github_from_token(self):
        """Test that GitHub is auto-detected when github_token is set."""
        config = Config(
            username="user",
            password="pass",
            github_token="ghp_test",
            github_repo="owner/repo",  # Repo is also required for practical use
        )
        assert _resolve_platform(config) == GitPlatform.GITHUB

    def test_gitea_takes_precedence_when_both_set(self):
        """Test that explicit git_platform takes precedence over auto-detection."""
        config = Config(
            username="user",
            password="pass",
            git_platform="gitea",
            github_repo="owner/repo",
            github_token="ghp_test",
        )
        assert _resolve_platform(config) == GitPlatform.GITEA

    @pytest.mark.parametrize(
        ("platform_str", "expected"),
        [
            ("github", GitPlatform.GITHUB),
            ("GITHUB", GitPlatform.GITHUB),
            ("GitHub", GitPlatform.GITHUB),
            ("gitea", GitPlatform.GITEA),
            ("GITEA", GitPlatform.GITEA),
            ("Gitea", GitPlatform.GITEA),
        ],
    )
    def test_platform_string_case_insensitive(self, platform_str: str, expected: GitPlatform):
        """Test that platform string is case-insensitive."""
        config = Config(username="user", password="pass", git_platform=platform_str)
        assert _resolve_platform(config) == expected


class TestForgeClientFactory:
    """Tests for create_forge_client factory function."""

    def test_creates_gitea_client_by_default(self):
        """Test that factory creates GiteaClient by default."""
        config = Config(
            username="user",
            password="pass",
            gitea_token="test-token",
            gitea_server="https://codeberg.org",
        )
        client = create_forge_client(config)
        assert isinstance(client, GiteaClient)
        assert client.token == "test-token"
        assert client.server_url == "https://codeberg.org"

    def test_creates_github_client_when_platform_is_github(self):
        """Test that factory creates GitHubClient when platform is GitHub."""
        config = Config(
            username="user",
            password="pass",
            git_platform="github",
            github_token="ghp_test-token",
            github_server="https://github.com",
        )
        client = create_forge_client(config)
        assert isinstance(client, GitHubClient)
        assert client.token == "ghp_test-token"
        assert client.server_url == "https://github.com"

    def test_creates_github_client_when_auto_detected(self):
        """Test that factory creates GitHubClient when GitHub is auto-detected."""
        config = Config(
            username="user",
            password="pass",
            github_repo="owner/repo",
            github_token="ghp_test",
            github_server="https://github.com",
            git_platform="",  # Empty string to trigger auto-detection
        )
        client = create_forge_client(config)
        assert isinstance(client, GitHubClient)


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_extract_total_count_variations(self):
        """Test _extract_total_count handles various response formats."""
        from inspire.cli.utils.forge import _extract_total_count

        assert _extract_total_count({"total_count": 42}) == 42
        assert _extract_total_count({"total": 42}) == 42
        assert _extract_total_count({"count": 42}) == 42
        assert _extract_total_count({}) is None
        assert _extract_total_count({"total_count": "42"}) == 42
        assert _extract_total_count({"total_count": "invalid"}) is None

    def test_parse_event_inputs(self):
        """Test _parse_event_inputs handles various event payloads."""
        from inspire.cli.utils.forge import _parse_event_inputs

        # Valid JSON with inputs
        event = json.dumps({"inputs": {"key": "value"}})
        assert _parse_event_inputs({"event_payload": event}) == {"key": "value"}

        # Empty event payload
        assert _parse_event_inputs({"event_payload": ""}) == {}

        # Invalid JSON
        assert _parse_event_inputs({"event_payload": "invalid"}) == {}

        # Missing inputs key
        event = json.dumps({"data": "value"})
        assert _parse_event_inputs({"event_payload": event}) == {}

    def test_matches_inputs(self):
        """Test _matches_inputs logic."""
        from inspire.cli.utils.forge import _matches_inputs

        inputs = {"key1": "value1", "key2": "value2"}

        # Exact match
        expected = {"key1": "value1"}
        assert _matches_inputs(inputs, expected) is True

        # Partial match with empty expected value (should skip)
        expected = {"key1": ""}
        assert _matches_inputs(inputs, expected) is True

        # Mismatch
        expected = {"key1": "different"}
        assert _matches_inputs(inputs, expected) is False

        # Multiple keys match
        expected = {"key1": "value1", "key2": "value2"}
        assert _matches_inputs(inputs, expected) is True

    def test_find_run_by_inputs(self):
        """Test _find_run_by_inputs logic."""
        from inspire.cli.utils.forge import _find_run_by_inputs

        runs = [
            {
                "event_payload": json.dumps({"inputs": {"request_id": "req-1"}}),
                "id": 1,
            },
            {
                "event_payload": json.dumps({"inputs": {"request_id": "req-2"}}),
                "id": 2,
            },
        ]

        # Find existing run
        result = _find_run_by_inputs(runs, {"request_id": "req-1"})
        assert result is not None
        assert result["id"] == 1

        # Not found
        result = _find_run_by_inputs(runs, {"request_id": "req-3"})
        assert result is None

        # Empty inputs in run
        runs_empty = [{"event_payload": "", "id": 3}]
        result = _find_run_by_inputs(runs_empty, {"request_id": "req-1"})
        assert result is None

    def test_artifact_name(self):
        """Test _artifact_name generates correct format."""
        from inspire.cli.utils.forge import _artifact_name

        result = _artifact_name("job-123", "req-456")
        assert result == "job-job-123-log-req-456"
