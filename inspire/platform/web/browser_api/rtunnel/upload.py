"""Jupyter Contents API upload: rtunnel binary transfer and hash verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

try:
    from playwright.sync_api import Error as PlaywrightError
except ImportError:  # pragma: no cover

    class PlaywrightError(Exception):  # type: ignore[no-redef]
        pass


from inspire.config.ssh_runtime import DEFAULT_RTUNNEL_DOWNLOAD_URL, SshRuntimeConfig

from ._jupyter import _build_jupyter_xsrf_headers, _jupyter_server_base
from .logging import trace_event, update_trace_summary

import logging

_log = logging.getLogger("inspire.platform.web.browser_api.rtunnel")

_CONTENTS_API_RTUNNEL_FILENAME = "inspire_rtunnel_bin"


def _upload_rtunnel_via_contents_api(
    context: Any,
    lab_url: str,
    local_binary_path: Path,
) -> bool:
    """Upload a local rtunnel binary to the notebook via Jupyter Contents API.

    Reads the binary at *local_binary_path*, base64-encodes it, and PUTs it to
    ``{server_base}api/contents/{filename}``.  Returns ``True`` on success,
    ``False`` on any failure (missing file, HTTP error, network error).
    """
    import base64 as _b64

    if not local_binary_path.is_file():
        _log.debug("Upload skipped: local binary not found at %s", local_binary_path)
        return False

    try:
        raw = local_binary_path.read_bytes()
    except OSError as exc:
        _log.debug("Failed to read local binary %s: %s", local_binary_path, exc)
        return False

    encoded = _b64.b64encode(raw).decode("ascii")
    base = _jupyter_server_base(lab_url)
    api_url = f"{base}api/contents/{_CONTENTS_API_RTUNNEL_FILENAME}"
    _log.debug("Uploading rtunnel via Contents API: %s (%d bytes)", api_url, len(raw))

    try:
        headers = _build_jupyter_xsrf_headers(context)
        resp = context.request.put(
            api_url,
            headers=headers,
            data={
                "type": "file",
                "format": "base64",
                "content": encoded,
            },
            timeout=30000,
        )
        _log.debug("Contents API upload response: %d", resp.status)
        if resp.status not in (200, 201):
            try:
                body = resp.text()[:500]
            except Exception:
                body = "(unable to read body)"
            _log.debug("Contents API upload error body: %s", body)
        return resp.status in (200, 201)
    except (
        PlaywrightError,
        ConnectionError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
    ) as exc:
        _log.debug("Contents API upload failed: %s", exc, exc_info=True)
        return False


def _compute_rtunnel_hash(path: Path) -> Optional[str]:
    """Return the SHA-256 hex digest of the file at *path*, or ``None`` on error."""
    import hashlib

    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        _log.debug("Failed to hash %s: %s", path, exc)
        return None


def _rtunnel_matches_on_notebook(
    context: Any,
    lab_url: str,
    local_hash: str,
) -> bool:
    """Check whether the notebook already has an up-to-date rtunnel binary.

    Returns ``True`` only when **both** the binary exists on the notebook
    (metadata-only check) **and** the ``.sha256`` sidecar matches *local_hash*.
    Returns ``False`` on any 404, mismatch, decode error, or network error.
    """
    import base64 as _b64

    base = _jupyter_server_base(lab_url)
    binary_url = f"{base}api/contents/{_CONTENTS_API_RTUNNEL_FILENAME}?content=0"
    sidecar_url = (
        f"{base}api/contents/{_CONTENTS_API_RTUNNEL_FILENAME}.sha256" f"?format=base64&content=1"
    )

    try:
        # 1. Check the binary exists (metadata only, fast).
        resp = context.request.get(binary_url, timeout=5000)
        if resp.status == 404:
            _log.debug("rtunnel binary not found on notebook (404)")
            return False
        if resp.status not in (200, 201):
            _log.debug("rtunnel binary metadata check returned %d", resp.status)
            return False

        # 2. Read the hash sidecar.
        resp = context.request.get(sidecar_url, timeout=5000)
        if resp.status == 404:
            _log.debug("rtunnel hash sidecar not found on notebook (404)")
            return False
        if resp.status not in (200, 201):
            _log.debug("rtunnel hash sidecar check returned %d", resp.status)
            return False

        body = resp.json()
        remote_b64 = body.get("content", "")
        remote_hash = _b64.b64decode(remote_b64).decode("ascii").strip()
        if remote_hash == local_hash:
            _log.debug("rtunnel hash matches: %s", local_hash)
            return True
        _log.debug("rtunnel hash mismatch: local=%s remote=%s", local_hash, remote_hash)
        return False
    except (
        PlaywrightError,
        ConnectionError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        UnicodeDecodeError,
    ) as exc:
        _log.debug("rtunnel hash check failed: %s", exc, exc_info=True)
        return False


def _upload_rtunnel_hash_sidecar(
    context: Any,
    lab_url: str,
    hex_hash: str,
) -> bool:
    """Upload a ``.sha256`` sidecar alongside the rtunnel binary (best-effort).

    Returns ``True`` on success, ``False`` on failure. Failures are logged but
    must not block the setup flow.
    """
    import base64 as _b64

    base = _jupyter_server_base(lab_url)
    api_url = f"{base}api/contents/{_CONTENTS_API_RTUNNEL_FILENAME}.sha256"
    encoded = _b64.b64encode(hex_hash.encode("ascii")).decode("ascii")

    try:
        headers = _build_jupyter_xsrf_headers(context)
        resp = context.request.put(
            api_url,
            headers=headers,
            data={
                "type": "file",
                "format": "base64",
                "content": encoded,
            },
            timeout=5000,
        )
        ok = resp.status in (200, 201)
        if not ok:
            _log.debug("Hash sidecar upload returned %d", resp.status)
        return ok
    except (
        PlaywrightError,
        ConnectionError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
    ) as exc:
        _log.debug("Hash sidecar upload failed: %s", exc, exc_info=True)
        return False


def _resolve_rtunnel_binary(
    *,
    context: Any,
    lab_url: str,
    ssh_runtime: Optional[SshRuntimeConfig],
) -> Optional[str]:
    """Decide whether to upload the rtunnel binary via the Contents API.

    Returns ``_CONTENTS_API_RTUNNEL_FILENAME`` when a usable copy is already on
    the notebook (hash-verified), or after a successful upload.  Returns
    ``None`` when no upload is needed (e.g. ``rtunnel_bin`` is configured so the
    setup script will copy from the shared path) or when all upload attempts
    fail.
    """
    import sys as _sys

    local_rtunnel = Path.home() / ".local" / "bin" / "rtunnel"
    local_exists = local_rtunnel.is_file()
    _log.debug("Local rtunnel path: %s (exists=%s)", local_rtunnel, local_exists)

    rtunnel_bin_configured = ssh_runtime and ssh_runtime.rtunnel_bin
    policy = ssh_runtime.rtunnel_upload_policy if ssh_runtime else "auto"

    if rtunnel_bin_configured:
        _sys.stderr.write(f"  Using configured rtunnel path: {ssh_runtime.rtunnel_bin}\n")
        trace_event("rtunnel_bin_configured", rtunnel_bin=ssh_runtime.rtunnel_bin)

    if policy == "never":
        _sys.stderr.write("  Upload policy: never — skipping Contents API upload.\n")
        update_trace_summary(upload_policy=policy)
        trace_event("rtunnel_upload_skipped", policy=policy)
        return None

    if policy == "auto" and rtunnel_bin_configured:
        if local_exists:
            local_hash = _compute_rtunnel_hash(local_rtunnel)
            if local_hash and _rtunnel_matches_on_notebook(context, lab_url, local_hash):
                trace_event("rtunnel_upload_reused_contents_copy", policy=policy)
                return _CONTENTS_API_RTUNNEL_FILENAME
        trace_event("rtunnel_upload_skipped", policy=policy, reason="configured_bin_path")
        return None

    # -- "always", or "auto" without rtunnel_bin: download + upload -----------

    if policy == "always" and rtunnel_bin_configured:
        _sys.stderr.write("  Upload policy: always — preparing Contents API fallback.\n")
        trace_event("rtunnel_upload_policy_forced", policy=policy)

    if not local_exists:
        download_url = (
            ssh_runtime.rtunnel_download_url if ssh_runtime else DEFAULT_RTUNNEL_DOWNLOAD_URL
        )
        if _download_rtunnel_locally(download_url, local_rtunnel):
            _sys.stderr.write("  Downloaded rtunnel binary locally.\n")
            trace_event("rtunnel_local_downloaded", download_url=download_url)
        else:
            _sys.stderr.write("  WARNING: Failed to download rtunnel binary locally.\n")
            trace_event("rtunnel_local_download_failed", download_url=download_url)

    if local_rtunnel.is_file():
        _log.debug("Local rtunnel binary: %d bytes", local_rtunnel.stat().st_size)
        local_hash = _compute_rtunnel_hash(local_rtunnel)
        if local_hash and _rtunnel_matches_on_notebook(context, lab_url, local_hash):
            _sys.stderr.write("  rtunnel binary already on notebook (skipping upload).\n")
            trace_event("rtunnel_upload_already_present", policy=policy)
            return _CONTENTS_API_RTUNNEL_FILENAME
        elif _upload_rtunnel_via_contents_api(context, lab_url, local_rtunnel):
            if local_hash:
                _upload_rtunnel_hash_sidecar(context, lab_url, local_hash)
            _sys.stderr.write("  Uploaded rtunnel binary via Jupyter Contents API.\n")
            trace_event("rtunnel_contents_upload_success", policy=policy)
            return _CONTENTS_API_RTUNNEL_FILENAME
        else:
            _sys.stderr.write(
                "  WARNING: Failed to upload rtunnel binary via Jupyter Contents API.\n"
            )
            trace_event("rtunnel_contents_upload_failed", policy=policy)

    else:
        _sys.stderr.write(f"  WARNING: rtunnel binary not found at {local_rtunnel}\n")
        trace_event("rtunnel_local_binary_missing", path=local_rtunnel)

    return None


def _download_rtunnel_locally(
    download_url: str,
    dest: Path,
) -> bool:
    """Download rtunnel binary from a URL to a local path.

    Downloads the ``.tar.gz`` archive, extracts the rtunnel binary, and places
    it at *dest*.  Returns ``True`` on success, ``False`` on any failure.
    """
    import tarfile
    import tempfile
    import urllib.request

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _log.debug("Downloading rtunnel from %s to %s", download_url, dest)
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            urllib.request.urlretrieve(download_url, str(tmp_path))
            _log.debug("Downloaded archive: %s (%d bytes)", tmp_path, tmp_path.stat().st_size)
            with tarfile.open(str(tmp_path), "r:gz") as tar:
                for member in tar.getmembers():
                    if member.isfile() and "rtunnel" in member.name:
                        extracted = tar.extractfile(member)
                        if extracted:
                            data = extracted.read()
                            dest.write_bytes(data)
                            dest.chmod(0o755)
                            _log.debug("Extracted rtunnel binary: %s (%d bytes)", dest, len(data))
                            return True
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        _log.debug("rtunnel local download failed: %s", exc, exc_info=True)
    return False
