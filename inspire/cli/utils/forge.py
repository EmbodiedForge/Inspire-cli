"""Git forge abstraction for GitHub and Gitea Actions.

This module provides a unified interface for interacting with both
GitHub Actions and Gitea Actions APIs, which are largely compatible
but have some differences in authentication and endpoints.

The factory function create_forge_client() returns the appropriate
client based on the configured platform.
"""

from __future__ import annotations

import json
import logging
import os
import time
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from inspire.cli.utils.config import Config, ConfigError


class GitPlatform(Enum):
    """Supported Git platforms for Actions."""
    GITEA = "gitea"
    GITHUB = "github"


class ForgeAuthError(ConfigError):
    """Authentication/configuration error for forge access."""
    pass


class ForgeError(Exception):
    """Generic forge API or workflow error."""
    pass


class GiteaAuthError(ForgeAuthError):
    """Authentication error for Gitea (backward compatibility alias)."""
    pass


class GiteaError(ForgeError):
    """Generic Gitea error (backward compatibility alias)."""
    pass


def _sanitize_token(token: str) -> str:
    """Sanitize a token by removing common prefixes."""
    token = token.strip()
    lower = token.lower()
    if lower.startswith("bearer "):
        token = token[7:].strip()
    elif lower.startswith("token "):
        token = token[6:].strip()
    return token


def _resolve_platform(config: Config) -> GitPlatform:
    """Resolve which Git platform to use from config.

    Priority:
    1. INSP_GIT_PLATFORM / git.platform setting (if explicitly set)
    2. Auto-detect from GitHub vars if set
    3. Default to GITEA for backward compatibility
    """
    # Check environment variable first
    platform_env = os.getenv("INSP_GIT_PLATFORM", "").strip().lower()

    # Check config setting
    platform_config = (getattr(config, "git_platform", None) or "").strip().lower()

    # Use env var if set, otherwise use config
    platform_str = platform_env if platform_env else platform_config

    if platform_str == "github":
        return GitPlatform.GITHUB
    elif platform_str == "gitea":
        return GitPlatform.GITEA

    # Auto-detect: if GitHub vars are set, use GitHub
    if config.github_repo or config.github_token:
        return GitPlatform.GITHUB

    # Default to Gitea for backward compatibility
    return GitPlatform.GITEA


def _get_active_repo(config: Config) -> str:
    """Get the repository from the active platform config."""
    platform = _resolve_platform(config)

    if platform == GitPlatform.GITHUB:
        repo = (getattr(config, "github_repo", None) or "").strip()
        if not repo:
            raise ForgeAuthError(
                "GitHub operations require INSP_GITHUB_REPO to be set.\n"
                "Use 'owner/repo' format.\n"
                "Example: export INSP_GITHUB_REPO='my-org/my-repo'"
            )
        if "/" not in repo:
            raise ForgeAuthError(
                f"Invalid INSP_GITHUB_REPO format '{repo}'. Expected 'owner/repo'."
            )
        return repo
    else:
        repo = (config.gitea_repo or "").strip()
        if not repo:
            raise ForgeAuthError(
                "Gitea operations require INSP_GITEA_REPO to be set.\n"
                "Use 'owner/repo' format.\n"
                "Example: export INSP_GITEA_REPO='owner/repo'"
            )
        if "/" not in repo:
            raise ForgeAuthError(
                f"Invalid INSP_GITEA_REPO format '{repo}'. Expected 'owner/repo'."
            )
        return repo


def _get_active_token(config: Config) -> str:
    """Get the token from the active platform config."""
    platform = _resolve_platform(config)

    if platform == GitPlatform.GITHUB:
        token = getattr(config, "github_token", None)
        if not token:
            raise ForgeAuthError(
                "GitHub operations require INSP_GITHUB_TOKEN environment variable.\n"
                "Set it with: export INSP_GITHUB_TOKEN='ghp_...'"
            )
        return _sanitize_token(token)
    else:
        if not config.gitea_token:
            raise ForgeAuthError(
                "Gitea operations require INSP_GITEA_TOKEN environment variable.\n"
                "Set it with: export INSP_GITEA_TOKEN='...'"
            )
        return _sanitize_token(config.gitea_token)


def _get_active_server(config: Config) -> str:
    """Get the server URL from the active platform config."""
    platform = _resolve_platform(config)

    if platform == GitPlatform.GITHUB:
        return (getattr(config, "github_server", None) or "https://github.com").rstrip("/")
    else:
        return (config.gitea_server or "https://codeberg.org").rstrip("/")


def _get_active_workflow_file(config: Config, workflow_type: str) -> str:
    """Get the workflow filename from the active platform config.

    Args:
        config: CLI configuration
        workflow_type: One of 'log', 'sync', 'bridge'
    """
    platform = _resolve_platform(config)

    if platform == GitPlatform.GITHUB:
        if workflow_type == "log":
            return getattr(config, "github_log_workflow", "retrieve_job_log.yml")
        elif workflow_type == "sync":
            return getattr(config, "github_sync_workflow", "sync_code.yml")
        elif workflow_type == "bridge":
            return getattr(config, "github_bridge_workflow", "run_bridge_action.yml")
    else:
        if workflow_type == "log":
            return config.gitea_log_workflow
        elif workflow_type == "sync":
            return config.gitea_sync_workflow
        elif workflow_type == "bridge":
            return config.gitea_bridge_workflow

    # Default fallback
    return "workflow.yml"


@dataclass
class ForgeClient(ABC):
    """Abstract base class for Git forge clients."""

    token: str
    server_url: str

    @abstractmethod
    def get_auth_header(self) -> str:
        """Return the Authorization header value."""
        pass

    @abstractmethod
    def get_api_base(self, repo: str) -> str:
        """Return the API base URL for the given repo."""
        pass

    @abstractmethod
    def get_raw_file_url(self, repo: str, branch: str, filepath: str) -> str:
        """Return the URL to fetch a raw file."""
        pass

    @abstractmethod
    def get_pagination_params(self, limit: int, page: int) -> str:
        """Return query string for pagination (platform-specific)."""
        pass

    def _build_request(
        self,
        method: str,
        url: str,
        data: Optional[dict] = None,
        accept: str = "application/json",
    ) -> urlrequest.Request:
        headers = {
            "Authorization": self.get_auth_header(),
            "Accept": accept,
            "User-Agent": "inspire-cli",
        }
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            body = None

        req = urlrequest.Request(url, data=body, headers=headers)
        req.get_method = lambda: method  # type: ignore[assignment]
        return req

    def request_json(
        self, method: str, url: str, data: Optional[dict] = None
    ) -> dict:
        """Make a JSON request with retry."""
        max_retries = 3
        retry_delay = 2.0

        for attempt in range(max_retries + 1):
            try:
                req = self._build_request(method, url, data)
                with urlrequest.urlopen(req, timeout=60) as resp:
                    charset = resp.headers.get_content_charset("utf-8")
                    payload = resp.read().decode(charset)
                    if not payload:
                        return {}
                    return json.loads(payload)
            except urlerror.HTTPError as e:
                detail = None
                try:
                    raw = e.read().decode("utf-8")
                    parsed = json.loads(raw)
                    detail = parsed.get("message") or parsed.get("error")
                except Exception:
                    pass
                msg = f"API error {e.code} for {url}"
                if detail:
                    msg += f": {detail}"

                if e.code >= 500 and attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ForgeError(msg)
            except urlerror.URLError as e:
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ForgeError(f"API request failed for {url}: {e}")

        return {}

    def request_bytes(self, method: str, url: str) -> bytes:
        """Make a binary request with retry."""
        max_retries = 3
        retry_delay = 2.0

        for attempt in range(max_retries + 1):
            try:
                logging.debug(
                    "Forge request_bytes %s %s (attempt %d)",
                    method,
                    url,
                    attempt + 1,
                )
                req = self._build_request(
                    method, url, data=None, accept="application/octet-stream"
                )
                with urlrequest.urlopen(req, timeout=120) as resp:
                    return resp.read()
            except urlerror.HTTPError as e:
                debug_body = ""
                try:
                    raw = e.read()
                    if raw:
                        debug_body = raw.decode("utf-8", "replace")[:500]
                except Exception:
                    pass
                logging.debug(
                    "Forge HTTPError %s for %s, body=%r",
                    e.code,
                    url,
                    debug_body,
                )
                msg = f"API error {e.code} for {url}"
                if e.code >= 500 and attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ForgeError(msg)
            except urlerror.URLError as e:
                logging.debug(
                    "Forge URLError for %s: %s (attempt %d)",
                    url,
                    e,
                    attempt + 1,
                )
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ForgeError(f"API request failed for {url}: {e}")

        return b""


@dataclass
class GiteaClient(ForgeClient):
    """Client for Gitea/Forgejo/Codeberg Actions API."""

    def get_auth_header(self) -> str:
        """Gitea uses 'token {token}' format."""
        return f"token {self.token}"

    def get_api_base(self, repo: str) -> str:
        """Gitea API base path."""
        return f"{self.server_url}/api/v1/repos/{repo}/actions"

    def get_raw_file_url(self, repo: str, branch: str, filepath: str) -> str:
        """Gitea raw file URL."""
        return f"{self.server_url}/api/v1/repos/{repo}/raw/{branch}/{filepath}"

    def get_pagination_params(self, limit: int, page: int) -> str:
        """Gitea uses limit instead of per_page."""
        return f"limit={limit}&page={page}"


@dataclass
class GitHubClient(ForgeClient):
    """Client for GitHub Actions API."""

    def get_auth_header(self) -> str:
        """GitHub uses 'Bearer {token}' format."""
        return f"Bearer {self.token}"

    def get_api_base(self, repo: str) -> str:
        """GitHub API base path.

        Note: GitHub API is at api.github.com, not github.com.
        For GitHub Enterprise, it's {host}/api/v3/repos/...
        """
        if self.server_url == "https://github.com":
            return f"https://api.github.com/repos/{repo}/actions"
        else:
            # GitHub Enterprise
            return f"{self.server_url}/api/v3/repos/{repo}/actions"

    def get_raw_file_url(self, repo: str, branch: str, filepath: str) -> str:
        """GitHub raw file URL (uses different domain)."""
        # Extract hostname for raw URL
        if self.server_url == "https://github.com":
            raw_base = "https://raw.githubusercontent.com"
        else:
            # GitHub Enterprise or custom
            raw_base = self.server_url.replace("https://", "https://raw.")

        return f"{raw_base}/{repo}/{branch}/{filepath}"

    def get_pagination_params(self, limit: int, page: int) -> str:
        """GitHub uses per_page instead of limit."""
        return f"per_page={limit}&page={page}"


def create_forge_client(config: Config) -> ForgeClient:
    """Factory function to create the appropriate forge client.

    Args:
        config: CLI configuration

    Returns:
        GiteaClient or GitHubClient based on configured platform
    """
    platform = _resolve_platform(config)
    token = _get_active_token(config)
    server_url = _get_active_server(config)

    if platform == GitPlatform.GITHUB:
        return GitHubClient(token=token, server_url=server_url)
    else:
        return GiteaClient(token=token, server_url=server_url)


# ============================================================================
# Helper functions for workflow operations
# ============================================================================

def _extract_total_count(response: dict) -> Optional[int]:
    """Extract total count from a workflow runs response."""
    total_count = response.get("total_count") or response.get("total") or response.get("count")
    try:
        return int(total_count) if total_count is not None else None
    except (TypeError, ValueError):
        return None


def _parse_event_inputs(run: dict) -> dict:
    """Parse inputs from a workflow run's event payload."""
    event_payload = run.get("event_payload", "")
    if not event_payload:
        return {}
    try:
        payload = json.loads(event_payload)
    except (json.JSONDecodeError, TypeError):
        return {}
    inputs = payload.get("inputs", {}) or {}
    return inputs if isinstance(inputs, dict) else {}


def _matches_inputs(inputs: dict, expected_inputs: dict) -> bool:
    """Check if inputs match expected values."""
    for key, value in expected_inputs.items():
        if not value:
            continue
        if str(inputs.get(key, "")) != str(value):
            return False
    return True


def _find_run_by_inputs(runs: list, expected_inputs: dict) -> Optional[dict]:
    """Find a workflow run matching the expected inputs."""
    for run in runs:
        inputs = _parse_event_inputs(run)
        if not inputs:
            continue
        if _matches_inputs(inputs, expected_inputs):
            return run
    return None


def _artifact_name(job_id: str, request_id: str) -> str:
    """Compute the artifact name from job_id and request_id."""
    return f"job-{job_id}-log-{request_id}"


# ============================================================================
# Public API functions (use active platform from config)
# ============================================================================

def trigger_workflow_dispatch(
    config: Config,
    workflow_file: str,
    inputs: dict,
    ref: str = "main",
) -> dict:
    """Trigger a workflow via workflow_dispatch.

    Args:
        config: CLI configuration
        workflow_file: Workflow filename (e.g., 'sync_code.yml')
        inputs: Workflow inputs
        ref: Git ref to run on (default: main)

    Returns:
        Response dict (may be empty for 204 responses)
    """
    repo = _get_active_repo(config)
    client = create_forge_client(config)
    api_base = client.get_api_base(repo)

    url = f"{api_base}/workflows/{workflow_file}/dispatches"

    data = {
        "ref": ref,
        "inputs": inputs,
    }

    try:
        response = client.request_json("POST", url, data)
        return response
    except ForgeError as e:
        raise ForgeError(f"Failed to trigger workflow: {e}")


def trigger_log_retrieval_workflow(
    config: Config,
    job_id: str,
    remote_log_path: str,
    request_id: str,
    start_offset: int = 0,
) -> None:
    """Trigger the workflow that uploads a job log as an artifact.

    Args:
        config: CLI configuration
        job_id: Inspire job ID
        remote_log_path: Absolute path to log on shared filesystem
        request_id: Unique request identifier
        start_offset: Byte offset to start reading from (default: 0 = full file)
    """
    inputs = {
        "job_id": job_id,
        "remote_log_path": remote_log_path,
        "request_id": request_id,
        "start_offset": str(start_offset),
    }
    workflow_file = _get_active_workflow_file(config, "log")
    trigger_workflow_dispatch(config, workflow_file, inputs)


def trigger_sync_workflow(
    config: Config,
    branch: str,
    commit_sha: str,
    force: bool = False,
) -> str:
    """Trigger the sync workflow.

    Returns the workflow run ID (or empty string if not available).
    """
    inputs = {
        "branch": branch,
        "commit_sha": commit_sha,
        "force": str(force).lower(),
        "target_dir": config.target_dir or "",
    }
    workflow_file = _get_active_workflow_file(config, "sync")
    trigger_workflow_dispatch(config, workflow_file, inputs)

    # Wait briefly and find the run ID
    time.sleep(2)

    repo = _get_active_repo(config)
    client = create_forge_client(config)

    # Build expected inputs for matching
    expected_inputs = {
        "branch": branch,
        "commit_sha": commit_sha,
        "force": str(force).lower(),
        "target_dir": config.target_dir or "",
    }

    limit = 20
    for _ in range(3):
        try:
            # Use platform-specific pagination
            runs_url = f"{client.get_api_base(repo)}/runs?{client.get_pagination_params(limit, 1)}"
            response = client.request_json("GET", runs_url)
            runs = response.get("workflow_runs", []) or []

            run = _find_run_by_inputs(runs, expected_inputs)
            if run:
                return str(run.get("id", ""))

            total_count = _extract_total_count(response)
            if total_count and total_count > limit:
                last_page = (total_count + limit - 1) // limit
                runs_url = f"{client.get_api_base(repo)}/runs?{client.get_pagination_params(limit, last_page)}"
                response = client.request_json("GET", runs_url)
                runs = response.get("workflow_runs", []) or []
                run = _find_run_by_inputs(runs, expected_inputs)
                if run:
                    return str(run.get("id", ""))
        except ForgeError:
            pass

        time.sleep(1)

    return ""


def trigger_bridge_action_workflow(
    config: Config,
    raw_command: str,
    artifact_paths: list[str],
    request_id: str,
    denylist: Optional[list[str]] = None,
) -> None:
    """Trigger the Bridge action workflow for arbitrary command exec."""
    denylist_str = "\n".join(denylist or [])
    artifact_paths_str = "\n".join(artifact_paths)

    inputs = {
        "raw_command": raw_command,
        "denylist": denylist_str,
        "target_dir": config.target_dir or "",
        "artifact_paths": artifact_paths_str,
        "request_id": request_id,
    }
    workflow_file = _get_active_workflow_file(config, "bridge")
    trigger_workflow_dispatch(config, workflow_file, inputs)


def get_workflow_runs(config: Config, limit: int = 20) -> list:
    """Get recent workflow runs."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)

    url = f"{client.get_api_base(repo)}/runs?{client.get_pagination_params(limit, 1)}"

    try:
        response = client.request_json("GET", url)
        return response.get("workflow_runs", []) or []
    except ForgeError as e:
        raise ForgeError(f"Failed to get workflow runs: {e}")


def get_workflow_run(config: Config, run_id: str) -> dict:
    """Get a specific workflow run."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)

    url = f"{client.get_api_base(repo)}/runs/{run_id}"

    try:
        return client.request_json("GET", url)
    except ForgeError as e:
        raise ForgeError(f"Failed to get workflow run: {e}")


def wait_for_workflow_completion(
    config: Config,
    run_id: str,
    timeout: Optional[int] = None,
) -> dict:
    """Wait for a workflow run to complete."""
    timeout_seconds = timeout or config.remote_timeout or 90
    deadline = time.time() + max(5, int(timeout_seconds))

    while True:
        if time.time() > deadline:
            raise TimeoutError(
                f"Workflow timed out after {timeout_seconds} seconds.\n"
                f"To increase the timeout, set: export INSP_REMOTE_TIMEOUT=<seconds>"
            )

        run = get_workflow_run(config, run_id)
        status = run.get("status")
        conclusion = run.get("conclusion")

        # Both Gitea and GitHub use: completed, success, failure
        if status in ("completed", "success", "failure"):
            return {
                "status": status,
                "conclusion": conclusion or status,
                "run_id": run_id,
                "html_url": run.get("html_url", ""),
            }

        time.sleep(3)


def _find_artifact_by_name(
    config: Config,
    artifact_name: str,
) -> Optional[dict]:
    """Search repository artifacts for one with the given name."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)

    url = f"{client.get_api_base(repo)}/artifacts?limit=100"
    try:
        response = client.request_json("GET", url)
        artifacts = response.get("artifacts", []) or []
        for art in artifacts:
            if art.get("name") == artifact_name and not art.get("expired", False):
                return art
    except ForgeError:
        pass
    return None


def wait_for_log_artifact(
    config: Config,
    job_id: str,
    request_id: str,
    cache_path: Path,
) -> None:
    """Poll for the log file and download it.

    Tries two methods:
    1. Artifact API (works on Gitea 1.24+ and GitHub)
    2. Raw file from 'logs' branch (works on any Git platform)
    """
    repo = _get_active_repo(config)
    client = create_forge_client(config)

    log_filename = _artifact_name(job_id, request_id)
    deadline = time.time() + max(5, int(config.remote_timeout or 90))

    while True:
        if time.time() > deadline:
            raise TimeoutError(
                f"Remote log retrieval timed out after {config.remote_timeout} seconds."
            )

        # Method 1: Try artifact API first
        artifact = _find_artifact_by_name(config, log_filename)
        if artifact is not None:
            artifact_id = artifact.get("id")
            if artifact_id:
                download_url = f"{client.get_api_base(repo)}/artifacts/{artifact_id}/zip"
                try:
                    data = client.request_bytes("GET", download_url)
                    # Extract the zip and write the contained log file to cache_path
                    with zipfile.ZipFile(BytesIO(data)) as zf:
                        members = [m for m in zf.infolist() if not m.is_dir()]
                        if members:
                            member = members[0]
                            cache_path.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(member, "r") as src, cache_path.open("wb") as dst:
                                dst.write(src.read())
                            return
                except ForgeError:
                    pass  # Fall through to try raw file method

        # Method 2: Try raw file from logs branch
        raw_url = client.get_raw_file_url(repo, "logs", f"{log_filename}.log")
        try:
            data = client.request_bytes("GET", raw_url)
            if data and len(data) > 0:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(data)
                return
        except ForgeError:
            pass  # File not ready yet, keep polling

        time.sleep(3)


def _prune_old_logs(cache_dir: Path, max_age_days: int = 7) -> None:
    """Remove log files older than max_age_days from the cache directory."""
    if not cache_dir.exists():
        return

    now = time.time()
    max_age_seconds = max_age_days * 24 * 3600

    try:
        for log_file in cache_dir.glob("*.log"):
            if not log_file.is_file():
                continue
            age_seconds = now - log_file.stat().st_mtime
            if age_seconds > max_age_seconds:
                try:
                    log_file.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def fetch_remote_log_via_bridge(
    config: Config,
    job_id: str,
    remote_log_path: str,
    cache_path: Path,
    refresh: bool = False,
) -> Path:
    """High-level helper to ensure a local cached copy of a remote log."""
    if cache_path.exists() and not refresh:
        return cache_path

    request_id = f"{int(time.time())}-{os.getpid()}"

    trigger_log_retrieval_workflow(
        config=config,
        job_id=job_id,
        remote_log_path=remote_log_path,
        request_id=request_id,
    )

    wait_for_log_artifact(
        config=config,
        job_id=job_id,
        request_id=request_id,
        cache_path=cache_path,
    )

    cache_dir = cache_path.parent
    _prune_old_logs(cache_dir, max_age_days=7)

    return cache_path


def fetch_remote_log_incremental(
    config: Config,
    job_id: str,
    remote_log_path: str,
    cache_path: Path,
    start_offset: int = 0,
) -> tuple[Path, int]:
    """Fetch incremental portion of remote log and append to cache.

    Args:
        config: CLI configuration
        job_id: Inspire job ID
        remote_log_path: Absolute path to log on shared filesystem
        cache_path: Local cache file path
        start_offset: Byte offset to start from

    Returns:
        Tuple of (cache_path, bytes_written)

    Raises:
        ForgeError: If workflow fails or artifact not found
        TimeoutError: If workflow times out
    """
    request_id = f"{int(time.time())}-{os.getpid()}"

    # Trigger workflow with offset
    trigger_log_retrieval_workflow(
        config=config,
        job_id=job_id,
        remote_log_path=remote_log_path,
        request_id=request_id,
        start_offset=start_offset,
    )

    # Download to temp file first
    temp_path = cache_path.parent / f"{job_id}.tmp.{os.getpid()}"
    try:
        wait_for_log_artifact(
            config=config,
            job_id=job_id,
            request_id=request_id,
            cache_path=temp_path,
        )

        # Get bytes written
        bytes_written = temp_path.stat().st_size if temp_path.exists() else 0

        if bytes_written > 0:
            # Append to existing cache
            if cache_path.exists() and start_offset > 0:
                with cache_path.open("ab") as dst:
                    dst.write(temp_path.read_bytes())
            else:
                # First fetch or offset=0, replace file
                temp_path.replace(cache_path)
                return cache_path, bytes_written

        return cache_path, bytes_written
    finally:
        # Cleanup temp file
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def wait_for_bridge_action_completion(
    config: Config,
    request_id: str,
    timeout: Optional[int] = None,
) -> dict:
    """Poll for bridge action workflow completion."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)
    timeout_seconds = timeout or config.bridge_action_timeout or 300
    deadline = time.time() + max(5, int(timeout_seconds))

    limit = 20

    def _find_matching_run(runs_list: list) -> Optional[dict]:
        run = _find_run_by_inputs(runs_list, {"request_id": request_id})
        if not run:
            return None
        status = run.get("status")
        conclusion = run.get("conclusion")
        logging.debug(
            "Found matching run: status=%s, conclusion=%s",
            status,
            conclusion,
        )
        # Both platforms use 'success'/'failure' as status or 'completed'
        if status in ("completed", "success", "failure"):
            return {
                "status": status,
                "conclusion": conclusion or status,
                "run_id": run.get("id"),
                "html_url": run.get("html_url", ""),
            }
        return None

    while True:
        if time.time() > deadline:
            raise TimeoutError(
                f"Bridge action timed out after {timeout_seconds} seconds."
            )

        try:
            runs_url = f"{client.get_api_base(repo)}/runs?{client.get_pagination_params(limit, 1)}"
            response = client.request_json("GET", runs_url)
            runs = response.get("workflow_runs", []) or []

            match = _find_matching_run(runs)
            if match:
                return match

            # Some forges return runs in ascending order; check last page
            total_count = _extract_total_count(response)
            if total_count and total_count > limit:
                last_page = (total_count + limit - 1) // limit
                runs_url = f"{client.get_api_base(repo)}/runs?{client.get_pagination_params(limit, last_page)}"
                response = client.request_json("GET", runs_url)
                runs = response.get("workflow_runs", []) or []
                match = _find_matching_run(runs)
                if match:
                    return match
        except ForgeError:
            pass

        time.sleep(3)


def download_bridge_artifact(
    config: Config,
    request_id: str,
    local_path: Path,
) -> None:
    """Download artifact for a bridge action run from the logs branch."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)

    artifact_name = f"bridge-action-{request_id}"
    raw_url = client.get_raw_file_url(repo, "logs", f"{artifact_name}.zip")

    try:
        data = client.request_bytes("GET", raw_url)
        if data and len(data) > 0:
            local_path.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(BytesIO(data)) as zf:
                zf.extractall(local_path)
            return
    except ForgeError:
        pass

    raise ForgeError(f"Artifact not found: {artifact_name}")


def fetch_bridge_output_log(
    config: Config,
    request_id: str,
) -> Optional[str]:
    """Fetch the output.log from a bridge action artifact on the logs branch."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)

    artifact_name = f"bridge-action-{request_id}"
    raw_url = client.get_raw_file_url(repo, "logs", f"{artifact_name}.zip")

    try:
        data = client.request_bytes("GET", raw_url)
        if data and len(data) > 0:
            with zipfile.ZipFile(BytesIO(data)) as zf:
                for member in zf.infolist():
                    if member.filename == "output.log" or member.filename.endswith(
                        "/output.log"
                    ):
                        with zf.open(member) as f:
                            return f.read().decode("utf-8", errors="replace")
    except ForgeError:
        pass

    return None
