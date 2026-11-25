"""Forge artifact operations (download bridge action outputs)."""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional

from inspire.config import Config

from .clients import create_forge_client
from .config import _get_active_repo
from .models import ForgeError


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
