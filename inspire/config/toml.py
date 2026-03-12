"""TOML parsing and config file discovery for Inspire CLI config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib

from inspire.config.models import CONFIG_FILENAME, PROJECT_CONFIG_DIR, ConfigError
from inspire.config.schema import get_option_by_toml
from inspire.config.schema_models import ConfigOption


def _find_project_config() -> Path | None:
    current = Path.cwd()
    while current != current.parent:
        config_path = current / PROJECT_CONFIG_DIR / CONFIG_FILENAME
        if config_path.exists():
            return config_path
        current = current.parent
    return None


def _load_toml(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _flatten_toml(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(_flatten_toml(value, full_key))
        else:
            result[full_key] = value
    return result


def _toml_key_to_field(toml_key: str) -> str | None:
    option = get_option_by_toml(toml_key)
    return option.field_name if option else None


def _validate_toml_value(option: ConfigOption, value: Any) -> Any:
    if option.value_type is not None and not isinstance(value, option.value_type):
        raise ConfigError(
            f"Invalid type for {option.toml_key}: expected {option.value_type.__name__}, "
            f"got {type(value).__name__}"
        )
    if option.parser and isinstance(value, str):
        try:
            return option.parser(value)
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"Invalid value for {option.toml_key}: {exc}") from exc
    return value
