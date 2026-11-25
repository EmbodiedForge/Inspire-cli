"""Report configuration keys that the loader will silently ignore (issue #10).

A typo in a config key does not fail: the loader reads the keys it knows and drops the rest, so
`target_dirs = [...]` under `[bridge]` behaves exactly like a bridge that was never configured. That is
the worst failure shape for this file, because the config *looks* set.

The rule is deliberately asymmetric, because noise here would be worse than silence:

* an **unknown section** is reported (`[remote-environment]` is almost certainly `[remote_env]`);
* an unknown **scalar** key inside a known section is reported (`target_dirs`, `targe_dir`);
* a **table** inside a known section is never reported. Named workspaces, profiles and account blocks are
  all tables, and a false accusation that working configuration is a typo teaches people to ignore this
  output;
* sections whose children are arbitrary by design (`[remote_env]` holds environment variable names) are
  skipped entirely.

Nothing here raises or fails a command. The caller decides how loud to be; this module only reports.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from inspire.config.schema import CONFIG_OPTIONS
from inspire.config.toml import _load_toml

logger = logging.getLogger(__name__)

#: Sections whose children are user-defined names rather than schema keys.
OPAQUE_SECTIONS = frozenset({"remote_env"})

#: Sections the loader honours but the option schema does not declare - account blocks, named project
#: and workspace selections, and profiles. Found the hard way: `[context]`, `[workspace_names]` and
#: `[workspace_specs]` are popped straight out of the raw TOML in `load_layers.py`, and the first version
#: of this module flagged all three on the maintainer's own working config. Deliberately
#: over-inclusive: allowing a name the loader ignores costs one missed detection, while accusing working
#: configuration of being a typo teaches people to ignore this output entirely.
STRUCTURAL_SECTIONS = frozenset(
    {
        "accounts",
        "context",
        "project_catalog",
        "project_selectors",
        "workspace_names",
        "workspace_selectors",
        "workspace_specs",
        "profiles",
        "projects",
        "workspaces",
    }
)


@dataclass(frozen=True)
class UnknownKey:
    """One config entry the loader will not read."""

    dotted: str
    source: str
    suggestion: str | None = None

    def describe(self) -> str:
        text = f"{self.dotted} ({self.source}) is not a configuration key and will be ignored"
        if self.suggestion:
            text += f" - did you mean {self.suggestion}?"
        return text


#: Keys the loader reads that the option schema does not declare. The schema is not a complete list of
#: accepted config - `[cli] prefer_source` is read straight out of the raw TOML in `load_layers.py`, the
#: `[defaults]` notebook/workspace keys come from `_DEFAULTS_FIELD_MAP` in `load_common.py`, and the
#: `[job]`/`[notebook]` `workspace_id` pins from `_apply_legacy_workspace_id_section` in `load_layers.py`
#: - so trusting the schema alone would have accused documented, working settings of being typos. Extend
#: this map rather than whitelisting the whole section, so detection survives where the keys are actually
#: known.
EXTRA_SECTION_KEYS: dict[str, frozenset[str]] = {
    "cli": frozenset({"prefer_source"}),
    "defaults": frozenset(
        {
            "notebook_image",
            "notebook_resource",
            "notebook_post_start",
            "notebook_post_start_mode",
            "workspace_id",
        }
    ),
    "job": frozenset({"workspace_id"}),
    "notebook": frozenset({"workspace_id"}),
}


def known_toml_keys() -> set[str]:
    """Every dotted TOML key the schema declares."""
    return {option.toml_key for option in CONFIG_OPTIONS if getattr(option, "toml_key", None)}


def _known_sections(keys: set[str]) -> set[str]:
    return (
        {key.split(".", 1)[0] for key in keys}
        | STRUCTURAL_SECTIONS
        | OPAQUE_SECTIONS
        | set(EXTRA_SECTION_KEYS)
    )


def _normalize(name: str) -> str:
    """Fold the separator confusion that causes most TOML typos: `remote-environment` vs `remote_env`."""
    return name.lower().replace("-", "_")


def _close(name: str, options: "list[str]") -> str | None:
    normalized = {_normalize(option): option for option in options}
    # A slightly looser cutoff than difflib's default: the best match wins anyway, and a wrong suggestion
    # is harmless advice, whereas silence on `[remote-environment]` leaves the reader to guess that the
    # supported spelling uses an underscore.
    matches = difflib.get_close_matches(_normalize(name), list(normalized), n=1, cutoff=0.66)
    return normalized[matches[0]] if matches else None


def _suggest(dotted: str, keys: set[str], sections: set[str]) -> str | None:
    candidates = [key for key in keys if key.split(".", 1)[0] == dotted.split(".", 1)[0]]
    if "." not in dotted:
        return _close(dotted, sorted(sections))
    if not candidates:
        # The whole section is unknown: suggest a section, not a key inside a section nobody typed.
        return _close(dotted.split(".", 1)[0], sorted(sections))
    return _close(dotted, sorted(candidates))


def audit_config_file(path: Path) -> list[UnknownKey]:
    """Find entries in one TOML file that the loader will ignore."""
    keys = known_toml_keys()
    sections = _known_sections(keys)
    findings: list[UnknownKey] = []

    try:
        data = _load_toml(path)
    except Exception as exc:  # noqa: BLE001 - a broken file is reported by the loader, not here
        logger.debug("config audit skipped %s: %s", path, exc)
        return findings

    for section, value in data.items():
        if isinstance(value, list):
            continue  # [[array.of.tables]] - projects and account blocks are declared this way
        if not isinstance(value, dict):
            # A bare top-level key is never valid: every documented option is section-scoped.
            findings.append(
                UnknownKey(str(section), str(path), _suggest(str(section), keys, sections))
            )
            continue
        if section not in sections:
            findings.append(
                UnknownKey(f"[{section}]", str(path), _suggest(section, keys, sections))
            )
            continue
        if section in OPAQUE_SECTIONS or section in STRUCTURAL_SECTIONS:
            continue
        for name, child in value.items():
            if isinstance(child, dict):
                continue  # structural: named workspaces, profiles, account blocks
            dotted = f"{section}.{name}"
            allowed = keys | {f"{section}.{key}" for key in EXTRA_SECTION_KEYS.get(section, ())}
            if dotted not in allowed:
                findings.append(UnknownKey(dotted, str(path), _suggest(dotted, allowed, sections)))

    return findings


def audit_config_files(paths: "Sequence[Path | None]") -> list[UnknownKey]:
    """Audit each existing file; ordering follows the input so output matches load precedence."""
    findings: list[UnknownKey] = []
    for path in paths:
        if path is None:
            continue
        try:
            if not Path(path).is_file():
                continue
        except OSError:  # pragma: no cover - unreadable path is the loader's problem
            continue
        findings.extend(audit_config_file(Path(path)))
    return findings


__all__ = [
    "EXTRA_SECTION_KEYS",
    "OPAQUE_SECTIONS",
    "STRUCTURAL_SECTIONS",
    "UnknownKey",
    "audit_config_file",
    "audit_config_files",
    "known_toml_keys",
]
