"""Environment parsing helpers for Inspire CLI config."""

from __future__ import annotations

import logging
import os
import re
import shlex
from typing import Optional

from inspire.config.models import ConfigError

_log = logging.getLogger("inspire.config.env")

_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REF_RE = re.compile(
    r"^\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))$"
)


def _parse_remote_timeout(value: str) -> int:
    """Parse INSP_REMOTE_TIMEOUT environment variable."""
    try:
        timeout = int(value)
        if timeout < 5:
            # Warn but allow small values for testing
            pass
        return timeout
    except ValueError as e:
        raise ConfigError(
            "Invalid INSP_REMOTE_TIMEOUT value. It must be an integer number of seconds."
        ) from e


def _parse_denylist(value: Optional[str]) -> list[str]:
    """Parse denylist from env (comma or newline separated)."""
    if not value:
        return []
    parts: list[str] = []
    for raw in value.replace("\r", "").split("\n"):
        for chunk in raw.split(","):
            item = chunk.strip()
            if item:
                parts.append(item)
    return parts


def resolve_remote_env(env_dict: dict[str, str]) -> dict[str, str]:
    """Resolve ``remote_env`` values, expanding local env references."""
    if not env_dict:
        return {}

    resolved: dict[str, str] = {}
    for key, raw_value in env_dict.items():
        if not _VAR_NAME_RE.match(key):
            raise ConfigError(
                f"Invalid remote_env key: {key!r} (must match {_VAR_NAME_RE.pattern})"
            )

        value = raw_value
        if value == "":
            env_var = key
            if env_var not in os.environ:
                raise ConfigError(
                    f"remote_env[{key}] is empty but {env_var} is not set in the local environment"
                )
            value = os.environ[env_var]
            _log.debug(
                "$%s expanded from LOCAL os.environ. "
                "Set [remote_env].%s in config.toml to avoid implicit local dependencies.",
                env_var,
                key,
            )
        else:
            match = _ENV_REF_RE.match(value)
            if match is not None:
                env_var = match.group("braced") or match.group("bare")
                if env_var not in os.environ:
                    raise ConfigError(
                        f"remote_env[{key}] references {env_var} but it is not set in the local environment"
                    )
                value = os.environ[env_var]
                _log.debug(
                    "$%s expanded from LOCAL os.environ for remote_env[%s]. "
                    "Set [remote_env].%s in config.toml to avoid implicit local dependencies.",
                    env_var,
                    key,
                    key,
                )

        resolved[key] = value

    return resolved


def build_env_exports(env_dict: dict[str, str]) -> str:
    """Build shell export commands for remote environment variables."""
    resolved = resolve_remote_env(env_dict)
    if not resolved:
        return ""

    exports: list[str] = []
    for key, value in resolved.items():
        exports.append(f"export {key}={shlex.quote(value)}")

    return " && ".join(exports) + " && "
