"""Config options: Bridge."""

from __future__ import annotations

from inspire.config.schema_models import ConfigOption, _parse_int, _parse_list

BRIDGE_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_BRIDGE_ACTION_TIMEOUT",
        toml_key="bridge.action_timeout",
        field_name="bridge_action_timeout",
        description="Bridge action timeout in seconds",
        default=600,
        category="Bridge",
        parser=_parse_int,
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_BRIDGE_DENYLIST",
        toml_key="bridge.denylist",
        field_name="bridge_action_denylist",
        description="Glob patterns to block from sync (comma/newline separated)",
        default=[],
        category="Bridge",
        parser=_parse_list,
        scope="project",
    ),
]
