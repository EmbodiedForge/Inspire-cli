"""Gitea helpers for remote job log retrieval and bridge operations.

This module provides a wrapper around the Gitea REST API to trigger
workflows on a Bridge runner for log retrieval, code sync, and
arbitrary command execution.

Gitea Actions API is compatible with GitHub Actions, so this module
is similar to the original GitHub implementation.
"""

from __future__ import annotations

import json
import logging
import os
import time
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from inspire.cli.utils.config import Config, ConfigError


class GiteaAuthError(ConfigError):
    """Authentication/configuration error for Gitea access."""


class GiteaError(Exception):
    """Generic Gitea API or workflow error."""


@dataclass
class _GiteaClient:
    """Small helper around the Gitea REST API."""

    token: str
    server_url: str = "https://gitea.com"

    def _build_request(
        self,
        method: str,
        url: str,
        data: Optional[dict] = None,
        accept: str = "application/json",
    ) -> urlrequest.Request:
        headers = {
            "Authorization": f"token {self.token}",
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
        """Make a JSON request to the Gitea API with retry."""
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
                msg = f"Gitea API error {e.code} for {url}"
                if detail:
                    msg += f": {detail}"

                if e.code >= 500 and attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GiteaError(msg)
            except urlerror.URLError as e:
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GiteaError(f"Gitea API request failed for {url}: {e}")

        return {}

    def request_bytes(self, method: str, url: str) -> bytes:
        """Make a binary request to the Gitea API with retry."""
        max_retries = 3
        retry_delay = 2.0

        for attempt in range(max_retries + 1):
            try:
                logging.debug(
                    "Gitea request_bytes %s %s (attempt %d)",
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
                    "Gitea HTTPError %s for %s, body=%r",
                    e.code,
                    url,
                    debug_body,
                )
                msg = f"Gitea API error {e.code} for {url}"
                if e.code >= 500 and attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GiteaError(msg)
            except urlerror.URLError as e:
                logging.debug(
                    "Gitea URLError for %s: %s (attempt %d)",
                    url,
                    e,
                    attempt + 1,
                )
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GiteaError(f"Gitea API request failed for {url}: {e}")

        return b""


def _sanitize_token(token: str) -> str:
    """Sanitize a Gitea token by removing common prefixes."""
    token = token.strip()
    lower = token.lower()
    if lower.startswith("bearer "):
        token = token[7:].strip()
    elif lower.startswith("token "):
        token = token[6:].strip()
    return token


def _resolve_gitea_token(config: Config) -> str:
    """Resolve a Gitea token from config."""
    if config.gitea_token:
        token = _sanitize_token(config.gitea_token)
        logging.debug(
            "Gitea token resolved from INSP_GITEA_TOKEN (length=%d)",
            len(token),
        )
        return token

    raise GiteaAuthError(
        "Remote operations require Gitea authentication.\n"
        "Set INSP_GITEA_TOKEN environment variable with a Personal Access Token."
    )


def _get_repo(config: Config) -> str:
    """Get the Gitea repo from config.

    Expected format: 'owner/repo'
    """
    repo = (config.gitea_repo or "").strip()
    if not repo:
        raise GiteaAuthError(
            "Remote operations require INSP_GITEA_REPO to be set.\n"
            "Use 'owner/repo' format.\n"
            "Example: export INSP_GITEA_REPO='my-org/my-repo'"
        )
    if "/" not in repo:
        raise GiteaAuthError(
            f"Invalid INSP_GITEA_REPO format '{repo}'. Expected 'owner/repo'."
        )
    return repo


def _get_server_url(config: Config) -> str:
    """Get the Gitea server URL from config."""
    return (config.gitea_server or "https://gitea.com").rstrip("/")


def _get_client(config: Config) -> _GiteaClient:
    token = _resolve_gitea_token(config)
    server_url = _get_server_url(config)
    return _GiteaClient(token=token, server_url=server_url)


def _artifact_name(job_id: str, request_id: str) -> str:
    """Compute the artifact name from job_id and request_id."""
    return f"job-{job_id}-log-{request_id}"


def trigger_workflow_dispatch(
    config: Config,
    workflow_file: str,
    inputs: dict,
    ref: str = "main",
) -> dict:
    """Trigger a Gitea Actions workflow via workflow_dispatch.

    Args:
        config: CLI configuration
        workflow_file: Workflow filename (e.g., 'sync_code.yml')
        inputs: Workflow inputs
        ref: Git ref to run on (default: main)

    Returns:
        Response dict (may be empty for 204 responses)
    """
    repo = _get_repo(config)
    client = _get_client(config)
    server_url = _get_server_url(config)

    url = f"{server_url}/api/v1/repos/{repo}/actions/workflows/{workflow_file}/dispatches"

    data = {
        "ref": ref,
        "inputs": inputs,
    }

    try:
        response = client.request_json("POST", url, data)
        return response
    except GiteaError as e:
        raise GiteaError(f"Failed to trigger workflow: {e}")


def trigger_log_retrieval_workflow(
    config: Config,
    job_id: str,
    remote_log_path: str,
    request_id: str,
) -> None:
    """Trigger the workflow that uploads a job log as an artifact."""
    inputs = {
        "job_id": job_id,
        "remote_log_path": remote_log_path,
        "request_id": request_id,
    }
    trigger_workflow_dispatch(
        config, config.gitea_log_workflow, inputs
    )


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
    trigger_workflow_dispatch(config, config.gitea_sync_workflow, inputs)

    # Wait briefly and find the run ID
    time.sleep(2)

    repo = _get_repo(config)
    client = _get_client(config)
    server_url = _get_server_url(config)

    runs_url = f"{server_url}/api/v1/repos/{repo}/actions/runs?limit=5"
    try:
        response = client.request_json("GET", runs_url)
        runs = response.get("workflow_runs", []) or []
        if runs:
            return str(runs[0].get("id", ""))
    except GiteaError:
        pass

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
    trigger_workflow_dispatch(config, config.gitea_bridge_workflow, inputs)


def get_workflow_runs(config: Config, limit: int = 20) -> list:
    """Get recent workflow runs."""
    repo = _get_repo(config)
    client = _get_client(config)
    server_url = _get_server_url(config)

    url = f"{server_url}/api/v1/repos/{repo}/actions/runs?limit={limit}"

    try:
        response = client.request_json("GET", url)
        return response.get("workflow_runs", []) or []
    except GiteaError as e:
        raise GiteaError(f"Failed to get workflow runs: {e}")


def get_workflow_run(config: Config, run_id: str) -> dict:
    """Get a specific workflow run."""
    repo = _get_repo(config)
    client = _get_client(config)
    server_url = _get_server_url(config)

    url = f"{server_url}/api/v1/repos/{repo}/actions/runs/{run_id}"

    try:
        return client.request_json("GET", url)
    except GiteaError as e:
        raise GiteaError(f"Failed to get workflow run: {e}")


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

        # Gitea uses: queued, in_progress, completed
        # Codeberg/Forgejo uses: success, failure directly as status
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
    repo = _get_repo(config)
    client = _get_client(config)
    server_url = _get_server_url(config)

    url = f"{server_url}/api/v1/repos/{repo}/actions/artifacts?limit=100"
    try:
        response = client.request_json("GET", url)
        artifacts = response.get("artifacts", []) or []
        for art in artifacts:
            if art.get("name") == artifact_name and not art.get("expired", False):
                return art
    except GiteaError:
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
    1. Artifact API (works on Gitea 1.24+)
    2. Raw file from 'logs' branch (works on any Git platform)
    """
    repo = _get_repo(config)
    client = _get_client(config)
    server_url = _get_server_url(config)

    log_filename = _artifact_name(job_id, request_id)
    deadline = time.time() + max(5, int(config.remote_timeout or 90))

    while True:
        if time.time() > deadline:
            raise TimeoutError(
                f"Remote log retrieval timed out after {config.remote_timeout} seconds."
            )

        # Method 1: Try artifact API first (Gitea 1.24+)
        artifact = _find_artifact_by_name(config, log_filename)
        if artifact is not None:
            artifact_id = artifact.get("id")
            if artifact_id:
                download_url = (
                    f"{server_url}/api/v1/repos/{repo}/actions/artifacts/{artifact_id}/zip"
                )
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
                except GiteaError:
                    pass  # Fall through to try raw file method

        # Method 2: Try raw file from logs branch (Codeberg/Forgejo)
        # The workflow pushes logs to a 'logs' branch as raw files
        raw_url = f"{server_url}/api/v1/repos/{repo}/raw/logs/{log_filename}.log"
        try:
            data = client.request_bytes("GET", raw_url)
            if data and len(data) > 0:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(data)
                return
        except GiteaError:
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


def wait_for_bridge_action_completion(
    config: Config,
    request_id: str,
    timeout: Optional[int] = None,
) -> dict:
    """Poll for bridge action workflow completion."""
    repo = _get_repo(config)
    client = _get_client(config)
    server_url = _get_server_url(config)
    timeout_seconds = timeout or config.bridge_action_timeout or 300
    deadline = time.time() + max(5, int(timeout_seconds))

    while True:
        if time.time() > deadline:
            raise TimeoutError(
                f"Bridge action timed out after {timeout_seconds} seconds."
            )

        runs_url = f"{server_url}/api/v1/repos/{repo}/actions/runs?limit=20"
        try:
            response = client.request_json("GET", runs_url)
            runs = response.get("workflow_runs", []) or []

            for run in runs:
                # Match by request_id in event_payload (Codeberg/Forgejo)
                event_payload = run.get("event_payload", "")
                if event_payload:
                    try:
                        payload = json.loads(event_payload)
                        inputs = payload.get("inputs", {})
                        if inputs.get("request_id") == request_id:
                            status = run.get("status")
                            conclusion = run.get("conclusion")
                            logging.debug(
                                "Found matching run: status=%s, conclusion=%s",
                                status,
                                conclusion,
                            )
                            # Codeberg uses 'success'/'failure' as status, not 'completed'
                            if status in ("completed", "success", "failure"):
                                return {
                                    "status": status,
                                    "conclusion": conclusion or status,
                                    "run_id": run.get("id"),
                                    "html_url": run.get("html_url", ""),
                                }
                    except (json.JSONDecodeError, TypeError):
                        pass
        except GiteaError:
            pass

        time.sleep(3)


def download_bridge_artifact(
    config: Config,
    request_id: str,
    local_path: Path,
) -> None:
    """Download artifact for a bridge action run from the logs branch."""
    repo = _get_repo(config)
    client = _get_client(config)
    server_url = _get_server_url(config)

    artifact_name = f"bridge-action-{request_id}"
    raw_url = f"{server_url}/api/v1/repos/{repo}/raw/logs/{artifact_name}.zip"

    try:
        data = client.request_bytes("GET", raw_url)
        if data and len(data) > 0:
            local_path.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(BytesIO(data)) as zf:
                zf.extractall(local_path)
            return
    except GiteaError:
        pass

    raise GiteaError(f"Artifact not found: {artifact_name}")


def fetch_bridge_output_log(
    config: Config,
    request_id: str,
) -> Optional[str]:
    """Fetch the output.log from a bridge action artifact on the logs branch."""
    repo = _get_repo(config)
    client = _get_client(config)
    server_url = _get_server_url(config)

    artifact_name = f"bridge-action-{request_id}"
    raw_url = f"{server_url}/api/v1/repos/{repo}/raw/logs/{artifact_name}.zip"

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
    except GiteaError:
        pass

    return None
