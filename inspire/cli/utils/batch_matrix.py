"""JSON/TOML Cartesian matrix expansion for workload batch commands."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any, Mapping

import tomllib

from inspire.config import ConfigError


class _StrictFormatMap(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        raise ConfigError(f"Unknown batch template variable: {key}")


def load_batch_file(path: Path) -> dict[str, Any]:
    """Load one JSON or TOML batch file."""
    suffix = path.suffix.lower()
    try:
        with path.open("rb") as stream:
            if suffix == ".json":
                data = json.load(stream)
            elif suffix in {".toml", ".tml"}:
                data = tomllib.load(stream)
            else:
                raise ConfigError("Batch file must use .json, .toml, or .tml")
    except OSError as exc:
        raise ConfigError(f"Could not read batch file {path}: {exc}") from exc
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Invalid batch file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Batch file must contain an object at the top level")
    return data


def _matrix_rows(value: Any) -> list[dict[str, Any]]:
    if value in (None, {}):
        return [{}]
    if not isinstance(value, Mapping):
        raise ConfigError("matrix must be an object")
    keys = [str(key) for key in value]
    dimensions: list[list[Any]] = []
    for key in keys:
        values = value[key]
        if not isinstance(values, list) or not values:
            raise ConfigError(f"matrix.{key} must be a non-empty array")
        dimensions.append(values)
    return [dict(zip(keys, values, strict=True)) for values in itertools.product(*dimensions)]


def _render(value: Any, variables: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        try:
            return value.format_map(_StrictFormatMap(variables))
        except (ValueError, KeyError) as exc:
            raise ConfigError(f"Invalid batch template {value!r}: {exc}") from exc
    if isinstance(value, list):
        return [_render(item, variables) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _render(item, variables) for key, item in value.items()}
    return value


def expand_batch(data: Mapping[str, Any], *, kind: str) -> list[dict[str, Any]]:
    """Expand defaults, per-item matrices, and string templates."""
    defaults = data.get("defaults", {})
    if not isinstance(defaults, Mapping):
        raise ConfigError("defaults must be an object")
    items = data.get(kind, data.get("jobs", data.get("items")))
    if not isinstance(items, list) or not items:
        raise ConfigError(f"Batch file must contain a non-empty {kind}, jobs, or items array")

    expanded: list[dict[str, Any]] = []
    global_matrix = data.get("matrix")
    for item_index, raw_item in enumerate(items):
        if not isinstance(raw_item, Mapping):
            raise ConfigError(f"Batch item {item_index} must be an object")
        item_matrix = raw_item.get("matrix", global_matrix)
        for matrix_values in _matrix_rows(item_matrix):
            merged = {
                **dict(defaults),
                **{str(key): value for key, value in raw_item.items() if key != "matrix"},
            }
            variables = {
                **merged,
                **matrix_values,
                "index": len(expanded),
                "item_index": item_index,
            }
            rendered = _render(merged, variables)
            if not isinstance(rendered, dict):
                raise ConfigError(f"Expanded batch item {item_index} is not an object")
            rendered["_matrix"] = dict(matrix_values)
            expanded.append(rendered)
    return expanded


__all__ = ["expand_batch", "load_batch_file"]
