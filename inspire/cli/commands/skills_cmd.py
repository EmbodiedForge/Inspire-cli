"""`inspire skills` - the agent skill docs that ship inside this package.

A skill doc is only trustworthy when it describes the CLI the caller actually has. These files are
therefore read out of the installed ``inspire`` package rather than fetched: the docs an agent reads
and the binary it runs are the same version, and an outdated checkout never sends an agent down a
path a newer CLI abandoned.

Everything here is local file reads - no platform call, no credential - so the group works before
``inspire init`` and on a machine with no account configured at all.
"""

from __future__ import annotations

import re
import sys
from typing import Any

import click

if sys.version_info >= (3, 11):  # pragma: no cover - version-selected import
    from importlib.resources.abc import Traversable
else:  # pragma: no cover - version-selected import
    from importlib.abc import Traversable

from importlib.resources import files

from inspire.cli.context import Context, EXIT_JOB_NOT_FOUND, EXIT_VALIDATION_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error

#: Every skill lives under this package directory, one layer deep: ``inspire/skills/<name>/``.
SKILLS_PACKAGE = "inspire"
SKILLS_DIRECTORY = "skills"
SKILL_ENTRYPOINT = "SKILL.md"

#: The contract's "resource not found" code is named for jobs by history; a missing skill directory
#: or file is the same class of answer, so the alias keeps the call sites readable.
EXIT_NOT_FOUND = EXIT_JOB_NOT_FOUND

#: ``metadata: {cliHelp: "..."}`` is a nested block in some skills and an inline flow mapping in
#: others, so it is matched anywhere inside the frontmatter rather than as a top-level key.
_CLI_HELP = re.compile(r"""cliHelp\s*:\s*["']?([^"'\n,}]+)""")

#: Placeholder shown when a skill directory exists but its SKILL.md does not say something yet.
UNKNOWN = "unknown"


def _skill_root() -> Traversable:
    """The packaged ``inspire/skills`` directory, resolved so an installed wheel works too."""
    return files(SKILLS_PACKAGE).joinpath(SKILLS_DIRECTORY)


def _reject_unsafe_segment(ctx: Context, value: str, label: str) -> None:
    """Refuse anything that could climb out of ``inspire/skills`` before it reaches the filesystem."""
    if not value.strip():
        exit_with_error(
            ctx,
            "ValidationError",
            f"{label} must not be empty",
            EXIT_VALIDATION_ERROR,
            hint="Pass a skill name such as `shared`.",
        )
    if value.startswith("/") or value.startswith("~") or "\\" in value:
        exit_with_error(
            ctx,
            "ValidationError",
            f"{label} must be a relative path inside a skill: {value!r}",
            EXIT_VALIDATION_ERROR,
            hint="Use a path like `examples/train.sh`, with no `..` and no leading `/`.",
        )
    for segment in value.split("/"):
        if segment in ("", ".", ".."):
            exit_with_error(
                ctx,
                "ValidationError",
                f"{label} must not traverse directories: {value!r}",
                EXIT_VALIDATION_ERROR,
                hint="Use a path like `examples/train.sh`, with no `..` and no leading `/`.",
            )


def _walk_safe(root: Traversable, relative: str) -> Traversable | None:
    """Resolve ``relative`` under ``root`` one segment at a time, or None when it does not exist."""
    current = root
    for segment in relative.split("/"):
        candidate = current.joinpath(segment)
        if not (candidate.is_dir() or candidate.is_file()):
            return None
        current = candidate
    return current


def _frontmatter(text: str) -> dict[str, str]:
    """The leading ``---`` block of a SKILL.md as flat fields, parsed without a YAML dependency.

    Only what this command reports is read: ``name``, ``description``, ``version``, and the
    ``cliHelp`` hint nested under ``metadata``. A folded scalar (``description: >``) is joined from
    its indented continuation lines; anything nested deeper than a top-level key is otherwise ignored.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    block: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        block.append(line)

    fields: dict[str, str] = {}
    index = 0
    while index < len(block):
        line = block[index]
        index += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or line[:1] in (" ", "\t"):
            continue
        key, separator, raw_value = line.partition(":")
        if not separator:
            continue
        value = raw_value.strip()
        if value in (">", ">-", "|", "|-"):
            pieces: list[str] = []
            while index < len(block) and (
                block[index][:1] in (" ", "\t") or not block[index].strip()
            ):
                pieces.append(block[index].strip())
                index += 1
            value = " ".join(piece for piece in pieces if piece)
        fields[key.strip().lower()] = value.strip("'\"")

    if "cli_help" not in fields:
        help_match = _CLI_HELP.search("\n".join(block))
        if help_match:
            fields["cli_help"] = help_match.group(1).strip()
    return fields


def _skill_names(root: Traversable) -> list[str]:
    """Every skill directory, sorted, whether or not it carries a SKILL.md yet."""
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())


def _require_root(ctx: Context) -> Traversable:
    root = _skill_root()
    if not root.is_dir():
        exit_with_error(
            ctx,
            "NotFound",
            f"No `{SKILLS_DIRECTORY}` directory in the installed {SKILLS_PACKAGE} package",
            EXIT_NOT_FOUND,
            hint="Reinstall inspire-cli; the agent skill docs ship inside the wheel.",
        )
    return root


def _skill_entries(root: Traversable) -> list[dict[str, Any]]:
    """One row per skill, described by its own frontmatter with the directory name as the floor."""
    entries: list[dict[str, Any]] = []
    for name in _skill_names(root):
        fields: dict[str, str] = {}
        entrypoint = root.joinpath(name).joinpath(SKILL_ENTRYPOINT)
        if entrypoint.is_file():
            fields = _frontmatter(entrypoint.read_text(encoding="utf-8"))
        entries.append(
            {
                "name": fields.get("name") or name,
                "description": fields.get("description", ""),
                "version": fields.get("version", ""),
                "cli_help": fields.get("cli_help") or "inspire --help",
            }
        )
    return entries


def _layer_entries(ctx: Context, root: Traversable, relative: str) -> list[dict[str, Any]]:
    """One ``ls`` layer under a skill, so an agent can find the files a SKILL.md points at."""
    target = _walk_safe(root, relative)
    if target is None or not target.is_dir():
        exit_with_error(
            ctx,
            "NotFound",
            f"No skill directory `{relative}`",
            EXIT_NOT_FOUND,
            hint="Run `inspire skills list` to see the skills this CLI ships.",
        )
    return [
        {"name": entry.name, "type": "dir" if entry.is_dir() else "file"}
        for entry in sorted(target.iterdir(), key=lambda item: item.name)
    ]


@click.group("skills")
def skills() -> None:
    """Read the agent skill docs packaged with this CLI version."""


@skills.command("list")
@click.argument("path", required=False)
@pass_context
def list_skills(ctx: Context, path: str | None) -> None:
    """List every skill, or one layer inside a skill.

    With no PATH, every skill is reported with the description, version, and help command its own
    frontmatter declares. With a PATH such as `shared`, that skill's directory layer is listed the
    way `ls` lists it, so an agent can see which files exist before reading one.

    \b
    Examples:
        inspire --json skills list
        inspire --json skills list shared
    """
    root = _require_root(ctx)
    if path is None:
        items: list[dict[str, Any]] = _skill_entries(root)
    else:
        _reject_unsafe_segment(ctx, path, "skill path")
        items = _layer_entries(ctx, root, path.strip().strip("/"))

    if ctx.json_output:
        click.echo(json_formatter.format_json({"items": items, "total": len(items)}))
        return

    for item in items:
        if item.get("type") is not None:
            name = str(item["name"])
            click.echo(f"{name}/" if item["type"] == "dir" else name)
        else:
            click.echo(
                "{name} ({version})  {description}".format(
                    name=item["name"],
                    version=item["version"] or UNKNOWN,
                    description=item["description"],
                ).rstrip()
            )


@skills.command("read")
@click.argument("name")
@click.argument("file", required=False)
@pass_context
def read_skill(ctx: Context, name: str, file: str | None) -> None:
    """Print a skill's SKILL.md, or a named file under it, verbatim.

    Human mode writes raw markdown to stdout so it can be piped or redirected as-is. JSON mode wraps
    the same text as ``{"name", "path", "content"}``.

    \b
    Examples:
        inspire skills read shared
        inspire --json skills read shared
    """
    _reject_unsafe_segment(ctx, name, "skill name")
    entry = SKILL_ENTRYPOINT if file is None else file
    _reject_unsafe_segment(ctx, entry, "skill file")
    relative = f"{name.strip().strip('/')}/{entry.strip().strip('/')}"

    root = _require_root(ctx)
    target = _walk_safe(root, relative)
    if target is None or not target.is_file():
        exit_with_error(
            ctx,
            "NotFound",
            f"No skill file `{relative}`",
            EXIT_NOT_FOUND,
            hint="`inspire skills list` shows the skills; `inspire skills list <name>` their files.",
        )
        return  # unreachable; exit_with_error exits, but keeps the type of `target` narrow

    content = target.read_text(encoding="utf-8")
    if ctx.json_output:
        click.echo(json_formatter.format_json({"name": name, "path": relative, "content": content}))
        return
    click.echo(content, nl=False)
