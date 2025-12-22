"""GitLab helpers for remote job log retrieval and bridge operations.

This module provides a wrapper around the GitLab REST API to trigger
pipelines on a Bridge runner for log retrieval, code sync, and
arbitrary command execution.

It uses only the Python standard library and surfaces clear exceptions
that callers can translate into CLI-friendly messages.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from inspire.cli.utils.config import Config, ConfigError


class GitLabAuthError(ConfigError):
    """Authentication/configuration error for GitLab access."""


class GitLabError(Exception):
    """Generic GitLab API or pipeline error."""


@dataclass
class _GitLabClient:
    """Small helper around the GitLab REST API."""

    token: str
    server_url: str = "https://gitlab.com"

    def _build_request(
        self, method: str, url: str, data: Optional[dict] = None
    ) -> urlrequest.Request:
        headers = {
            "PRIVATE-TOKEN": self.token,
            "Accept": "application/json",
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

    def _build_form_request(
        self, method: str, url: str, form_data: dict
    ) -> urlrequest.Request:
        """Build a form-urlencoded request (for pipeline triggers)."""
        headers = {
            "PRIVATE-TOKEN": self.token,
            "Accept": "application/json",
            "User-Agent": "inspire-cli",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        body = urlparse.urlencode(form_data, doseq=True).encode("utf-8")
        req = urlrequest.Request(url, data=body, headers=headers)
        req.get_method = lambda: method  # type: ignore[assignment]
        return req

    def request_json(
        self, method: str, url: str, data: Optional[dict] = None
    ) -> dict:
        """Make a JSON request to the GitLab API with retry for transient errors."""
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
                detail = None
                try:
                    raw = e.read().decode("utf-8")
                    parsed = json.loads(raw)
                    detail = parsed.get("message") or parsed.get("error")
                except Exception:
                    pass
                msg = f"GitLab API error {e.code} for {url}"
                if detail:
                    msg += f": {detail}"

                if e.code >= 500 and attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GitLabError(msg)
            except urlerror.URLError as e:
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GitLabError(f"GitLab API request failed for {url}: {e}")

        return {}

    def request_form(self, method: str, url: str, form_data: dict) -> dict:
        """Make a form-urlencoded request to the GitLab API."""
        max_retries = 2
        retry_delay = 1.0

        for attempt in range(max_retries + 1):
            try:
                req = self._build_form_request(method, url, form_data)
                with urlrequest.urlopen(req, timeout=30) as resp:
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
                msg = f"GitLab API error {e.code} for {url}"
                if detail:
                    msg += f": {detail}"

                if e.code >= 500 and attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GitLabError(msg)
            except urlerror.URLError as e:
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GitLabError(f"GitLab API request failed for {url}: {e}")

        return {}

    def request_bytes(self, method: str, url: str) -> bytes:
        """Make a binary request to the GitLab API with retry."""
        max_retries = 2
        retry_delay = 1.0

        for attempt in range(max_retries + 1):
            try:
                logging.debug(
                    "GitLab request_bytes %s %s (attempt %d)",
                    method,
                    url,
                    attempt + 1,
                )
                req = self._build_request(method, url, data=None)
                with urlrequest.urlopen(req, timeout=60) as resp:
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
                    "GitLab HTTPError %s for %s, body=%r",
                    e.code,
                    url,
                    debug_body,
                )
                msg = f"GitLab API error {e.code} for {url}"
                if e.code >= 500 and attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GitLabError(msg)
            except urlerror.URLError as e:
                logging.debug(
                    "GitLab URLError for %s: %s (attempt %d)",
                    url,
                    e,
                    attempt + 1,
                )
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise GitLabError(f"GitLab API request failed for {url}: {e}")

        return b""


def _sanitize_token(token: str) -> str:
    """Sanitize a GitLab token by removing common prefixes."""
    token = token.strip()
    lower = token.lower()
    if lower.startswith("bearer "):
        token = token[7:].strip()
    elif lower.startswith("private-token "):
        token = token[14:].strip()
    return token


def _resolve_gitlab_token(config: Config) -> str:
    """Resolve a GitLab token from config or glab CLI.

    Priority order:
    1. INSP_GITLAB_TOKEN (via Config.gitlab_token)
    2. `glab auth status -t` (if available and succeeds)
    """
    if config.gitlab_token:
        token = _sanitize_token(config.gitlab_token)
        logging.debug(
            "GitLab token resolved from INSP_GITLAB_TOKEN (length=%d)",
            len(token),
        )
        return token

    # Try glab CLI as a fallback
    try:
        proc = subprocess.run(
            ["glab", "auth", "status", "-t"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # glab outputs token to stderr with format: "Token: glpat-xxx"
        for line in proc.stderr.split("\n"):
            if "Token:" in line:
                token = line.split("Token:")[-1].strip()
                token = _sanitize_token(token)
                if token:
                    logging.debug(
                        "GitLab token resolved from `glab auth status` (length=%d)",
                        len(token),
                    )
                    return token
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logging.debug("Failed to resolve GitLab token via glab CLI: %s", e)

    raise GitLabAuthError(
        "Remote operations require GitLab authentication.\n"
        "Set INSP_GITLAB_TOKEN environment variable with a Personal Access Token, or\n"
        "configure the `glab` CLI with: glab auth login"
    )


def _get_project_id(config: Config) -> str:
    """Get the GitLab project ID from config.

    Expected format: 'namespace/project' or numeric project ID
    """
    project = (config.gitlab_project or "").strip()
    if not project:
        raise GitLabAuthError(
            "Remote operations require INSP_GITLAB_PROJECT to be set.\n"
            "Use 'namespace/project' format or numeric project ID.\n"
            "Example: export INSP_GITLAB_PROJECT='my-group/inspire-training'"
        )
    # URL-encode the project path for API calls
    return urlparse.quote(project, safe="")


def _get_server_url(config: Config) -> str:
    """Get the GitLab server URL from config."""
    return config.gitlab_server or "https://gitlab.com"


def _get_client(config: Config) -> _GitLabClient:
    token = _resolve_gitlab_token(config)
    server_url = _get_server_url(config)
    return _GitLabClient(token=token, server_url=server_url)


def _artifact_name(job_id: str, request_id: str) -> str:
    """Compute the artifact name from job_id and request_id."""
    return f"job-{job_id}-log-{request_id}"


def trigger_pipeline(
    config: Config,
    job_type: str,
    variables: dict,
    ref: str = "main",
) -> dict:
    """Trigger a GitLab pipeline with variables.

    Args:
        config: CLI configuration
        job_type: Type of job (sync, retrieve_log, bridge_action)
        variables: Pipeline variables to set
        ref: Git ref to run pipeline on (default: main)

    Returns:
        Pipeline response dict with 'id', 'web_url', etc.
    """
    project_id = _get_project_id(config)
    client = _get_client(config)
    server_url = _get_server_url(config)

    url = f"{server_url}/api/v4/projects/{project_id}/pipeline"

    # Build form data with variables
    form_data: dict = {
        "ref": ref,
    }

    # Add JOB_TYPE variable
    all_vars = {"JOB_TYPE": job_type, **variables}

    # GitLab expects variables as variables[KEY]=VALUE
    for key, value in all_vars.items():
        form_data[f"variables[{key}]"] = str(value)

    try:
        response = client.request_form("POST", url, form_data)
        return response
    except GitLabError as e:
        raise GitLabError(f"Failed to trigger pipeline: {e}")


def trigger_log_retrieval_pipeline(
    config: Config,
    job_id: str,
    remote_log_path: str,
    request_id: str,
) -> dict:
    """Trigger the pipeline that uploads a job log as an artifact."""
    variables = {
        "LOG_JOB_ID": job_id,
        "LOG_REMOTE_PATH": remote_log_path,
        "LOG_REQUEST_ID": request_id,
    }
    return trigger_pipeline(config, "retrieve_log", variables)


def trigger_sync_pipeline(
    config: Config,
    branch: str,
    commit_sha: str,
    force: bool = False,
) -> dict:
    """Trigger the sync pipeline."""
    variables = {
        "SYNC_BRANCH": branch,
        "SYNC_COMMIT_SHA": commit_sha,
        "SYNC_FORCE": str(force).lower(),
        "SYNC_TARGET_DIR": config.target_dir or "",
    }
    return trigger_pipeline(config, "sync", variables)


def trigger_bridge_action_pipeline(
    config: Config,
    raw_command: str,
    artifact_paths: list[str],
    request_id: str,
    denylist: Optional[list[str]] = None,
) -> dict:
    """Trigger the Bridge action pipeline for arbitrary command exec."""
    denylist_str = "\n".join(denylist or [])
    artifact_paths_str = "\n".join(artifact_paths)

    variables = {
        "BRIDGE_RAW_COMMAND": raw_command,
        "BRIDGE_DENYLIST": denylist_str,
        "BRIDGE_TARGET_DIR": config.target_dir or "",
        "BRIDGE_ARTIFACT_PATHS": artifact_paths_str,
        "BRIDGE_REQUEST_ID": request_id,
    }
    return trigger_pipeline(config, "bridge_action", variables)


def get_pipeline_status(config: Config, pipeline_id: int) -> dict:
    """Get the status of a pipeline."""
    project_id = _get_project_id(config)
    client = _get_client(config)
    server_url = _get_server_url(config)

    url = f"{server_url}/api/v4/projects/{project_id}/pipelines/{pipeline_id}"

    try:
        return client.request_json("GET", url)
    except GitLabError as e:
        raise GitLabError(f"Failed to get pipeline status: {e}")


def wait_for_pipeline_completion(
    config: Config,
    pipeline_id: int,
    timeout: Optional[int] = None,
) -> dict:
    """Wait for a pipeline to complete.

    Returns:
        Dict with 'status', 'web_url', etc.
    """
    timeout_seconds = timeout or config.remote_timeout or 90
    deadline = time.time() + max(5, int(timeout_seconds))

    while True:
        if time.time() > deadline:
            raise TimeoutError(
                f"Pipeline timed out after {timeout_seconds} seconds.\n"
                f"To increase the timeout, set: export INSP_REMOTE_TIMEOUT=<seconds>"
            )

        pipeline = get_pipeline_status(config, pipeline_id)
        status = pipeline.get("status")

        # GitLab pipeline statuses: created, waiting_for_resource, preparing,
        # pending, running, success, failed, canceled, skipped, manual, scheduled
        if status in ("success", "failed", "canceled", "skipped"):
            return {
                "status": status,
                "conclusion": "success" if status == "success" else status,
                "pipeline_id": pipeline_id,
                "web_url": pipeline.get("web_url", ""),
            }

        time.sleep(3)


def get_pipeline_jobs(config: Config, pipeline_id: int) -> list:
    """Get jobs for a pipeline."""
    project_id = _get_project_id(config)
    client = _get_client(config)
    server_url = _get_server_url(config)

    url = f"{server_url}/api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs"

    try:
        return client.request_json("GET", url)
    except GitLabError as e:
        raise GitLabError(f"Failed to get pipeline jobs: {e}")


def download_job_artifacts(
    config: Config,
    job_id: int,
    local_path: Path,
) -> None:
    """Download artifacts from a GitLab job."""
    project_id = _get_project_id(config)
    client = _get_client(config)
    server_url = _get_server_url(config)

    url = f"{server_url}/api/v4/projects/{project_id}/jobs/{job_id}/artifacts"

    try:
        data = client.request_bytes("GET", url)
    except GitLabError as e:
        raise GitLabError(f"Failed to download artifacts: {e}")

    local_path.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BytesIO(data)) as zf:
        zf.extractall(local_path)


def _find_job_by_name(
    config: Config,
    pipeline_id: int,
    job_name: str,
) -> Optional[dict]:
    """Find a job by name in a pipeline."""
    jobs = get_pipeline_jobs(config, pipeline_id)
    for job in jobs:
        if job.get("name") == job_name:
            return job
    return None


def wait_for_log_artifact(
    config: Config,
    job_id: str,
    request_id: str,
    cache_path: Path,
    pipeline_id: int,
) -> None:
    """Wait for the log retrieval job to complete and download the artifact."""
    timeout_seconds = config.remote_timeout or 90
    deadline = time.time() + max(5, int(timeout_seconds))

    while True:
        if time.time() > deadline:
            raise TimeoutError(
                f"Remote log retrieval timed out after {timeout_seconds} seconds."
            )

        # Check pipeline status
        pipeline = get_pipeline_status(config, pipeline_id)
        status = pipeline.get("status")

        if status == "success":
            # Find the retrieve_job_log job
            job = _find_job_by_name(config, pipeline_id, "retrieve_job_log")
            if job and job.get("id"):
                gitlab_job_id = job["id"]

                # Download artifacts
                project_id = _get_project_id(config)
                client = _get_client(config)
                server_url = _get_server_url(config)

                url = f"{server_url}/api/v4/projects/{project_id}/jobs/{gitlab_job_id}/artifacts"

                try:
                    data = client.request_bytes("GET", url)
                except GitLabError as e:
                    raise GitLabError(f"Failed to download log artifact: {e}")

                # Extract the log file
                with zipfile.ZipFile(BytesIO(data)) as zf:
                    members = [m for m in zf.infolist() if not m.is_dir()]
                    log_members = [
                        m for m in members if m.filename.endswith(".log")
                    ]
                    if not log_members:
                        raise GitLabError("Artifact does not contain any log files.")

                    member = log_members[0]
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member, "r") as src, cache_path.open("wb") as dst:
                        dst.write(src.read())
                return
            else:
                raise GitLabError("Could not find retrieve_job_log job in pipeline.")

        elif status in ("failed", "canceled", "skipped"):
            raise GitLabError(f"Pipeline {status}. Check GitLab for details.")

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

    pipeline_response = trigger_log_retrieval_pipeline(
        config=config,
        job_id=job_id,
        remote_log_path=remote_log_path,
        request_id=request_id,
    )

    pipeline_id = pipeline_response.get("id")
    if not pipeline_id:
        raise GitLabError("Failed to get pipeline ID from trigger response.")

    wait_for_log_artifact(
        config=config,
        job_id=job_id,
        request_id=request_id,
        cache_path=cache_path,
        pipeline_id=pipeline_id,
    )

    cache_dir = cache_path.parent
    _prune_old_logs(cache_dir, max_age_days=7)

    return cache_path


def wait_for_bridge_action_completion(
    config: Config,
    pipeline_id: int,
    timeout: Optional[int] = None,
) -> dict:
    """Wait for bridge action pipeline completion."""
    timeout_seconds = timeout or config.bridge_action_timeout or 300
    return wait_for_pipeline_completion(config, pipeline_id, timeout_seconds)


def download_bridge_artifact(
    config: Config,
    pipeline_id: int,
    local_path: Path,
) -> None:
    """Download artifact from a bridge action pipeline."""
    job = _find_job_by_name(config, pipeline_id, "bridge_action_exec")
    if not job or not job.get("id"):
        raise GitLabError("Could not find bridge_action_exec job in pipeline.")

    download_job_artifacts(config, job["id"], local_path)


def fetch_bridge_output_log(
    config: Config,
    pipeline_id: int,
) -> Optional[str]:
    """Fetch the output.log from a bridge action artifact."""
    job = _find_job_by_name(config, pipeline_id, "bridge_action_exec")
    if not job or not job.get("id"):
        return None

    project_id = _get_project_id(config)
    client = _get_client(config)
    server_url = _get_server_url(config)

    url = f"{server_url}/api/v4/projects/{project_id}/jobs/{job['id']}/artifacts"

    try:
        data = client.request_bytes("GET", url)
    except GitLabError:
        return None

    with zipfile.ZipFile(BytesIO(data)) as zf:
        for member in zf.infolist():
            if member.filename == "output.log" or member.filename.endswith(
                "/output.log"
            ):
                with zf.open(member) as f:
                    return f.read().decode("utf-8", errors="replace")

    return None
