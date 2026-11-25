"""Shared Launch Preview envelope for workload-specific prepared launches."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from inspire.cli.logging_setup import redact_text

_SENSITIVE_KEYS = {"authorization", "cookie", "set-cookie"}
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "auth",
)


def _is_sensitive_key(key: object) -> bool:
    lowered = str(key).strip().lower().replace("-", "_")
    return lowered in _SENSITIVE_KEYS or any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_structure(value: Any, known_secrets: tuple[str, ...] = ()) -> Any:
    """Redact credential-shaped fields and known session values without renaming other fields."""
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if _is_sensitive_key(key) else redact_structure(item, known_secrets))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_structure(item, known_secrets) for item in value]
    if isinstance(value, tuple):
        return [redact_structure(item, known_secrets) for item in value]
    if isinstance(value, str):
        for secret in known_secrets:
            if secret:
                value = value.replace(secret, "<redacted>")
        return redact_text(value)
    return value


@dataclass(frozen=True)
class LaunchPreview:
    """Side-effect-free public view of one prepared workload launch."""

    kind: str
    account_context: dict[str, Any]
    intent: dict[str, Any]
    sources: dict[str, Any]
    resolved: dict[str, Any]
    scheduling_offer: dict[str, Any] | list[dict[str, Any]] | None
    availability: dict[str, Any] | list[dict[str, Any]] | None
    platform_request: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    mutation: str = "skipped"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["platform_request"] = redact_structure(self.platform_request)
        return payload


__all__ = ["LaunchPreview", "redact_structure"]
