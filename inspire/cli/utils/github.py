"""GitHub helpers for remote job log retrieval.

This module provides a minimal wrapper around the GitHub REST API to
trigger a workflow on a Bridge runner that copies a remote training log
into an artifact, then downloads that artifact to the local machine.

It is intentionally conservative: it uses only the Python standard
library and surfaces clear exceptions that callers can translate into
CLI-friendly messages.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest
import zipfile

from inspire.cli.utils.config import Config, ConfigError


class GitHubAuthError(ConfigError):
    """Authentication/configuration error for GitHub access."""


class GitHubError(Exception):
    """Generic GitHub API or workflow error."""


@dataclass
class _GitHubClient:
    """Small helper around the GitHub REST API."""

    token: str

    def _build_request(self, method: str, url: str, data: Optional[dict] = None) -> urlrequest.Request:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "inspire-cli-job-logs",
        }
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            body = None

        req = urlrequest.Request(url, data=body, headers=headers)
        req.get_method = lambda: method  # type: ignore[assignment]
        return req

    def request_json(self, method: str, url: str, data: Optional[dict] = None) -> dict:
        """Make a JSON request to the GitHub API with retry for transient errors.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: GitHub API URL
            data: Optional JSON payload

        Returns:
            Parsed JSON response

        Raises:
            GitHubError: On HTTP errors or network failures
        """
        max_retries = 2
        retry_delay = 1.0

        for attempt in range(max_retries + 1):
            try:
                req = self._build_request(method, url, data)
                with urlrequest.urlopen(req, timeout=30) as resp:
                    charset = resp.headers.get_content_charset("utf-8")
                    payload = resp.read().decode(charset)
                    if not payload:
                        return {}
                    return json.loads(payload)
            except urlerror.HTTPError as e:
                # Try to parse any JSON error details
                detail = None
                try:
                    raw = e.read().decode("utf-8")
                    detail = json.loads(raw).get("message")
                except Exception:
                    pass
                msg = f"GitHub API error {e.code} for {url}"
                if detail:
                    msg += f": {detail}"

                # Retry on 5xx errors; fail immediately on 4xx
                if e.code >= 500 and attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GitHubError(msg)
            except urlerror.URLError as e:
                # Retry transient network errors
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GitHubError(f"GitHub API request failed for {url}: {e}")

    def request_bytes(self, method: str, url: str) -> bytes:
        """Make a binary request to the GitHub API with retry for transient errors.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: GitHub API URL

        Returns:
            Raw bytes response

        Raises:
            GitHubError: On HTTP errors or network failures
        """
        max_retries = 2
        retry_delay = 1.0

        for attempt in range(max_retries + 1):
            try:
                logging.debug(
                    "GitHub request_bytes %s %s (attempt %d)",
                    method,
                    url,
                    attempt + 1,
                )
                req = self._build_request(method, url, data=None)
                with urlrequest.urlopen(req, timeout=60) as resp:
                    return resp.read()
            except urlerror.HTTPError as e:
                # Capture extra details for debugging without leaking secrets.
                debug_body = ""
                try:
                    raw = e.read()
                    if raw:
                        debug_body = raw.decode("utf-8", "replace")[:500]
                except Exception:
                    pass
                logging.debug(
                    "GitHub HTTPError %s for %s (final_url=%s), body=%r",
                    e.code,
                    url,
                    getattr(e, "url", None),
                    debug_body,
                )
                msg = f"GitHub API error {e.code} for {url}"
                if e.code >= 500 and attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GitHubError(msg)
            except urlerror.URLError as e:
                logging.debug(
                    "GitHub URLError for %s: %s (attempt %d)",
                    url,
                    e,
                    attempt + 1,
                )
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GitHubError(f"GitHub API request failed for {url}: {e}")


def _resolve_github_token(config: Config) -> str:
    """Resolve a GitHub token using PAT or `gh` CLI.

    Priority order:
    1. INSP_GITHUB_TOKEN (via Config.github_token)
    2. `gh auth token` (if available and succeeds)

    This is required for remote log retrieval from laptops.
    """

    if config.github_token:
        token = config.github_token
        logging.debug(
            "GitHub token resolved from INSP_GITHUB_TOKEN (length=%d)",
            len(token),
        )
        return token

    # Try gh CLI as a fallback
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        token = proc.stdout.strip()
        if token:
            logging.debug(
                "GitHub token resolved from `gh auth token` (length=%d)",
                len(token),
            )
            return token
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logging.debug("Failed to resolve GitHub token via gh CLI: %s", e)

    raise GitHubAuthError(
        "Remote log retrieval requires GitHub authentication.\n"
        "Set INSP_GITHUB_TOKEN environment variable with a Personal Access Token, or\n"
        "configure the `gh` CLI with: gh auth login"
    )


def _get_repo(config: Config) -> str:
    """Get the GitHub repo from config, validating format.

    Expected format: 'owner/repo'
    This is required for remote log retrieval from laptops.
    """
    repo = (config.github_repo or "").strip()
    if not repo:
        raise GitHubAuthError(
            "Remote log retrieval requires INSP_GITHUB_REPO to be set to 'owner/repo' format.\n"
            "Example: export INSP_GITHUB_REPO='my-org/inspire-training'"
        )
    if "/" not in repo:
        raise GitHubAuthError(
            f"Invalid INSP_GITHUB_REPO format '{repo}'. Expected 'owner/repo'."
        )
    return repo


def _get_client(config: Config) -> _GitHubClient:
    token = _resolve_github_token(config)
    return _GitHubClient(token=token)


def _artifact_name(job_id: str, request_id: str) -> str:
    """Compute the artifact name from job_id and request_id.

    This name must match the GitHub Actions workflow output naming convention.
    Used by both trigger_log_retrieval_workflow and wait_for_log_artifact to
    ensure consistency.
    """
    return f"job-{job_id}-log-{request_id}"


def trigger_log_retrieval_workflow(
    config: Config,
    job_id: str,
    remote_log_path: str,
    request_id: str,
) -> None:
    """Trigger the Bridge workflow that uploads a job log as an artifact.

    This uses the workflow filename from config.github_workflow and
    dispatches a workflow_dispatch event with job inputs.
    """

    repo = _get_repo(config)
    client = _get_client(config)

    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{config.github_workflow}/dispatches"
    )
    data = {
        "ref": "main",  # workflows run from main in this repo
        "inputs": {
            "job_id": job_id,
            "remote_log_path": remote_log_path,
            "request_id": request_id,
        },
    }

    # GitHub returns 204 No Content on success; we don't need the body.
    try:
        client.request_json("POST", url, data=data)
    except GitHubError as e:
        raise GitHubError(f"Failed to trigger log retrieval workflow: {e}")


def _find_artifact_by_name(
    client: _GitHubClient,
    repo: str,
    artifact_name: str,
) -> Optional[dict]:
    """Search repository artifacts for one with the given name.

    We use the general artifacts list API instead of tying to a
    particular run ID to keep the logic simple and robust. The
    `request_id` embedded in `artifact_name` makes collisions unlikely.
    """

    # We keep this simple: just scan the first page.
    url = f"https://api.github.com/repos/{repo}/actions/artifacts?per_page=100"
    payload = client.request_json("GET", url)
    artifacts = payload.get("artifacts", []) or []
    for art in artifacts:
        if art.get("name") == artifact_name and not art.get("expired", False):
            return art
    return None


def wait_for_log_artifact(
    config: Config,
    job_id: str,
    request_id: str,
    cache_path: Path,
) -> None:
    """Poll GitHub for the log artifact and download it.

    This waits up to `config.remote_timeout` seconds for an artifact
    named `job-<job_id>-log-<request_id>` to appear, then downloads and
    extracts it into `cache_path`, overwriting any existing file.
    """

    repo = _get_repo(config)
    client = _get_client(config)

    artifact_name = _artifact_name(job_id, request_id)
    deadline = time.time() + max(5, int(config.remote_timeout or 90))

    while True:
        if time.time() > deadline:
            raise TimeoutError(
                f"Remote log retrieval timed out after {config.remote_timeout} seconds. "
                f"The Bridge runner may be busy or the workflow did not complete.\n"
                f"To increase the timeout, set: export INSP_REMOTE_TIMEOUT=<seconds>"
            )

        artifact = _find_artifact_by_name(client, repo, artifact_name)
        if artifact is not None:
            artifact_id = artifact.get("id")
            if not artifact_id:
                raise GitHubError(
                    f"Found artifact {artifact_name} but it has no id field."
                )

            # Prefer using the `gh` CLI to download the artifact zip, since we
            # already depend on it for authentication and it handles any
            # GitHub-specific nuances around redirects. Fall back to direct
            # HTTP if `gh` is not available or fails.
            data: Optional[bytes] = None

            # Attempt download via `gh api` when we are *not* using an explicit
            # INSP_GITHUB_TOKEN. This keeps pure-PAT setups working even on
            # machines without `gh` installed.
            if config.github_token is None:
                try:
                    logging.debug(
                        "Attempting artifact download via gh api for %s (id=%s)",
                        artifact_name,
                        artifact_id,
                    )
                    proc = subprocess.run(
                        [
                            "gh",
                            "api",
                            f"repos/{repo}/actions/artifacts/{artifact_id}/zip",
                            "--method",
                            "GET",
                        ],
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    data = proc.stdout
                    logging.debug(
                        "gh api artifact download succeeded (bytes=%d)",
                        len(data),
                    )
                except (FileNotFoundError, subprocess.CalledProcessError) as e:
                    logging.debug(
                        "gh api artifact download failed, falling back to direct HTTP: %s",
                        e,
                    )

            if data is None:
                download_url = (
                    f"https://api.github.com/repos/{repo}/actions/artifacts/"
                    f"{artifact_id}/zip"
                )
                data = client.request_bytes("GET", download_url)

            # Extract the zip and write the contained log file to cache_path.
            with zipfile.ZipFile(BytesIO(data)) as zf:
                members = [m for m in zf.infolist() if not m.is_dir()]
                if not members:
                    raise GitHubError(
                        f"Artifact {artifact_name} does not contain any files."
                    )
                # Use the first file in the archive as the log.
                member = members[0]
                # Ensure parent dir exists
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as src, cache_path.open(
                    "wb"
                ) as dst:
                    dst.write(src.read())
            return

        # Not found yet; sleep briefly and retry.
        time.sleep(3)


def _prune_old_logs(cache_dir: Path, max_age_days: int = 7) -> None:
    """Remove log files older than max_age_days from the cache directory.

    This prevents unbounded growth of the log cache on laptop machines.

    Args:
        cache_dir: Directory containing cached log files
        max_age_days: Maximum age in days before pruning (default: 7)
    """
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
                    # Silent failure if we can't delete (permissions, etc.)
                    pass
    except OSError:
        # Silent failure if we can't list the directory
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


def trigger_bridge_action_workflow(
    config: Config,
    raw_command: str,
    artifact_paths: list[str],
    request_id: str,
    denylist: Optional[list[str]] = None,
) -> None:
    """Trigger the Bridge action workflow for arbitrary command exec."""

    repo = _get_repo(config)
    client = _get_client(config)

    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{config.bridge_action_workflow}/dispatches"
    )

    denylist_str = "\n".join(denylist or [])
    artifact_paths_str = "\n".join(artifact_paths)

    data = {
        "ref": "main",
        "inputs": {
            "raw_command": raw_command,
            "denylist": denylist_str,
            "target_dir": config.target_dir or "",
            "artifact_paths": artifact_paths_str,
            "request_id": request_id,
        },
    }

    try:
        client.request_json("POST", url, data=data)
    except GitHubError as e:
        raise GitHubError(f"Failed to trigger bridge action workflow: {e}")


def wait_for_bridge_action_completion(
    config: Config,
    request_id: str,
    timeout: Optional[int] = None,
) -> dict:
    """Poll for bridge action workflow completion."""

    repo = _get_repo(config)
    client = _get_client(config)
    timeout_seconds = timeout or config.bridge_action_timeout or 300
    deadline = time.time() + max(5, int(timeout_seconds))

    runs_url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{config.bridge_action_workflow}/runs?per_page=20"
    )
    run_name = f"Bridge action {request_id}"

    while True:
        if time.time() > deadline:
            raise TimeoutError(
                f"Bridge action timed out after {timeout_seconds} seconds. "
                "Check GitHub Actions for status."
            )

        payload = client.request_json("GET", runs_url)
        runs = payload.get("workflow_runs", []) or []

        for run in runs:
            if run.get("name") != run_name:
                continue
            status = run.get("status")
            conclusion = run.get("conclusion")
            if status == "completed":
                return {
                    "status": status,
                    "conclusion": conclusion,
                    "run_id": run.get("id"),
                    "html_url": run.get("html_url", ""),
                }
        time.sleep(3)


def download_bridge_artifact(
    config: Config,
    request_id: str,
    local_path: Path,
) -> None:
    """Download artifact for a bridge action run."""

    repo = _get_repo(config)
    client = _get_client(config)

    artifact_name = f"bridge-action-{request_id}"
    artifact = _find_artifact_by_name(client, repo, artifact_name)
    if artifact is None:
        raise GitHubError(f"Artifact not found: {artifact_name}")

    artifact_id = artifact.get("id")
    if not artifact_id:
        raise GitHubError(f"Artifact {artifact_name} missing id field")

    data: Optional[bytes] = None

    if config.github_token is None:
        try:
            proc = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/actions/artifacts/{artifact_id}/zip",
                    "--method",
                    "GET",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            data = proc.stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

    if data is None:
        download_url = (
            f"https://api.github.com/repos/{repo}/actions/artifacts/"
            f"{artifact_id}/zip"
        )
        data = client.request_bytes("GET", download_url)

    local_path.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BytesIO(data)) as zf:
        zf.extractall(local_path)


def fetch_bridge_output_log(
    config: Config,
    request_id: str,
) -> Optional[str]:
    """Fetch the output.log from a bridge action artifact.

    Returns the log content as a string, or None if not found.
    """
    repo = _get_repo(config)
    client = _get_client(config)

    artifact_name = f"bridge-action-{request_id}"
    artifact = _find_artifact_by_name(client, repo, artifact_name)
    if artifact is None:
        return None

    artifact_id = artifact.get("id")
    if not artifact_id:
        return None

    data: Optional[bytes] = None

    if config.github_token is None:
        try:
            proc = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/actions/artifacts/{artifact_id}/zip",
                    "--method",
                    "GET",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            data = proc.stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

    if data is None:
        download_url = (
            f"https://api.github.com/repos/{repo}/actions/artifacts/"
            f"{artifact_id}/zip"
        )
        data = client.request_bytes("GET", download_url)

    # Extract output.log from the zip
    with zipfile.ZipFile(BytesIO(data)) as zf:
        for member in zf.infolist():
            if member.filename == "output.log" or member.filename.endswith("/output.log"):
                with zf.open(member) as f:
                    return f.read().decode("utf-8", errors="replace")

    return None
