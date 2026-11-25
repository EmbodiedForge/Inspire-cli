"""Comment-preserving, atomic TOML document writes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import tomlkit


def load_toml_document(path: Path) -> tomlkit.TOMLDocument:
    """Load a TOML document or return an empty document when it does not exist."""
    if not path.exists():
        return tomlkit.document()
    return tomlkit.parse(path.read_text(encoding="utf-8"))


def atomic_write_text(path: Path, payload: str, *, default_mode: int = 0o600) -> None:
    """Atomically replace a text file, keeping its owner-restricted mode.

    The existing mode is preserved so a deliberate choice (0600 vs 0640 for a team-shared
    file) survives a rewrite - but group and other bits are always dropped. Every caller
    writes config or account state, and preserving a loose mode literally re-issued it on
    every save: one 0664 ``config.toml`` from an editor, a ``cp``, or an old release would
    keep a plaintext password world-readable no matter how many times we "wrote it 0600".
    If a genuinely shared file is ever needed it should be designed, not inherited.

    A symlinked path is followed to its target first: a dotfiles setup keeps ``config.toml``
    as a link into the repo, and replacing the link itself would quietly turn the managed
    file into a regular copy while the repository version stopped being the live one.
    """
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (path.stat().st_mode & 0o777 if path.exists() else default_mode) & ~0o077
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_write_toml(path: Path, document: tomlkit.TOMLDocument) -> None:
    """Atomically replace one TOML file while preserving its existing mode."""
    atomic_write_text(path, tomlkit.dumps(document))


__all__ = ["atomic_write_text", "atomic_write_toml", "load_toml_document"]
