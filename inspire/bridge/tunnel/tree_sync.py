"""Non-destructive directory tree sync over a bridge.

`inspire sync` moves commits, so uncommitted work cannot travel with it, and its remote half ends
in a `git reset --hard` that is one flag away from deleting that work (Codeberg #33). This module
moves a *tree* instead, and it never removes the destination: a fresh destination is created, an
existing one is moved aside to a timestamped sibling, and every `rm` below targets only a path this
module just created itself.

Two engines share one displacement step:

``tar``
    Local gzipped tar -> staged upload -> checksum -> extract into a staging sibling -> move into
    place. Upload and remote script run through injected seams, so an SSH bridge stages with scp
    while a Jupyter-only bridge stages through the Contents API. Needs nothing on the remote but
    `tar` and a sha256 tool, which is the whole point: the notebook images that motivated #33 have
    no rsync.

``rsync``
    Incremental transfer through the same ProxyCommand SSH argv. Merge mode (default) runs
    `rsync -az --partial` with **no** `--delete`, so the destination only ever gains. Swap mode
    stages through `--link-dest` (cheap hardlinks against the current destination) and then runs
    the same displacement step as tar, which is how a fixed remote path is replaced atomically
    without a window where it is missing.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import posixpath
import re
import shlex
import subprocess
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from .config import load_tunnel_config
from .models import TunnelConfig, TunnelError
from .scp import run_scp_transfer
from .ssh_exec import get_ssh_command_args, run_ssh_command

logger = logging.getLogger(__name__)

RemoteCommandRunner = Callable[..., subprocess.CompletedProcess[str]]
FileTransferRunner = Callable[..., subprocess.CompletedProcess[str]]
LocalCommandRunner = Callable[..., subprocess.CompletedProcess[str]]

OK_MARKER = "INSPIRE_TREE_SYNC_OK"
ERR_MARKER = "INSPIRE_TREE_SYNC_ERR"
RSYNC_PRESENT_MARKER = "INSPIRE_RSYNC_PRESENT"

# Remote script exits. The displacement step reports these, so a caller can tell a corrupt upload
# apart from a rejected tree without reading prose.
EXIT_SHA_MISMATCH = 90
EXIT_MISSING_TREE_ROOT = 91
EXIT_REQUIRE_FAILED = 92
EXIT_BACKUP_EXISTS = 93

_REMOTE_EXIT_MESSAGES = {
    EXIT_SHA_MISMATCH: "archive checksum mismatch on the bridge",
    EXIT_MISSING_TREE_ROOT: "staged tree root missing after extraction",
    EXIT_REQUIRE_FAILED: "a required path was missing from the staged tree",
    EXIT_BACKUP_EXISTS: "a displacement backup from the same second already exists",
}

_HOST_SPEC_RE = re.compile(r"^[A-Za-z0-9._-]+@localhost$")


@dataclass(frozen=True)
class TreeSyncResult:
    """Outcome of one tree sync, shaped for the CLI success payload."""

    success: bool
    engine: str
    local: str
    remote: str
    swap: bool = False
    bytes: int = 0
    files: int = 0
    displaced_backup: Optional[str] = None
    engine_note: Optional[str] = None
    dry_run: bool = False
    error: Optional[str] = None
    exit_code: Optional[int] = None

    def as_payload(self) -> dict[str, object]:
        return {
            "status": "success" if self.success else "error",
            "engine": self.engine,
            "local": self.local,
            "remote": self.remote,
            "swap": self.swap,
            "bytes": self.bytes,
            "files": self.files,
            "displaced_backup": self.displaced_backup,
            "dry_run": self.dry_run,
            "engine_note": self.engine_note,
        }


# ---------------------------------------------------------------------------
# Input validation and local sizing
# ---------------------------------------------------------------------------


def _validate_local_dir(local_dir: str) -> Path:
    local = Path(str(local_dir))
    if not local.exists():
        raise ValueError(f"Local directory not found: {local_dir}")
    if not local.is_dir():
        raise ValueError(f"Local path is not a directory: {local_dir}")
    return local


def _validate_relative_paths(paths: Sequence[str]) -> list[str]:
    """Keep `--require` inside the synced tree: absolute paths and `..` are rejected."""
    checked: list[str] = []
    for raw in paths:
        path = str(raw).strip()
        if not path:
            raise ValueError("Required paths must not be empty")
        if path.startswith("/") or path.startswith("~/"):
            raise ValueError(f"Required paths must be relative to the synced tree: {path}")
        normalized = posixpath.normpath(path)
        if normalized == ".." or normalized.startswith("../"):
            raise ValueError(f"Required paths must stay inside the synced tree: {path}")
        checked.append(normalized)
    return checked


def _local_tree_stats(local: Path) -> tuple[int, int]:
    """Count files and total content bytes of the tree we are about to carry."""
    files = 0
    total_bytes = 0
    for _root, _dirs, names in os.walk(local):
        for name in names:
            files += 1
            with contextlib.suppress(OSError):
                total_bytes += (Path(_root) / name).stat().st_size
    return files, total_bytes


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_local_archive(local: Path, archive_path: str) -> None:
    """Write `local` as a tar.gz whose single root is the directory's own name."""
    with tarfile.open(archive_path, mode="w:gz") as archive:
        archive.add(str(local), arcname=local.name, recursive=True)


# ---------------------------------------------------------------------------
# Remote path naming and script building
# ---------------------------------------------------------------------------


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _staged_path(remote_dir: str, stamp: str) -> str:
    return f"{remote_dir.rstrip('/')}.inspire-staging-{stamp}"


def _backup_path(remote_dir: str, stamp: str) -> str:
    return f"{remote_dir.rstrip('/')}.inspire-bak-{stamp}"


def _cleanup_lines() -> list[str]:
    """Drop only what this script created: the staging sibling and the staged archive."""
    return [
        '  rm -rf "$staging"',
        '  if [ -n "$stage" ]; then rm -f "$stage"; fi',
    ]


def _build_displacement_block(require_paths: Sequence[str]) -> str:
    """Remote tail shared by both engines: gate the staged tree, then move it into place.

    Expects the caller to have set `dest`, `tree`, `staging`, `bak`, and `stage` (empty when no
    archive is involved). The destination is only ever `mv`'d - never removed - and a collision
    with an existing backup aborts rather than overwriting it.
    """
    lines: list[str] = []
    if require_paths:
        joined = " ".join(shlex.quote(path) for path in require_paths)
        lines += [
            f"for _req in {joined}; do",
            '  if [ ! -e "${tree}/${_req}" ]; then',
            f'    echo "{ERR_MARKER} required path missing from staged tree: ${{_req}}" >&2',
            *_cleanup_lines(),
            f"    exit {EXIT_REQUIRE_FAILED}",
            "  fi",
            "done",
        ]
    lines += [
        "_displaced=0",
        'if [ -e "$dest" ]; then',
        '  if [ -e "$bak" ]; then',
        f'    echo "{ERR_MARKER} backup already exists: $bak" >&2',
        *_cleanup_lines(),
        f"    exit {EXIT_BACKUP_EXISTS}",
        "  fi",
        '  mv "$dest" "$bak"',
        "  _displaced=1",
        "fi",
        'mv "$tree" "$dest"',
        "_files=$(find \"$dest\" -type f 2>/dev/null | wc -l | tr -d '[:space:]')",
        'rm -rf "$staging"',
        'if [ -n "$stage" ]; then rm -f "$stage"; fi',
        'if [ "$_displaced" = "1" ]; then _bak_report=$bak; else _bak_report=; fi',
        f'printf \'{OK_MARKER}\\tbak=%s\\tfiles=%s\\n\' "$_bak_report" "${{_files:-0}}"',
    ]
    return "\n".join(lines)


def _build_tar_sync_script(
    *,
    stage_name: str,
    remote_dir: str,
    tree_name: str,
    expected_sha: str,
    staging: str,
    backup: str,
    require_paths: Sequence[str],
) -> str:
    """Checksum, stage, gate, and displace a uploaded archive in a single remote script.

    The archive is looked up by name under two roots because the two transports stage it
    differently: scp writes the absolute `/tmp/<name>`, while the Jupyter Contents API is rooted at
    the notebook server's directory, so its `/tmp` is `<server root>/tmp`. Both candidates are
    tried in that order and `stage` stays empty when neither holds the file.
    """
    lines = [
        "set -e",
        'stage=""',
        f"_stage_name={shlex.quote(stage_name)}",
        'for _candidate in "/tmp/$_stage_name" "$(pwd -P)/tmp/$_stage_name"; do',
        '  if [ -f "$_candidate" ]; then stage="$_candidate"; break; fi',
        "done",
        f"dest={shlex.quote(remote_dir)}",
        f"staging={shlex.quote(staging)}",
        f"bak={shlex.quote(backup)}",
        f"_expected={shlex.quote(expected_sha)}",
        'if [ -z "$stage" ]; then',
        f'  echo "{ERR_MARKER} staged archive not found: /tmp/$_stage_name '
        f'or $(pwd -P)/tmp/$_stage_name" >&2',
        f"  exit {EXIT_SHA_MISMATCH}",
        "fi",
        "_actual=$(sha256sum \"$stage\" 2>/dev/null | awk '{print $1}')",
        '[ -n "$_actual" ] || _actual=$(shasum -a 256 "$stage" 2>/dev/null | awk \'{print $1}\')',
        'if [ "$_actual" != "$_expected" ]; then',
        f'  echo "{ERR_MARKER} checksum mismatch for $stage (expected $_expected, got $_actual)" >&2',
        *_cleanup_lines(),
        f"  exit {EXIT_SHA_MISMATCH}",
        "fi",
        'mkdir -p "$(dirname "$dest")"',
        'rm -rf "$staging"',
        'mkdir -p "$staging"',
        'tar -xzf "$stage" -C "$staging"',
        f"tree={shlex.quote(f'{staging}/{tree_name}')}",
        'if [ ! -d "$tree" ]; then',
        f'  echo "{ERR_MARKER} staged tree root missing after extraction: $tree" >&2',
        *_cleanup_lines(),
        f"  exit {EXIT_MISSING_TREE_ROOT}",
        "fi",
        _build_displacement_block(require_paths),
    ]
    return "\n".join(lines)


def _build_require_check_script(*, remote_dir: str, require_paths: Sequence[str]) -> str:
    """Post-merge gate: confirm the required paths exist in the destination now."""
    joined = " ".join(shlex.quote(path) for path in require_paths)
    lines = [
        "set -e",
        f"dest={shlex.quote(remote_dir)}",
        'if [ ! -d "$dest" ]; then',
        f'  echo "{ERR_MARKER} destination missing after sync: $dest" >&2',
        f"  exit {EXIT_REQUIRE_FAILED}",
        "fi",
    ]
    if require_paths:
        lines += [
            f"for _req in {joined}; do",
            '  if [ ! -e "${dest}/${_req}" ]; then',
            f'    echo "{ERR_MARKER} required path missing after sync: ${{_req}}" >&2',
            f"    exit {EXIT_REQUIRE_FAILED}",
            "  fi",
            "done",
        ]
    lines += [
        "_files=$(find \"$dest\" -type f 2>/dev/null | wc -l | tr -d '[:space:]')",
        f"printf '{OK_MARKER}\\tbak=\\tfiles=%s\\n' \"${{_files:-0}}\"",
    ]
    return "\n".join(lines)


def _build_rsync_preflight_script(*, remote_dir: str, staging: Optional[str]) -> str:
    lines = [
        "set -e",
        f"dest={shlex.quote(remote_dir)}",
        'mkdir -p "$(dirname "$dest")"',
    ]
    if staging is not None:
        lines += [f"staging={shlex.quote(staging)}", 'rm -rf "$staging"']
    return "\n".join(lines)


def _build_swap_displace_script(
    *, remote_dir: str, staging: str, backup: str, require_paths: Sequence[str]
) -> str:
    """Displacement for a tree rsync already staged, so no archive step is involved."""
    lines = [
        "set -e",
        'stage=""',
        f"dest={shlex.quote(remote_dir)}",
        f"staging={shlex.quote(staging)}",
        f"bak={shlex.quote(backup)}",
        f"tree={shlex.quote(staging)}",
        'mkdir -p "$(dirname "$dest")"',
        'if [ ! -d "$tree" ]; then',
        f'  echo "{ERR_MARKER} staged tree missing after rsync: $tree" >&2',
        f"  exit {EXIT_MISSING_TREE_ROOT}",
        "fi",
        _build_displacement_block(require_paths),
    ]
    return "\n".join(lines)


def _build_install_rsync_script(*, apt_mirror_url: Optional[str]) -> str:
    """Install rsync on the bridge: plain apt first, then the configured mirror if one is set.

    The mirror attempt moves apt sources aside under a suffix unique to this invocation and puts
    them back through a trap, so a pre-existing ``*.bak`` is never read, clobbered, or activated,
    and a timeout or kill in the middle of the window still leaves apt configured as it was found.
    The apt calls inside that window run as background jobs waited on by the shell, which keeps
    the trap able to interrupt them: bash defers traps received while a foreground command runs,
    but interrupts ``wait`` immediately.
    """
    lines = [
        "set +e",
        "if command -v rsync >/dev/null 2>&1; then",
        f'  echo "{RSYNC_PRESENT_MARKER}"',
        "  exit 0",
        "fi",
        "_INET=0",
        'timeout 3 "${BASH:-/bin/bash}" -c '
        "'exec 3<>/dev/tcp/archive.ubuntu.com/80' 2>/dev/null && _INET=1 || true",
        "export DEBIAN_FRONTEND=noninteractive",
        'if [ "$_INET" = 1 ]; then',
        "  timeout 60 apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=10 update -qq"
        " >/dev/null 2>&1 || true",
        "  timeout 120 apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=20 install -y -qq rsync"
        " >/dev/null 2>&1 || true",
        "fi",
    ]
    mirror = str(apt_mirror_url or "").strip()
    if mirror:
        tag = f".inspire-apt-{uuid.uuid4().hex[:12]}"
        lines += [
            "if ! command -v rsync >/dev/null 2>&1; then",
            f"  MIRROR_URL={shlex.quote(mirror)}",
            '  DISTRO_ID=$(. /etc/os-release 2>/dev/null && echo "${ID:-}")',
            '  MIRROR_DISTRO="${DISTRO_ID:-ubuntu}"',
            '  [ "$MIRROR_DISTRO" = "debian" ] || MIRROR_DISTRO="ubuntu"',
            '  CODENAME=$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-}")',
            '  [ -n "$CODENAME" ] || CODENAME=$(lsb_release -cs 2>/dev/null || true)',
            '  [ -n "$CODENAME" ] || CODENAME=jammy',
            '  MIRROR_COMPONENTS="main restricted universe multiverse"',
            '  [ "${DISTRO_ID:-}" = "debian" ] && MIRROR_COMPONENTS="main"',
            '  MIRROR_URL="${MIRROR_URL%/}"',
            '  case "$MIRROR_URL" in',
            '    */repository) MIRROR_URL="$MIRROR_URL/$MIRROR_DISTRO" ;;',
            "    */repository/debian|*/repository/ubuntu) true ;;",
            "  esac",
            '  MIRROR_URL="$MIRROR_URL/"',
            f"  _tag={shlex.quote(tag)}",
            "  _apt_pid=",
            "  _inspire_restore_apt() {",
            '    if [ -n "$_apt_pid" ]; then kill "$_apt_pid" 2>/dev/null; _apt_pid=; fi',
            "    rm -f /etc/apt/sources.list.d/inspire-mirror.list",
            '    for _bak in "/etc/apt/sources.list$_tag" /etc/apt/sources.list.d/*"$_tag"; do',
            '      [ -f "$_bak" ] || continue',
            '      mv -f "$_bak" "${_bak%$_tag}" 2>/dev/null',
            "    done",
            "  }",
            "  trap '_inspire_restore_apt' EXIT",
            "  trap 'exit 129' HUP",
            "  trap 'exit 130' INT",
            "  trap 'exit 143' TERM",
            "  for _f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list"
            " /etc/apt/sources.list.d/*.sources; do",
            '    [ -f "$_f" ] || continue',
            '    [ -e "$_f$_tag" ] && continue',
            '    mv "$_f" "$_f$_tag" 2>/dev/null',
            "  done",
            '  echo "deb $MIRROR_URL $CODENAME $MIRROR_COMPONENTS"'
            " > /etc/apt/sources.list.d/inspire-mirror.list",
            "  timeout 60 apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=10 update -qq"
            " >/dev/null 2>&1 </dev/null &",
            "  _apt_pid=$!",
            '  wait "$_apt_pid" || true',
            "  _apt_pid=",
            "  timeout 120 apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=20 install -y -qq rsync"
            " >/dev/null 2>&1 </dev/null &",
            "  _apt_pid=$!",
            '  wait "$_apt_pid" || true',
            "  _apt_pid=",
            "  _inspire_restore_apt",
            "fi",
        ]
    lines += [
        "if command -v rsync >/dev/null 2>&1; then",
        f'  echo "{RSYNC_PRESENT_MARKER}"',
        "  exit 0",
        "fi",
        'echo "rsync is not installed and could not be installed on the bridge" >&2',
        "exit 1",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Remote result parsing
# ---------------------------------------------------------------------------


def _parse_ok_marker(stdout: str) -> tuple[Optional[str], Optional[int]]:
    for line in reversed((stdout or "").splitlines()):
        if not line.startswith(OK_MARKER):
            continue
        fields = dict(part.split("=", 1) for part in line.split("\t")[1:] if "=" in part)
        backup = fields.get("bak") or None
        files: Optional[int]
        try:
            files = int(fields.get("files", ""))
        except ValueError:
            files = None
        return backup, files
    return None, None


def _last_error_line(result: subprocess.CompletedProcess[str]) -> str:
    for stream in (result.stderr, result.stdout):
        text = str(stream or "").strip()
        if not text:
            continue
        for line in reversed(text.splitlines()):
            stripped = line.strip()
            if stripped:
                return stripped[:400]
    return "no output from the bridge"


def _result_from_remote(
    result: subprocess.CompletedProcess[str],
    *,
    base: TreeSyncResult,
) -> TreeSyncResult:
    if result.returncode == 0:
        backup, files = _parse_ok_marker(result.stdout)
        return TreeSyncResult(
            **{
                **base.__dict__,
                "success": True,
                "displaced_backup": backup,
                "files": base.files if files is None else files,
            }
        )
    detail = _last_error_line(result)
    known = _REMOTE_EXIT_MESSAGES.get(result.returncode or 0)
    message = f"{known}: {detail}" if known else detail
    return TreeSyncResult(
        **{**base.__dict__, "success": False, "error": message, "exit_code": result.returncode}
    )


def _failure(base: TreeSyncResult, message: str) -> TreeSyncResult:
    return TreeSyncResult(**{**base.__dict__, "success": False, "error": message})


def _run_remote(
    run_command_fn: RemoteCommandRunner,
    script: str,
    *,
    bridge_name: Optional[str],
    config: Optional[TunnelConfig],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return run_command_fn(
        script,
        bridge_name=bridge_name,
        config=config,
        timeout=timeout,
        capture_output=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------


def sync_tree_via_tar(
    *,
    local_dir: str,
    remote_dir: str,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: int = 120,
    require: Sequence[str] = (),
    dry_run: bool = False,
    run_command_fn: RemoteCommandRunner | None = None,
    transfer_fn: FileTransferRunner | None = None,
    stage_name: str | None = None,
) -> TreeSyncResult:
    """Upload a tree as a checksummed archive and move it into place without deleting anything.

    Args:
        local_dir: Local directory whose *contents* become the remote directory's contents.
        remote_dir: Destination path on the bridge. Created when absent; moved aside when present.
        bridge_name: Bridge profile to use (default profile when None).
        config: Tunnel configuration (loaded when None).
        timeout: Per-step timeout in seconds.
        require: Paths, relative to the tree, that must exist in the staged tree before the
            destination is touched at all.
        dry_run: Report the local payload without uploading or running anything remotely.
        run_command_fn: Remote shell seam (SSH by default; the Jupyter transport injects its own).
        transfer_fn: File staging seam for the archive (scp by default).
        stage_name: Name the archive is staged under, in ``tmp``. A caller whose transfer seam
            cannot honour an absolute path passes a stable name and resolves it remotely.

    Returns:
        TreeSyncResult; failures carry ``error``/``exit_code`` instead of raising.

    Raises:
        ValueError: If ``local_dir`` is not a directory or a required path is not tree-relative.
        TunnelNotAvailableError/BridgeNotFoundError: If no usable bridge is configured.
    """
    local = _validate_local_dir(local_dir)
    require_paths = _validate_relative_paths(require)
    if not str(remote_dir or "").strip():
        raise ValueError("Remote directory must not be empty")
    if config is None:
        config = load_tunnel_config()
    run_command_fn = run_command_fn or run_ssh_command
    transfer_fn = transfer_fn or run_scp_transfer

    files, total_bytes = _local_tree_stats(local)
    stamp = _utc_stamp()
    staging = _staged_path(remote_dir, stamp)
    backup = _backup_path(remote_dir, stamp)
    stage_name = stage_name or f"inspire-tree-{uuid.uuid4().hex[:8]}.tar.gz"
    remote_archive = f"/tmp/{stage_name}"

    base = TreeSyncResult(
        success=False,
        engine="tar",
        local=str(local),
        remote=remote_dir,
        swap=False,
        bytes=total_bytes,
        files=files,
        dry_run=dry_run,
    )

    with tempfile.TemporaryDirectory(prefix="inspire-tree-sync-") as tmp_dir:
        archive_path = os.path.join(tmp_dir, "tree.tar.gz")
        try:
            _create_local_archive(local, archive_path)
        except (OSError, tarfile.TarError) as error:
            return _failure(base, f"Failed to archive {local}: {error}")
        expected_sha = _sha256_file(archive_path)
        archive_bytes = os.path.getsize(archive_path)

        if dry_run:
            return TreeSyncResult(
                **{
                    **base.__dict__,
                    "success": True,
                    "engine_note": (
                        f"dry-run: would upload {archive_bytes} bytes as a tar.gz archive and "
                        f"move it into {remote_dir} without deleting it"
                    ),
                }
            )

        logger.debug(
            "sync_tree_via_tar bridge=%s remote=%s archive=%s bytes=%s files=%s",
            bridge_name,
            remote_dir,
            remote_archive,
            archive_bytes,
            files,
        )
        try:
            scp_result = transfer_fn(
                local_path=archive_path,
                remote_path=remote_archive,
                download=False,
                bridge_name=bridge_name,
                config=config,
                timeout=timeout,
            )
            if scp_result.returncode != 0:
                return _failure(
                    base,
                    f"Failed to upload tree archive (scp exit {scp_result.returncode}): "
                    f"{_last_error_line(scp_result)}",
                )

            script = _build_tar_sync_script(
                stage_name=stage_name,
                remote_dir=remote_dir,
                tree_name=local.name,
                expected_sha=expected_sha,
                staging=staging,
                backup=backup,
                require_paths=require_paths,
            )
            result = _run_remote(
                run_command_fn,
                script,
                bridge_name=bridge_name,
                config=config,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return _failure(base, f"Tree sync timed out after {timeout}s")
        except (subprocess.SubprocessError, OSError, TunnelError, ValueError) as error:
            return _failure(base, str(error))

        return _result_from_remote(result, base=base)


def _build_rsync_transport(
    *,
    bridge_name: Optional[str],
    config: Optional[TunnelConfig],
) -> tuple[str, str]:
    """Derive rsync's `-e` string and host spec from the same argv `inspire tunnel ssh` uses.

    The ProxyCommand value carries spaces and its own quotes, so every argv element is quoted
    before being joined; rsync re-splits the result with shell-like rules.
    """
    args = get_ssh_command_args(bridge_name=bridge_name, config=config)
    # rsync execs the first token of -e as the remote shell, so the program name has to stay in
    argv = list(args)
    host = ""
    if argv and _HOST_SPEC_RE.fullmatch(argv[-1]):
        host = str(argv.pop())
    if not host:
        raise TunnelError("Could not derive the bridge SSH destination for rsync.")
    return " ".join(shlex.quote(part) for part in argv), host


def _run_local_rsync(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def sync_tree_via_rsync(
    *,
    local_dir: str,
    remote_dir: str,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: int = 120,
    swap: bool = False,
    require: Sequence[str] = (),
    dry_run: bool = False,
    run_command_fn: RemoteCommandRunner | None = None,
    local_run_fn: LocalCommandRunner | None = None,
) -> TreeSyncResult:
    """Sync a tree with rsync through the bridge ProxyCommand.

    Merge mode (``swap=False``) runs `rsync -az --partial` with no `--delete`, so files the
    destination has and the source lacks survive. Swap mode stages into a sibling with
    `--link-dest=<remote_dir>` and then displaces the destination the same way the tar engine
    does, which keeps a fixed remote path valid until the moment it is replaced.

    Raises:
        ValueError: If ``local_dir`` is not a directory or a required path is not tree-relative.
        TunnelError/BridgeNotFoundError/TunnelNotAvailableError: If no usable bridge is configured.
    """
    local = _validate_local_dir(local_dir)
    require_paths = _validate_relative_paths(require)
    if not str(remote_dir or "").strip():
        raise ValueError("Remote directory must not be empty")
    if config is None:
        config = load_tunnel_config()
    run_command_fn = run_command_fn or run_ssh_command
    local_run_fn = local_run_fn or _run_local_rsync

    files, total_bytes = _local_tree_stats(local)
    base = TreeSyncResult(
        success=False,
        engine="rsync",
        local=str(local),
        remote=remote_dir,
        swap=swap,
        bytes=total_bytes,
        files=files,
        dry_run=dry_run,
    )
    if dry_run:
        return TreeSyncResult(
            **{
                **base.__dict__,
                "success": True,
                "engine_note": (
                    "dry-run: would rsync into "
                    + (f"{_staged_path(remote_dir, '<stamp>')} then displace " if swap else "")
                    + f"{remote_dir} without deleting it"
                ),
            }
        )

    transport, host = _build_rsync_transport(bridge_name=bridge_name, config=config)
    stamp = _utc_stamp()
    staged = _staged_path(remote_dir, stamp) if swap else ""
    backup = _backup_path(remote_dir, stamp)
    target = staged or remote_dir

    argv = ["rsync", "-az", "--partial"]
    if swap:
        argv += ["--link-dest", remote_dir]
    argv += ["-e", transport, f"{str(local).rstrip('/')}/", f"{host}:{target.rstrip('/')}"]

    logger.debug(
        "sync_tree_via_rsync bridge=%s remote=%s swap=%s argv=%s",
        bridge_name,
        remote_dir,
        swap,
        argv,
    )

    try:
        preflight = _run_remote(
            run_command_fn,
            _build_rsync_preflight_script(remote_dir=remote_dir, staging=(staged or None)),
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
        )
        if preflight.returncode != 0:
            return _failure(
                base, f"Failed to prepare the remote destination: {_last_error_line(preflight)}"
            )

        rsync_result = local_run_fn(argv, timeout=timeout)
        if rsync_result.returncode != 0:
            return _failure(
                base,
                f"rsync failed (exit {rsync_result.returncode}): {_last_error_line(rsync_result)}",
            )

        if swap:
            script = _build_swap_displace_script(
                remote_dir=remote_dir,
                staging=staged,
                backup=backup,
                require_paths=require_paths,
            )
        else:
            script = _build_require_check_script(remote_dir=remote_dir, require_paths=require_paths)
        result = _run_remote(
            run_command_fn,
            script,
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _failure(base, f"Tree sync timed out after {timeout}s")
    except (subprocess.SubprocessError, OSError, TunnelError, ValueError) as error:
        return _failure(base, str(error))

    return _result_from_remote(result, base=base)


# ---------------------------------------------------------------------------
# Remote rsync availability
# ---------------------------------------------------------------------------


def probe_remote_rsync(
    *,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: int = 30,
    run_command_fn: RemoteCommandRunner | None = None,
) -> bool:
    """True when `rsync` is already on the bridge's PATH."""
    if config is None:
        config = load_tunnel_config()
    run_command_fn = run_command_fn or run_ssh_command
    try:
        result = run_command_fn(
            "command -v rsync",
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
            capture_output=True,
            check=False,
        )
    except (subprocess.SubprocessError, OSError, TunnelError, ValueError) as error:
        logger.debug("probe_remote_rsync failed: %s", error)
        return False
    return result.returncode == 0


def _configured_apt_mirror() -> Optional[str]:
    """APT mirror from `[remote_env] APT_MIRROR_URL` / tunnel defaults, if any is configured."""
    from inspire.config.ssh_runtime import resolve_ssh_runtime_config

    try:
        return resolve_ssh_runtime_config().apt_mirror_url or None
    except Exception as error:  # noqa: BLE001 - a missing mirror only drops the fallback path
        logger.debug("could not resolve apt mirror: %s", error)
        return None


def ensure_remote_rsync(
    *,
    bridge_name: Optional[str] = None,
    config: Optional[TunnelConfig] = None,
    timeout: int = 300,
    apt_mirror_url: Optional[str] = None,
    run_command_fn: RemoteCommandRunner | None = None,
) -> bool:
    """Install `rsync` on the bridge if it is missing, using the configured apt mirror as backup.

    Returns True when rsync is usable afterwards. The mirror attempt disables the system's apt
    sources only by moving them under an invocation-unique suffix and restores them through a
    trap that fires on success, failure, and termination, so the bridge's apt config is left as
    it was found - including any pre-existing ``*.bak`` files, which are never touched.
    """
    if probe_remote_rsync(
        bridge_name=bridge_name,
        config=config,
        timeout=min(timeout, 30),
        run_command_fn=run_command_fn,
    ):
        return True
    if config is None:
        config = load_tunnel_config()
    run_command_fn = run_command_fn or run_ssh_command
    if apt_mirror_url is None:
        apt_mirror_url = _configured_apt_mirror()

    script = _build_install_rsync_script(apt_mirror_url=apt_mirror_url)
    try:
        result = _run_remote(
            run_command_fn,
            script,
            bridge_name=bridge_name,
            config=config,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError, TunnelError, ValueError) as error:
        logger.debug("ensure_remote_rsync failed: %s", error)
        return False
    return result.returncode == 0 and RSYNC_PRESENT_MARKER in (result.stdout or "")


__all__ = [
    "ERR_MARKER",
    "EXIT_BACKUP_EXISTS",
    "EXIT_MISSING_TREE_ROOT",
    "EXIT_REQUIRE_FAILED",
    "EXIT_SHA_MISMATCH",
    "OK_MARKER",
    "RSYNC_PRESENT_MARKER",
    "TreeSyncResult",
    "ensure_remote_rsync",
    "probe_remote_rsync",
    "sync_tree_via_rsync",
    "sync_tree_via_tar",
]
