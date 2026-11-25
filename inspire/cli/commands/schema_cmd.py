"""`inspire schema` - the CLI's own surface as data, for callers that read JSON rather than prose.

An agent deciding what to run next has to answer "which command exists, and what does it cost?" from
`--help` text written for a person. This command answers both from the same metadata Click already
holds: the command tree, each command's parameters, and the risk and deprecation registries in
``inspire.cli.risk``. It is generated, never curated - a command that gains an option gains it here in
the same commit, which is the only reason the output is trustworthy.

It touches no platform and no credential, so it works before `inspire init` and on a machine with no
account configured at all.
"""

from __future__ import annotations

import difflib
import inspect
import re
from typing import Any, Optional

import click

from inspire.cli.context import Context, EXIT_VALIDATION_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.risk import deprecation_note, iter_commands, risk_note, risk_of
from inspire.cli.utils.errors import exit_with_error

#: A help line that demonstrates a call: an indented `inspire ...`, an `Examples:` line carrying one,
#: or a bulleted one. Prose that merely mentions a command in passing does not match.
_EXAMPLE_LINE = re.compile(r"^(?:[-*]\s+|Examples?\s*:\s*|e\.g\.\s+)?(inspire\s+\S.*)$")

#: JSON has no way to carry these, and the values that are not scalars (click's own `Sentinel.UNSET`)
#: mean exactly what null means: nothing was declared.
_JSON_DEFAULT_TYPES = (str, int, float, bool, type(None))


def _help_text(command: click.Command) -> str:
    """The command's help the way Click renders it: de-indented, up to any form feed.

    Click's "do not rewrap this paragraph" marker is a backspace character. It is invisible in a
    terminal and a control byte in JSON, so it is dropped here; nothing else about the text changes.
    """
    if command.help is None:
        return ""
    return inspect.cleandoc(command.help).partition("\f")[0].replace("\x08", "")


def _summary(command: click.Command) -> str:
    """One line about the command: its first sentence, which is how every help text here is written."""
    if command.short_help:
        return " ".join(command.short_help.split())
    lines = _help_text(command).splitlines()
    first = lines[0].strip() if lines else ""
    head, period, rest = first.partition(". ")
    return head + period if rest.strip() else first


def _examples(command: click.Command) -> list[str]:
    """The example calls the command's own help and epilog carry, in the order they appear."""
    text = "\n".join(part for part in (_help_text(command), command.epilog or "") if part)
    examples: dict[str, None] = {}
    for line in text.splitlines():
        match = _EXAMPLE_LINE.match(line.strip())
        if match:
            examples.setdefault(match.group(1).strip(), None)
    return list(examples)


def _json_default(default: Any) -> Any:
    """A command's declared default in a form JSON can carry, or null when it has no constant value."""
    if isinstance(default, _JSON_DEFAULT_TYPES):
        return default
    if isinstance(default, (list, tuple, set, frozenset)):
        return [item for item in default if isinstance(item, _JSON_DEFAULT_TYPES)]
    return None


def _param_name(param: click.Parameter) -> str:
    """The one name a caller types: the long option, or the argument's own name."""
    if isinstance(param, click.Option):
        long_opts = [opt for opt in param.opts if opt.startswith("--")] or param.opts
        return max(long_opts, key=len)
    return param.opts[0] if param.opts else param.name


def _param_aliases(param: click.Parameter, name: str) -> list[str]:
    """Every other spelling of the parameter, so an alias is as findable as the canonical name."""
    if not isinstance(param, click.Option):
        return []
    return sorted({opt for opt in [*param.opts, *param.secondary_opts] if opt != name})


def _param_spec(param: click.Parameter) -> dict[str, Any]:
    """One parameter, described from Click's metadata and nothing else."""
    name = _param_name(param)
    spec: dict[str, Any] = {
        "name": name,
        "aliases": _param_aliases(param, name),
        "kind": "option" if isinstance(param, click.Option) else "argument",
        "type": param.type.name,
        "required": bool(param.required),
        "default": _json_default(param.default),
    }
    choices = getattr(param.type, "choices", None)
    if choices:
        spec["choices"] = list(choices)
    help_text = getattr(param, "help", None)
    if help_text:
        spec["help"] = " ".join(str(help_text).split())
    return spec


def _resolve(root: click.Command, words: tuple[str, ...]) -> Optional[click.Command]:
    """Walk a command path through the tree, or None when any segment is not a known command."""
    command = root
    for word in words:
        subcommands = getattr(command, "commands", None)
        if not subcommands or word not in subcommands:
            return None
        command = subcommands[word]
    return command


def _tree(root: click.Command) -> list[dict[str, Any]]:
    """Every command and group as ``{path, summary, risk}`` (+ deprecation when set), in the order
    `--help` lists them.

    Only deprecated entries gain ``deprecated``/``deprecated_note``: an absent field and a false
    flag say the same thing about the other ~190 commands, and the tree stays compact to read.
    """
    entries: list[dict[str, Any]] = []
    for path, command in iter_commands(root):
        entry: dict[str, Any] = {
            "path": path,
            "summary": _summary(command),
            "risk": risk_of(command, path),
        }
        alias_of = getattr(command, "alias_of", None)
        if alias_of:
            entry["alias_of"] = alias_of
        note = deprecation_note(path)
        if note:
            entry["deprecated"] = True
            entry["deprecated_note"] = note
        entries.append(entry)
    entries.sort(key=lambda entry: entry["path"])
    return entries


def _detail(command: click.Command, path: str) -> dict[str, Any]:
    """One command's full contract: what it is, what it costs, and what it accepts.

    A deprecated command additionally carries ``deprecated: true`` and its ``deprecated_note``;
    any other command carries neither field - only its absence tells the caller the command is
    the current way to do the thing.
    """
    description = _help_text(command)
    detail: dict[str, Any] = {
        "path": path,
        "summary": _summary(command),
        "description": description,
        "risk": risk_of(command, path),
    }
    deprecation = deprecation_note(path)
    if deprecation:
        detail["deprecated"] = True
        detail["deprecated_note"] = deprecation
    detail["params"] = [_param_spec(param) for param in command.params]
    alias_of = getattr(command, "alias_of", None)
    if alias_of:
        detail["alias_of"] = alias_of
    note = risk_note(path)
    if note:
        detail["risk_note"] = note
    examples = _examples(command)
    if examples:
        detail["examples"] = examples
    subcommands = getattr(command, "commands", None)
    if subcommands:
        detail["subcommands"] = sorted(subcommands)
    # Options a command consumes from the front of its arguments instead of through Click,
    # because everything after the remote command must be forwarded verbatim.
    front_options = getattr(command, "front_options", None)
    if front_options:
        detail["front_options"] = [dict(option) for option in front_options]
    return detail


#: A summary this long is already past what distinguishes one command from another in a table; the
#: full text is one `inspire schema <path>` away, and JSON always carries it whole.
_SUMMARY_COLUMN_WIDTH = 72


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Left-aligned columns, each as wide as its widest cell."""
    widths = [
        max([len(header)] + [len(row[index]) for row in rows])
        for index, header in enumerate(headers)
    ]
    lines = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)).rstrip()
    ]
    lines.extend(
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
        for row in rows
    )
    return "\n".join(lines)


def _render_tree(entries: list[dict[str, Any]]) -> str:
    rows = [
        [
            entry["risk"],
            entry["path"],
            _clip(entry["summary"], _SUMMARY_COLUMN_WIDTH)
            + (" [deprecated]" if entry.get("deprecated") else ""),
        ]
        for entry in entries
    ]
    table = _table(["RISK", "COMMAND", "SUMMARY"], rows)
    return f"{table}\n\n{len(entries)} commands. Detail: inspire schema <command path>"


def _render_detail(detail: dict[str, Any]) -> str:
    lines = [f"inspire {detail['path']}", f"risk: {detail['risk']}"]
    if detail.get("deprecated"):
        lines.append("deprecated: yes")
    if detail.get("deprecated_note"):
        lines.append(f"deprecated note: {detail['deprecated_note']}")
    if detail.get("risk_note"):
        lines.append(f"risk note: {detail['risk_note']}")
    description = detail.get("description") or ""
    if description:
        lines.extend(["", description])
    if detail.get("subcommands"):
        lines.extend(["", "Subcommands: " + ", ".join(detail["subcommands"])])
    if detail["params"]:
        rows = [
            [
                param["name"],
                param["kind"],
                param["type"],
                "yes" if param["required"] else "no",
                "-" if param["default"] is None else str(param["default"]),
                ", ".join(param["choices"]) if param.get("choices") else "-",
                param.get("help", "-"),
            ]
            for param in detail["params"]
        ]
        headers = ["PARAM", "KIND", "TYPE", "REQUIRED", "DEFAULT", "CHOICES", "HELP"]
        lines.extend(["", _table(headers, rows)])
    if detail.get("front_options"):
        # Not Click parameters, so they cannot appear in the table above - which is exactly
        # how `bridge exec --file` came to be documented in prose and absent from the contract.
        rows = [
            [option["name"], option["metavar"], option["help"]]
            for option in detail["front_options"]
        ]
        lines.extend(
            [
                "",
                "Front-only options (consumed before COMMAND; later occurrences are forwarded):",
                _table(["PARAM", "VALUE", "HELP"], rows),
            ]
        )
    # The description usually already carries its own Examples block; only examples from the epilog,
    # or written in a form the description does not show, are worth repeating here.
    extra_examples = [
        example for example in detail.get("examples", []) if example not in description
    ]
    if extra_examples:
        lines.extend(["", "Examples:"] + [f"  {example}" for example in extra_examples])
    return "\n".join(lines)


@click.command("schema")
@click.argument("command_path", nargs=-1, metavar="[COMMAND]...")
@pass_context
def schema(ctx: Context, command_path: tuple[str, ...]) -> None:
    """Describe every command, or one command's full contract.

    The output is generated from Click's own metadata plus the risk registry, so it cannot drift from
    what the CLI actually accepts: no command maintains a schema block by hand. Nothing here touches
    the platform, so it answers before `inspire init` too.

    With no arguments it lists every command with its risk level (read, write, high-risk-write - see
    docs/CLI-CONTRACT.md), marking any deprecated one. With a command path it prints that command's
    description, risk, deprecation note when it has one, parameters, and the examples its help text
    carries. In JSON, a deprecated entry carries ``deprecated`` and ``deprecated_note``; any other
    entry carries neither.

    \b
    Examples:
        inspire schema
        inspire schema job list
        inspire --json schema | jq -r '.data.items[] | select(.risk == "high-risk-write") | .path'
    """
    root = click.get_current_context().find_root().command

    if not command_path:
        entries = _tree(root)
        if ctx.json_output:
            click.echo(json_formatter.format_json({"items": entries, "total": len(entries)}))
        else:
            click.echo(_render_tree(entries))
        return

    path = " ".join(command_path)
    command = _resolve(root, command_path)
    if command is None:
        close = difflib.get_close_matches(path, [entry for entry, _ in iter_commands(root)], n=3)
        hint = "Run `inspire schema` for every command path."
        if close:
            hint = f"Did you mean: {', '.join(close)}? " + hint
        exit_with_error(
            ctx,
            "ValidationError",
            f"Unknown command path: {path!r}",
            EXIT_VALIDATION_ERROR,
            hint=hint,
        )

    detail = _detail(command, path)
    if ctx.json_output:
        click.echo(json_formatter.format_json(detail))
    else:
        click.echo(_render_detail(detail))


__all__ = ["schema"]
