"""Exact-name redaction for surfaces whose response schema is known.

The CLI's general redaction (``redact_text`` in :mod:`inspire.cli.logging_setup`,
``redact_structure`` in :mod:`inspire.cli.utils.launch_preview`) matches field names by
*substring*, because it has to guard every command's output without knowing any schema.
That is the wrong rule for a surface we do know: the platform's API-key rows carry
``key_id`` - the identifier a caller needs to pass to ``model api-keys show`` - next to
``value``, which is the key material itself.  A substring rule matches the ``key`` in the
first and misses the second, so such a command both hides the argument of the next command
and prints the secret.
"""

from __future__ import annotations

from typing import Any, Iterable

#: Field names on the platform's ``user`` service that carry API-key material.  ``key_id``
#: and ``ak_id`` are deliberately absent: they name a key, they do not authenticate with it.
API_KEY_MATERIAL_FIELDS = frozenset({"value", "sk", "secret", "api_key", "access_key", "token"})

_REDACTED = "<redacted>"


def redact_named_fields(value: Any, fields: Iterable[str] = API_KEY_MATERIAL_FIELDS) -> Any:
    """Redact every ``fields`` entry, at any depth, leaving other names untouched."""
    lowered = {str(field).strip().lower() for field in fields}
    return _redact(value, lowered)


def _redact(value: Any, lowered: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _REDACTED if str(key).strip().lower() in lowered else _redact(item, lowered)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, lowered) for item in value]
    return value


__all__ = ["API_KEY_MATERIAL_FIELDS", "redact_named_fields"]
