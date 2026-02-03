"""SSH key helpers for notebook SSH."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def load_ssh_public_key(pubkey_path: Optional[str] = None) -> str:
    """Load an SSH public key to authorize notebook SSH access."""
    candidates: list[Path]

    if pubkey_path:
        candidates = [Path(pubkey_path).expanduser()]
    else:
        candidates = [
            Path.home() / ".ssh" / "id_ed25519.pub",
            Path.home() / ".ssh" / "id_rsa.pub",
        ]

    for path in candidates:
        if path.exists():
            key = path.read_text(encoding="utf-8", errors="ignore").strip()
            if key:
                return key

    raise ValueError(
        "No SSH public key found. Provide --pubkey PATH or generate one with 'ssh-keygen'."
    )


__all__ = ["load_ssh_public_key"]
