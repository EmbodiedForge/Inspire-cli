"""Helpers for selecting known compute groups."""

from __future__ import annotations

from inspire.config import Config
from inspire.cli.utils.resources import KNOWN_COMPUTE_GROUPS
from inspire.compute_groups import compute_group_name_map, load_compute_groups_from_config


def _known_compute_groups_from_config(*, show_all: bool) -> dict[str, str]:
    known_groups = KNOWN_COMPUTE_GROUPS
    if show_all:
        return known_groups

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        if config.compute_groups:
            groups_tuple = load_compute_groups_from_config(config.compute_groups)
            return compute_group_name_map(groups_tuple)
    except Exception:
        return known_groups
    return known_groups
