"""Forge artifact operations (download logs / bridge outputs)."""

from __future__ import annotations

import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional

from inspire.config import Config

from .clients import create_forge_client
from .config import _get_active_repo
from .helpers import _artifact_name
from .models import ForgeError


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
                    if member.filename == "output.log" or member.filename.endswith("/output.log"):
                        with zf.open(member) as f:
                            return f.read().decode("utf-8", errors="replace")
    except ForgeError:
        pass

    return None
