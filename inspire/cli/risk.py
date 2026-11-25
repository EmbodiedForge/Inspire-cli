"""Risk level and deprecation for each command path, derived from the command's own name.

docs/CLI-CONTRACT.md gives every command a risk level and shows it in that command's `--help` and in
`inspire schema`. Hand-writing 191 levels would be 191 strings to keep honest, so the level comes from
the command's own verb and only the exceptions are listed. Precedence:

1. ``RISK_OVERRIDES`` - a path whose name lies about what it does: ``api`` runs a caller-selected
   method or JavaScript, and ``storage probe`` writes a remote file before removing it.
2. ``VERB_RISK`` - the command's verb, found by scanning its path from the last segment backwards and
   trying each segment whole before its first dash-token: ``ssh-keys`` is a listing, ``delete-check``
   only checks, ``set-default`` is a ``set``, and ``runtime set cwd`` finds its verb one segment up.
3. ``DEFAULT_RISK`` - the cautious guess for a verb nobody has looked at.

Step 3 is why ``explicit_risk`` is public and separate: it answers "nothing classified this" instead of
returning the guess, so a new command nobody classified fails ``tests/test_schema_command.py`` rather
than being labelled silently in production. Add the verb, or the path with a reason, and the test
passes again.

A group is a container, not an action, so its level is the highest among its leaves: `inspire --help`
then advertises the worst thing that group can do instead of implying a `job` tree is read-only.

``DEPRECATION_NOTES`` is the same trick for commands that still work but should no longer be
chosen: ``inspire run`` and ``inspire job quota`` remain registered and compatible, and one line
here is the whole deprecation - no command's own module or docstring carries it. A note names the
replacement and may only say the command could be removed in a future version; no removal version
is invented, because the registry cannot know a release decision that has not been made.

``apply_risk_labels`` is the render-time half: called once from `inspire/cli/main.py` after every
command is registered, it swaps each command object onto a subclass that prefixes the level (and,
when deprecated, a ``[deprecated]`` tag plus the replacement guidance) to its help text, and warns
once on stderr when a deprecated command is actually invoked. Reading stays warning-free:
``--help`` exits during argument parsing and ``inspire schema`` only inspects metadata, so neither
ever reaches the invocation path. Nothing per-command is edited, and both labels follow the
registry - change the registry and every `--help` line changes with it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Optional, cast

import click

RISK_READ = "read"
RISK_WRITE = "write"
RISK_HIGH_RISK_WRITE = "high-risk-write"

#: The three levels docs/CLI-CONTRACT.md defines, ordered by how much damage a call can do.
RISK_LEVELS: tuple[str, ...] = (RISK_READ, RISK_WRITE, RISK_HIGH_RISK_WRITE)

#: Used only to pick the worst level among a group's leaves; never shown to a caller.
_SEVERITY: dict[str, int] = {level: index for index, level in enumerate(RISK_LEVELS)}

#: Guess for a command whose verb appears in neither table. Mutating until proven otherwise: an
#: under-rated command is a surprise, an over-rated one is an extra `--yes`.
DEFAULT_RISK = RISK_WRITE

#: Verb -> level. A command's path is scanned from its last segment backwards, and each segment is
#: tried whole first, then by its first dash-token: `ssh-keys` is a listing while `bridge ssh` is not,
#: `image set-default` classifies as `set`, and `runtime set cwd` finds its verb one segment up.
VERB_RISK: dict[str, str] = {
    # No platform state changes, and the only local file written is the answer itself (`storage get`).
    "api-keys": RISK_READ,
    "availability": RISK_READ,
    "benchmark": RISK_READ,
    "cat": RISK_READ,
    "check": RISK_READ,
    "command": RISK_READ,
    "connection-info": RISK_READ,
    "delete-check": RISK_READ,
    "detail": RISK_READ,
    "describe": RISK_READ,
    "doctor": RISK_READ,
    "download": RISK_READ,
    "env": RISK_READ,
    "events": RISK_READ,
    "explain": RISK_READ,
    "export": RISK_READ,
    "get": RISK_READ,
    "info": RISK_READ,
    "inspect": RISK_READ,
    "instances": RISK_READ,
    "list": RISK_READ,
    "logs": RISK_READ,
    "ls": RISK_READ,
    "metrics": RISK_READ,
    # The mount path is a naming convention, resolved locally: no platform call, no state change.
    "mount-path": RISK_READ,
    "models": RISK_READ,
    "nodes": RISK_READ,
    "offers": RISK_READ,
    "permissions": RISK_READ,
    "policy": RISK_READ,
    "profile": RISK_READ,
    "quota": RISK_READ,
    "read": RISK_READ,
    "related": RISK_READ,
    "resolve": RISK_READ,
    # One read-only screen: cat/awk over /sys/fs/cgroup and /proc, nvidia-smi, and two GETs.
    "resource": RISK_READ,
    "roots": RISK_READ,
    "scaling": RISK_READ,
    "schema": RISK_READ,
    "search": RISK_READ,
    "show": RISK_READ,
    "specs": RISK_READ,
    "stat": RISK_READ,
    "stats": RISK_READ,
    "status": RISK_READ,
    "ssh-config": RISK_READ,
    "ssh-keys": RISK_READ,
    "test": RISK_READ,
    "url": RISK_READ,
    "validate": RISK_READ,
    "versions": RISK_READ,
    "wait": RISK_READ,
    "whoami": RISK_READ,
    # Creates or modifies something: a platform resource, a config file, a remote file.
    "add": RISK_WRITE,
    "batch": RISK_WRITE,
    "create": RISK_WRITE,
    "deploy": RISK_WRITE,
    "exec": RISK_WRITE,
    "init": RISK_WRITE,
    "mkdir": RISK_WRITE,
    "mv": RISK_WRITE,
    "preheat": RISK_WRITE,
    "push": RISK_WRITE,
    "put": RISK_WRITE,
    "register": RISK_WRITE,
    "remove": RISK_WRITE,
    "run": RISK_WRITE,
    "save": RISK_WRITE,
    "scale": RISK_WRITE,
    "scp": RISK_WRITE,
    "select": RISK_WRITE,
    "set": RISK_WRITE,
    "ssh": RISK_WRITE,
    "start": RISK_WRITE,
    "sync": RISK_WRITE,
    "unset": RISK_WRITE,
    "update": RISK_WRITE,
    "upload": RISK_WRITE,
    "use": RISK_WRITE,
    # Destroys or interrupts something that exists. These are the commands that need `--yes`.
    "clear": RISK_HIGH_RISK_WRITE,
    "delete": RISK_HIGH_RISK_WRITE,
    "reset": RISK_HIGH_RISK_WRITE,
    "rm": RISK_HIGH_RISK_WRITE,
    "stop": RISK_HIGH_RISK_WRITE,
}

#: Path -> level, for the commands a verb would get wrong. Keep this list short and each entry
#: explained: it is the part of the registry that is a judgement rather than a rule.
RISK_OVERRIDES: dict[str, str] = {
    # The caller selects the HTTP method or JavaScript, so this table cannot see the actual effect.
    "api": RISK_WRITE,
    # These commands print by default but their output/install options write local files.
    "config env": RISK_WRITE,
    "tunnel ssh-config": RISK_WRITE,
    # Scaling to zero removes all serving capacity; positive replica counts skip the confirmation gate.
    "model scale": RISK_HIGH_RISK_WRITE,
    # The name says "probe", the call writes a remote file before removing it.
    "storage probe": RISK_WRITE,
}

#: Path -> one-line explanation, surfaced as `risk_note` by `inspire schema <path>`. Use it where the
#: level alone would mislead in either direction.
RISK_NOTES: dict[str, str] = {
    "api": (
        "A raw GET is normally read-only; other methods and eval can mutate platform state. The "
        "endpoint, Action, or evaluated JavaScript determines the actual effect, which the caller "
        "controls, so the whole command carries write risk."
    ),
    "config env": (
        "Without --output this only prints a template. --output writes the named local file; "
        "replacing an existing file additionally requires --force."
    ),
    "tunnel ssh-config": (
        "Without --install this only prints SSH configuration. --install adds or updates entries "
        "in ~/.ssh/config."
    ),
    "model scale": (
        "--replicas 0 interrupts all inference capacity and requires --yes/--force outside an "
        "interactive TTY. Positive replica counts are not blocked by that confirmation gate."
    ),
    "storage probe": (
        "Writes and then deletes a throwaway object on the remote to prove the WebDAV channel works. "
        "Nothing of yours is touched, but the call is not read-only."
    ),
    "sync": (
        "Writes the selected commit onto the Bridge worktree. With --force it hard-resets the "
        "Bridge branch to that commit and discards uncommitted work there; passing --force is the "
        "confirmation, so no second prompt follows."
    ),
}

#: Path -> replacement guidance, for commands that still work but should no longer be chosen.
#: Keyed and read exactly like the registries above, so a deprecation is one line here rather
#: than a label typed into the command's own help. No entry claims a removal version: a note may
#: only say the command may be removed in a future version, which is the strongest statement the
#: registry can honestly make before an actual release decision.
DEPRECATION_NOTES: dict[str, str] = {
    "run": (
        "Use `inspire job create ...` instead; sync code with `inspire sync` explicitly, and read "
        "logs with `inspire job logs`. May be removed in a future version."
    ),
    "job quota": (
        "Compatibility view of the platform's Scheduling Offers; use `inspire job offers` instead. "
        "May be removed in a future version."
    ),
}


def normalize_path(command_path: str) -> str:
    """Collapse a command path to the single-space form the registry is keyed by."""
    return " ".join(command_path.split())


def explicit_risk(command_path: str) -> Optional[str]:
    """The level the registry actually assigns, or None when nothing here classifies this path.

    None is the answer a caller must not present as a level: it means an unrecognised command and the
    caller is looking at a guess. See the module docstring for who is supposed to fail on it.
    """
    path = normalize_path(command_path)
    if path in RISK_OVERRIDES:
        return RISK_OVERRIDES[path]
    for segment in reversed(path.split()):
        level = VERB_RISK.get(segment) or VERB_RISK.get(segment.split("-")[0])
        if level:
            return level
    return None


def risk_for(command_path: str) -> str:
    """The level of a **leaf** command path; total, so it never asks the caller to handle a gap.

    Prefer ``explicit_risk`` when a missing classification is itself the finding.
    """
    return explicit_risk(command_path) or DEFAULT_RISK


def risk_of(command: click.Command, command_path: str) -> str:
    """The level of one command object: its own for a leaf, the worst of its leaves for a group."""
    subcommands = getattr(command, "commands", None)
    if subcommands:
        worst = max(
            (risk_of(sub, f"{command_path} {name}".strip()) for name, sub in subcommands.items()),
            key=_SEVERITY.__getitem__,
        )
        return worst
    return risk_for(command_path)


def risk_note(command_path: str) -> Optional[str]:
    """Why a level was chosen, when the level alone would mislead."""
    return RISK_NOTES.get(normalize_path(command_path))


def deprecation_note(command_path: str) -> Optional[str]:
    """Replacement guidance when this path is deprecated, or None when it is not.

    A non-None answer means the command still works and stays registered, but a caller should be
    pointed at its replacement. ``apply_risk_labels`` is what turns a note into a ``[deprecated]``
    help tag and a one-time invocation warning; ``inspire schema`` surfaces it as
    ``deprecated_note``.
    """
    return DEPRECATION_NOTES.get(normalize_path(command_path))


def iter_commands(root: click.Command, prefix: str = "") -> Iterator[tuple[str, click.Command]]:
    """Yield ``(path, command)`` for every command and group below ``root``.

    Depth-first in click's own registration order, root excluded. ``inspire schema`` and the
    coverage test both walk with this so they cannot disagree about what the CLI offers.
    """
    for name, command in getattr(root, "commands", {}).items():
        path = f"{prefix} {name}".strip()
        yield path, command
        if getattr(command, "commands", None):
            yield from iter_commands(command, path)


class _RiskLabelledCommand(click.Command):
    """Render this command's registry metadata: its risk level, and its deprecation when it has one.

    Both are looked up from ``risk_paths`` on every render and every invocation rather than baked
    into a help string, so the registries stay the single source and no command's docstring
    mentions risk or deprecation at all.
    """

    #: Every path this command answers to. An alias - `image info` *is* `image search`, one object
    #: registered twice - therefore shows the worst level among its names rather than whichever
    #: registration ran last, and is deprecated if any of its names is.
    risk_paths: tuple[str, ...] = ()

    def _risk_label(self) -> str:
        levels = {risk_of(self, path) for path in self.risk_paths} or {DEFAULT_RISK}
        return f"[{max(levels, key=_SEVERITY.__getitem__)}]"

    def _deprecation(self) -> Optional[tuple[str, str]]:
        """The ``(path, note)`` of the first deprecated path this command answers to."""
        for path in self.risk_paths:
            note = deprecation_note(path)
            if note is not None:
                return path, note
        return None

    def format_help_text(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if not self.help:
            super().format_help_text(ctx, formatter)
            return
        label = self._risk_label()
        deprecation = self._deprecation()
        if deprecation is not None:
            label = f"[deprecated] {label}"
        original = self.help
        try:
            self.help = f"{label} {original}"
            if deprecation is not None:
                self.help = f"{self.help}\n\nDEPRECATED: {deprecation[1]}"
            super().format_help_text(ctx, formatter)
        finally:
            self.help = original

    def get_short_help_str(self, limit: int = 45) -> str:
        short = super().get_short_help_str(limit)
        deprecation = self._deprecation()
        if deprecation is None:
            return f"{self._risk_label()} {short}".strip()
        return f"[deprecated] {self._risk_label()} {short} {deprecation[1]}".strip()

    def invoke(self, ctx: click.Context) -> Any:
        """Warn once on stderr when a deprecated command is actually invoked, then run it.

        ``--help`` never arrives here - Click's help option prints and exits while arguments are
        still being parsed - and ``inspire schema`` only inspects metadata, so reading about a
        deprecation is silent by construction. A command invoked as a bare object has no parent
        context: the registry keys on the path from the CLI root, and a bare object was never
        invoked under that path, so standalone Click tests of the object are left alone.
        """
        deprecation = self._deprecation()
        if deprecation is not None and ctx.parent is not None:
            self._emit_deprecation_warning(ctx, deprecation)
        return super().invoke(ctx)

    def _emit_deprecation_warning(self, ctx: click.Context, deprecation: tuple[str, str]) -> None:
        """One warning, on stderr, naming the deprecated path and what replaces it.

        The CLI's ``Context`` rides on ``ctx.obj`` for anything invoked through `main`, but a
        standalone group test may hand us some other object (or none), so the nearest real
        ``Context`` is looked up and a bare one substituted when there is none. Local imports:
        this registry loads before the output layer on the CLI's import path, and only this one
        emit needs it.
        """
        from inspire.cli.context import Context as CLIContext
        from inspire.cli.utils.output import emit_warning

        path, note = deprecation
        emit_warning(
            ctx.find_object(CLIContext) or CLIContext(),
            f"`inspire {path}` is deprecated. {note}",
        )


_tagged_classes: dict[type, type] = {}


def _tagged_class(cls: type) -> type:
    """``cls`` with the registry mixin added, built once per command class.

    Derived from whatever class the command actually is rather than from ``click.Command``, so a
    command defined with its own subclass keeps working and only gains the registry behaviour. An
    already-labelled class comes back unchanged, which keeps this whole operation safe to run twice.
    """
    if issubclass(cls, _RiskLabelledCommand):
        return cls
    tagged = _tagged_classes.get(cls)
    if tagged is None:
        tagged = type(f"RiskLabelled{cls.__name__}", (_RiskLabelledCommand, cls), {})
        _tagged_classes[cls] = tagged
    return tagged


def apply_risk_labels(root: click.Command, prefix: str = "") -> None:
    """Wire ``root``'s whole tree to the registries: rendered help, and deprecation warnings. Call
    once, after every command is registered.

    The root itself stays unlabeled: it is not a thing an agent chooses between, and a level on
    `inspire --help` would read as a warning about the CLI.
    """
    for path, command in iter_commands(root, prefix):
        command.__class__ = _tagged_class(type(command))
        labelled = cast(_RiskLabelledCommand, command)
        if path not in labelled.risk_paths:
            labelled.risk_paths = (*labelled.risk_paths, path)
