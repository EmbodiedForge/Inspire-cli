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
    context: Any = None,
    lab_url: str = "",
    local_binary_path: Path = Path(),
    session: Any = None,
    token: str | None = None,
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

    if session is not None:
        # HTTP path using requests.Session
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"token {token}"
        else:
            xsrf = None
            for cookie in session.cookies:
                if cookie.name == "_xsrf":
                    xsrf = cookie.value
                    break
            if xsrf:
                headers["X-XSRFToken"] = xsrf
        try:
            resp = session.put(
                api_url,
                headers=headers,
                json={
                    "type": "file",
                    "format": "base64",
                    "content": encoded,
                },
                timeout=30,
            )
            _log.debug("Contents API upload response: %d", resp.status_code)
            if resp.status_code not in (200, 201):
                _log.debug("Contents API upload error body: %s", resp.text[:500])
            return resp.status_code in (200, 201)
        except Exception as exc:
            _log.debug("Contents API upload failed: %s", exc, exc_info=True)
            return False

    # Playwright path
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
    context: Any = None,
    lab_url: str = "",
    local_hash: str = "",
    session: Any = None,
    token: str | None = None,
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
        f"{base}api/contents/{_CONTENTS_API_RTUNNEL_FILENAME}.sha256?format=base64&content=1"
    )

    if session is not None:
        # HTTP path using requests.Session
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"token {token}"
        try:
            resp = session.get(binary_url, headers=headers, timeout=5)
            if resp.status_code == 404:
                _log.debug("rtunnel binary not found on notebook (404)")
                return False
            if resp.status_code not in (200, 201):
                _log.debug("rtunnel binary metadata check returned %d", resp.status_code)
                return False

            resp = session.get(sidecar_url, headers=headers, timeout=5)
            if resp.status_code == 404:
                _log.debug("rtunnel hash sidecar not found on notebook (404)")
                return False
            if resp.status_code not in (200, 201):
                _log.debug("rtunnel hash sidecar check returned %d", resp.status_code)
                return False

            body = resp.json()
            remote_b64 = body.get("content", "")
            remote_hash = _b64.b64decode(remote_b64).decode("ascii").strip()
            if remote_hash == local_hash:
                _log.debug("rtunnel hash matches: %s", local_hash)
                return True
            _log.debug("rtunnel hash mismatch: local=%s remote=%s", local_hash, remote_hash)
            return False
        except Exception as exc:
            _log.debug("rtunnel hash check failed: %s", exc, exc_info=True)
            return False

    # Playwright path
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
    context: Any = None,
    lab_url: str = "",
    hex_hash: str = "",
    session: Any = None,
    token: str | None = None,
) -> bool:
    """Upload a ``.sha256`` sidecar alongside the rtunnel binary (best-effort).

    Returns ``True`` on success, ``False`` on failure. Failures are logged but
    must not block the setup flow.
    """
    import base64 as _b64

    base = _jupyter_server_base(lab_url)
    api_url = f"{base}api/contents/{_CONTENTS_API_RTUNNEL_FILENAME}.sha256"
    encoded = _b64.b64encode(hex_hash.encode("ascii")).decode("ascii")

    if session is not None:
        # HTTP path using requests.Session
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"token {token}"
        try:
            resp = session.put(
                api_url,
                headers=headers,
                json={
                    "type": "file",
                    "format": "base64",
                    "content": encoded,
                },
                timeout=5,
            )
            ok = resp.status_code in (200, 201)
            if not ok:
                _log.debug("Hash sidecar upload returned %d", resp.status_code)
            return ok
        except Exception as exc:
            _log.debug("Hash sidecar upload failed: %s", exc, exc_info=True)
            return False

    # Playwright path
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
    context: Any = None,
    lab_url: str = "",
    ssh_runtime: Optional[SshRuntimeConfig] = None,
    session: Any = None,
    token: str | None = None,
) -> Optional[str]:
    """Decide whether to upload the rtunnel binary via the Contents API.

    Returns ``_CONTENTS_API_RTUNNEL_FILENAME`` when a usable copy is already on
    the notebook (hash-verified), or after a successful upload.  Returns
    ``None`` when no upload is needed (e.g. ``rtunnel_bin`` is configured so the
    setup script will copy from the shared path) or when all upload attempts
    fail.
    """
    local_rtunnel = Path.home() / ".local" / "bin" / "rtunnel"
    local_exists = local_rtunnel.is_file()
    _log.debug("Local rtunnel path: %s (exists=%s)", local_rtunnel, local_exists)

    rtunnel_bin_configured = ssh_runtime and ssh_runtime.rtunnel_bin
    policy = ssh_runtime.rtunnel_upload_policy if ssh_runtime else "auto"

    if ssh_runtime is not None and rtunnel_bin_configured:
        _log.info("  Using configured rtunnel path: %s", ssh_runtime.rtunnel_bin)
        trace_event("rtunnel_bin_configured", rtunnel_bin=ssh_runtime.rtunnel_bin)

    if policy == "never":
        _log.debug("  Upload policy: never — skipping Contents API upload.")
        update_trace_summary(upload_policy=policy)
        trace_event("rtunnel_upload_skipped", policy=policy)
        return None

    if policy == "auto" and rtunnel_bin_configured:
        if local_exists:
            local_hash = _compute_rtunnel_hash(local_rtunnel)
            if local_hash and _rtunnel_matches_on_notebook(
                context=context,
                lab_url=lab_url,
                local_hash=local_hash,
                session=session,
                token=token,
            ):
                trace_event("rtunnel_upload_reused_contents_copy", policy=policy)
                return _CONTENTS_API_RTUNNEL_FILENAME
        trace_event("rtunnel_upload_skipped", policy=policy, reason="configured_bin_path")
        return None

    # -- "always", or "auto" without rtunnel_bin: download + upload -----------

    if policy == "always" and rtunnel_bin_configured:
        _log.debug("  Upload policy: always — preparing Contents API fallback.")
        trace_event("rtunnel_upload_policy_forced", policy=policy)

    if not local_exists:
        download_url = (
            ssh_runtime.rtunnel_download_url if ssh_runtime else DEFAULT_RTUNNEL_DOWNLOAD_URL
        )
        if _download_rtunnel_locally(download_url, local_rtunnel):
            _log.debug("  Downloaded rtunnel binary locally.")
            trace_event("rtunnel_local_downloaded", download_url=download_url)
        else:
            _log.warning("  Failed to download rtunnel binary locally.")
            trace_event("rtunnel_local_download_failed", download_url=download_url)

    if local_rtunnel.is_file():
        _log.debug("Local rtunnel binary: %d bytes", local_rtunnel.stat().st_size)
        local_hash = _compute_rtunnel_hash(local_rtunnel)
        if local_hash and _rtunnel_matches_on_notebook(
            context=context,
            lab_url=lab_url,
            local_hash=local_hash,
            session=session,
            token=token,
        ):
            _log.debug("  rtunnel binary already on notebook (skipping upload).")
            trace_event("rtunnel_upload_already_present", policy=policy)
            return _CONTENTS_API_RTUNNEL_FILENAME
        elif _upload_rtunnel_via_contents_api(
            context=context,
            lab_url=lab_url,
            local_binary_path=local_rtunnel,
            session=session,
            token=token,
        ):
            if local_hash:
                _upload_rtunnel_hash_sidecar(
                    context=context,
                    lab_url=lab_url,
                    hex_hash=local_hash,
                    session=session,
                    token=token,
                )
            _log.info("  Uploaded rtunnel binary via Jupyter Contents API.")
            trace_event("rtunnel_contents_upload_success", policy=policy)
            return _CONTENTS_API_RTUNNEL_FILENAME
        else:
            _log.warning("  Failed to upload rtunnel binary via Jupyter Contents API.")
            trace_event("rtunnel_contents_upload_failed", policy=policy)

    else:
        _log.warning("  rtunnel binary not found at %s", local_rtunnel)
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
