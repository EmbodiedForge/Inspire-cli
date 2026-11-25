"""Subprocess-shaped adapter for notebook Jupyter execution and transfer."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from inspire.cli.commands.notebook.notebook_download import download_from_notebook
from inspire.platform.web.browser_api.notebook_shell import execute_notebook_shell_command
from inspire.platform.web.browser_api.notebook_transfer import (
    upload_bytes_to_notebook_contents,
    upload_path_to_notebook,
)
from inspire.platform.web.session import WebSession


class JupyterBridgeClient:
    """Adapt Jupyter APIs to the runner contracts used by Bridge sync."""

    def __init__(self, *, notebook_id: str, session: WebSession) -> None:
        self.notebook_id = notebook_id
        self.session = session

    def run_command(
        self,
        command: str,
        bridge_name: str | None = None,
        config: Any = None,
        timeout: int | None = None,
        capture_output: bool = True,
        check: bool = False,
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        del bridge_name, config
        try:
            result = execute_notebook_shell_command(
                notebook_id=self.notebook_id,
                command=command,
                session=self.session,
                timeout=timeout or 60,
            )
            completed = subprocess.CompletedProcess(
                args=command,
                returncode=result.returncode,
                stdout=result.stdout if capture_output else "",
                # CompletedProcess is typed str on this seam and its callers strip it, so the merged-PTY
                # "no stderr channel" (None) collapses to "" here; the honest shape lives in the JSON verbs.
                stderr=(result.stderr or "") if capture_output else "",
            )
        except Exception as error:  # noqa: BLE001 - subprocess-shaped error boundary
            completed = subprocess.CompletedProcess(
                args=command,
                returncode=255,
                stdout="",
                stderr=str(error),
            )
        if check and completed.returncode != 0:
            raise subprocess.CalledProcessError(
                completed.returncode,
                command,
                output=completed.stdout,
                stderr=completed.stderr,
            )
        return completed

    def transfer(
        self,
        local_path: str,
        remote_path: str,
        *,
        download: bool = False,
        recursive: bool = False,
        bridge_name: str | None = None,
        config: Any = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del recursive, bridge_name, config
        try:
            if download:
                download_result = download_from_notebook(
                    notebook_id=self.notebook_id,
                    session=self.session,
                    remote_path=remote_path,
                    local_path=local_path,
                    timeout=timeout or 600,
                )
                stdout = f"Downloaded {download_result.bytes} bytes to {download_result.local_path}"
            else:
                upload_result = upload_path_to_notebook(
                    notebook_id=self.notebook_id,
                    session=self.session,
                    local_path=Path(local_path),
                    remote_path=remote_path,
                    timeout=timeout or 600,
                )
                stdout = f"Uploaded {upload_result.bytes} bytes to {upload_result.remote_path}"
            return subprocess.CompletedProcess(
                args=[local_path, remote_path],
                returncode=0,
                stdout=stdout,
                stderr="",
            )
        except Exception as error:  # noqa: BLE001 - subprocess-shaped error boundary
            return subprocess.CompletedProcess(
                args=[local_path, remote_path],
                returncode=1,
                stdout="",
                stderr=str(error),
            )

    def upload_raw(
        self,
        *,
        local_path: str,
        remote_path: str,
        download: bool = False,
        bridge_name: str | None = None,
        config: Any = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Drop a local file on the notebook byte-for-byte, with nothing installed or unpacked.

        This is the staging half of the tar engine: the archive lands where the Contents API puts
        it — under the notebook server's root, not the container's `/tmp` — so the remote script
        has to look in both places. Downloading stays unavailable because this seam never reads
        the notebook filesystem back.
        """
        del bridge_name, config
        if download:
            return subprocess.CompletedProcess(
                args=[local_path, remote_path],
                returncode=1,
                stdout="",
                stderr="The Jupyter Contents staging seam uploads only; it cannot download.",
            )
        rel_path = str(remote_path).lstrip("/")
        try:
            content = Path(local_path).read_bytes()
            upload_bytes_to_notebook_contents(
                notebook_id=self.notebook_id,
                session=self.session,
                rel_path=rel_path,
                content_bytes=content,
                timeout=timeout or 600,
            )
            return subprocess.CompletedProcess(
                args=[local_path, remote_path],
                returncode=0,
                stdout=f"Staged {len(content)} bytes at {rel_path}",
                stderr="",
            )
        except Exception as error:  # noqa: BLE001 - subprocess-shaped error boundary
            return subprocess.CompletedProcess(
                args=[local_path, remote_path],
                returncode=1,
                stdout="",
                stderr=str(error),
            )


__all__ = ["JupyterBridgeClient"]
