"""Detached ``notebook exec`` records: work that lives on the notebook, not on the channel.

The Jupyter terminal is a channel this CLI does not own. Measured on 2026-11-07 on one 8xCPU
notebook: a silent 400s command was abandoned after 267s of a 600s ``--timeout``, a command that
printed every 10s was abandoned after 327s, and a follow-up exec showed the abandoned command
*killed*, not merely unwatched - giving up deletes the terminal, and the command runs in that
terminal's shell. The same measurement showed the way out: a command started with
``setsid nohup ... </dev/null &`` came back in 3s and was still alive 45s later, after the terminal
that launched it had been deleted.

So this module builds the remote scripts that make the channel irrelevant. The launcher starts the
command in a new session, pointing its output at files on the notebook's own filesystem and its exit
status at a file, and returns in seconds. A read is a fresh, short-lived fetch of those files from a
byte offset, and can wait on the notebook for bytes to arrive rather than polling them from here.
Each record is one directory under :data:`RECORD_ROOT`, discoverable with a plain ``ls`` - nothing on
the caller's machine is needed to read an exec back.

The record directory (``/tmp/inspire-exec/<exec-id>/``):

``stdout``
    Everything the command wrote, stdout and stderr together (``2>&1``). One file means one byte
    offset, which is what ``--attach --after`` takes.
``status``
    The command's exit code, written when it finishes. Its absence is why ``--attach`` asks whether
    the recorded pid is alive rather than assuming "no status" means "still running".
``pid``
    The detached shell's own process id, written by that shell as its first act.
``command``
    The caller's own command text, and the only text written to the record - it is also exactly what
    runs, because the ``[remote_env]`` prefix is applied by the launching shell and inherited through
    fork/exec rather than being written down. That prefix can hold secrets expanded from the local
    environment, and a file in ``/tmp`` outlives the call that wrote it.
``runner.sh``
    The small script the detached shell runs: write ``pid``, run the command, write ``status``.
``pty``
    Only written when a PTY was asked for (plain ``notebook exec``; ``--detach`` does not ask). ``1``
    means the command ran under a far-side terminal, ``0`` that the image could not give it one and
    it ran on a plain pipe. Every read reports it, because the stream model is a fact about the bytes
    in ``stdout`` and the process reading them is usually not the one that started them.

Reads are byte-exact and idempotent. The remote side answers with ``wc -c`` for the size, so the
offsets are positions in the file whatever the terminal did to the bytes in between, and the payload
travels base64-encoded because a PTY rewrites control characters and cannot be trusted to carry
arbitrary bytes.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
import secrets
from dataclasses import dataclass
from typing import Any, Optional

#: Where records live on the notebook. ``/tmp`` because it needs no privilege, is present in every
#: notebook image this path is used against, and is not a user's data directory.
RECORD_ROOT = "/tmp/inspire-exec"

#: Exec ids are this prefix plus 16 hex digits, so ``ls /tmp/inspire-exec`` shows them at a glance
#: and a mistyped id cannot name a path.
ID_PREFIX = "nbx-"
_ID_RE = re.compile(r"^nbx-[0-9a-f]{16}$")

#: Raw bytes of the record one read fetches. Kept well under the transport's 256 KiB line cap even
#: after base64 (48 KiB -> 64 KiB) and small enough that one read is a short call.
ATTACH_CHUNK_BYTES = 49152

#: Seconds one far-side wait sleeps between two looks at the record. The read that waits costs the
#: notebook a ``wc`` and two ``cat`` per poll, so this is the trade between per-output latency and
#: load while a command says nothing: 0.2s is what makes a tail feel live without a busy loop.
READ_POLL_SECONDS = 0.2

#: States :func:`parse_attach_output` can report. ``starting`` is the sub-second window in which the
#: record directory exists and the detached shell has not written its pid yet; ``vanished`` is a
#: pid that is gone with no status written - killed, or a container that restarted.
ATTACH_STATES = ("running", "exited", "starting", "vanished", "missing")

#: ``exitReason``-style protocol failures, reported rather than papered over.
ERROR_NO_SUCH_RECORD = "no-such-record"
ERROR_OFFSET_BEYOND_END = "offset-beyond-end"
ERROR_UNREADABLE_RESPONSE = "unreadable-response"
ERROR_CORRUPT_PAYLOAD = "corrupt-payload"
ERROR_LENGTH_MISMATCH = "length-mismatch"


class AttachProtocolError(ValueError):
    """A read came back in a shape this module cannot honour without guessing."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def new_exec_id() -> str:
    """A fresh exec id: unguessable, path-safe, and sortable by eye under ``ls``."""
    return f"{ID_PREFIX}{secrets.token_hex(8)}"


def validate_exec_id(value: str) -> str:
    """Return *value* as an exec id, or raise ``ValueError``.

    The id comes back from a terminal and goes into a shell command line and a path, so anything
    outside the alphabet this module mints is refused here rather than escaped downstream.
    """
    candidate = str(value or "").strip()
    if not _ID_RE.match(candidate):
        raise ValueError(
            f"{candidate!r} is not an exec id; they look like "
            f"'{ID_PREFIX}{'0' * 16}' (see `ls {RECORD_ROOT}` on the notebook)"
        )
    return candidate


def record_dir(exec_id: str) -> str:
    """The record directory of *exec_id* on the notebook."""
    return f"{RECORD_ROOT}/{validate_exec_id(exec_id)}"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


#: The shell the far-side command runs under: ``--noprofile --norc`` so an image's login script
#: cannot print its own banner into the record or fail on a non-interactive stdin.
_SHELL_LINES = (
    "if [ -x /bin/bash ]; then",
    '  /bin/bash --noprofile --norc -lc "$__inspire_command"',
    "else",
    '  /bin/sh -lc "$__inspire_command"',
    "fi",
)

#: How a runner asks for a PTY. util-linux ``script`` is the only widely-installed tool that gives a
#: command a controlling terminal from a shell script: ``-c`` runs it under one, ``-e`` propagates
#: its exit code (including 128+signal), ``-f`` flushes every write so the record grows while the
#: command runs, and ``-q`` keeps ``script``'s own "Script started/done" lines out of it. The
#: typescript goes to ``/dev/null`` because the record file is already being written by the runner's
#: own stdout redirect - two writers into one file would mean two offsets.
#:
#: The probe is not decoration: it runs a command through ``script`` with the same flags, so an
#: image without util-linux ``script`` (BSD ``script`` takes none of them) or without ``/dev/ptmx``
#: falls to the plain pipe instead of producing a run with no output. It allocates a real PTY, which
#: is the thing that can still fail after ``command -v`` succeeds.
_PTY_PROBE = "script -q -e -f -c 'exit 0' /dev/null </dev/null >/dev/null 2>&1"


def _build_runner_script(directory: str, command: str, *, pty: bool) -> str:
    """The script the detached shell runs: record itself, run the command, record the status.

    With *pty*, the command runs under a far-side PTY when the image can give it one, and the runner
    writes what it actually got to ``<record>/pty`` - the one place a later read can learn whether the
    bytes it is serving came off a terminal. That marker is written *before* ``pid`` deliberately:
    the launcher returns as soon as the pid appears, so a record that is observable at all already
    says what its stream is.

    ``script -c`` hands its argument to ``/bin/sh``, which does not inherit shell variables, so the
    command text reaches the PTY child through the environment rather than through a command string
    that would have to quote arbitrary text safely. The text is the caller's own command - the same
    text the record already keeps in ``command`` - never the ``[remote_env]`` prefix.
    """
    decode = f"__inspire_command=$(printf %s {_b64(command)} | base64 -d)"
    if pty:
        body = [
            "__inspire_pty=0",
            f"if command -v script >/dev/null 2>&1 && {_PTY_PROBE}; then __inspire_pty=1; fi",
            f"printf '%s' \"$__inspire_pty\" > {directory}/pty",
            f"echo $$ > {directory}/pid",
            "export __inspire_command",
            'if [ "$__inspire_pty" = 1 ]; then',
            "  if [ -x /bin/bash ]; then",
            "    __inspire_ptycmd='/bin/bash --noprofile --norc -lc \"$__inspire_command\"'",
            "  else",
            "    __inspire_ptycmd='/bin/sh -lc \"$__inspire_command\"'",
            "  fi",
            '  script -q -e -f -c "$__inspire_ptycmd" /dev/null',
            "else",
            "  if [ -x /bin/bash ]; then",
            '    /bin/bash --noprofile --norc -lc "$__inspire_command"',
            "  else",
            '    /bin/sh -lc "$__inspire_command"',
            "  fi",
            "fi",
        ]
        lines = [decode, *body]
    else:
        lines = [f"echo $$ > {directory}/pid", decode, *_SHELL_LINES]
    lines += [
        "__inspire_rc=$?",
        f"printf '%s\\n' \"$__inspire_rc\" > {directory}/status",
        "",
    ]
    return "\n".join(lines)


def build_detach_command(exec_id: str, command: str, *, pty: bool = False) -> str:
    """The remote script that starts *command* detached and returns immediately.

    ``command`` is the caller's own text, and it is the only text this writes anywhere: the
    ``[remote_env]`` prefix rides in front of *this* script instead, so the launching shell applies
    the exports and the detached process inherits them through fork/exec. Prefixing the runner
    instead would persist base64 of a prefix that can hold secrets expanded from the local
    environment in a file that outlives the call - and base64 of a secret is the secret.

    The script reports the pid only after the detached shell has written it, which is what lets
    ``--detach`` promise the work is running rather than that it was asked nicely. *pty* asks for a
    far-side PTY; see :func:`_build_runner_script` for what happens when the image cannot.
    """
    directory = record_dir(exec_id)
    runner = _build_runner_script(directory, command, pty=pty)
    return "\n".join(
        [
            f"__inspire_dir='{directory}'",
            "if ! command -v setsid >/dev/null 2>&1; then",
            "  printf '%s\\n' 'inspire-exec: setsid is not available in this container, so a "
            "command started here could not outlive this terminal; refusing to start it.' >&2",
            "  exit 1",
            "fi",
            'if ! mkdir -p -- "$__inspire_dir" 2>/dev/null; then',
            "  printf 'inspire-exec: cannot create %s\\n' \"$__inspire_dir\" >&2",
            "  exit 1",
            "fi",
            f'printf %s {_b64(runner)} | base64 -d > "$__inspire_dir/runner.sh" || exit 1',
            f"printf '%s\\n' \"$(printf %s {_b64(command)} | base64 -d)\" "
            '> "$__inspire_dir/command" || exit 1',
            ': > "$__inspire_dir/stdout" || exit 1',
            # setsid puts the command in a new session, so deleting this terminal's process group
            # cannot reach it; nohup and </dev/null cover the hang-up and the stdin read that would
            # otherwise stop it. This is the exact shape measured to survive terminal deletion.
            'setsid nohup /bin/sh "$__inspire_dir/runner.sh" '
            '>"$__inspire_dir/stdout" 2>&1 </dev/null &',
            # Wait (briefly) for the detached shell to record itself, so a launch that failed at
            # once is reported as a failure instead of as a successful detach of nothing.
            "__inspire_try=0",
            'while [ ! -s "$__inspire_dir/pid" ] && [ "$__inspire_try" -lt 20 ]; do',
            "  __inspire_try=$(($__inspire_try + 1))",
            "  sleep 0.1",
            "done",
            'if [ ! -s "$__inspire_dir/pid" ]; then',
            "  printf 'inspire-exec: the detached command left no pid in %s; its output so far "
            'is in %s/stdout\\n\' "$__inspire_dir" "$__inspire_dir" >&2',
            "  exit 1",
            "fi",
            f"printf 'INSPIRE_EXEC id={exec_id} dir=%s pid=%s\\n' "
            '"$__inspire_dir" "$(cat "$__inspire_dir/pid" 2>/dev/null)"',
            "",
        ]
    )


def build_attach_command(
    exec_id: str,
    *,
    after: int,
    tag: str,
    chunk: int = ATTACH_CHUNK_BYTES,
    wait: float = 0.0,
) -> str:
    """The remote script that reads one window of a record, from byte *after* onwards.

    Every number it reports comes from ``wc -c`` on the file, so ``next`` is a real position in the
    log rather than a count of what survived the terminal. The payload is base64 for the same
    reason: a PTY turns ``\\n`` into ``\\r\\n`` and eats ``\\r``, which is fine for prose and not
    fine for a resume cursor.

    *tag* prefixes the header lines and holds characters outside the base64 alphabet, so nothing the
    record contains can impersonate a header line. The payload is last, after the ``body`` line, so
    the header is never chased by bytes that may still be arriving.

    *wait* is how long this read may block on the notebook for the window to become non-empty,
    returning at once when bytes arrive or the record stops moving (exited, or its process gone).
    It is what lets one call carry minutes of output instead of one poll: waiting where the bytes
    are costs one round trip per burst rather than one per poll interval. ``0`` - what ``--attach``
    sends - answers immediately.
    """
    directory = record_dir(exec_id)
    if after < 0:
        raise ValueError("--after cannot be negative")
    if chunk < 1:
        raise ValueError("the read window must be at least one byte")
    if wait < 0:
        raise ValueError("the read's wait budget cannot be negative")
    lines = [
        f"__inspire_dir='{directory}'",
        f"__inspire_tag='{tag}'",
        f"__inspire_after={int(after)}",
        f"__inspire_chunk={int(chunk)}",
        # One look at the record, re-run by the wait loop below. Size, exit status, pid, stream
        # model and the state derived from them always move together, so they are never read from
        # two different instants of a file that is still growing.
        "__inspire_scan() {",
        '  __inspire_size=$(wc -c < "$__inspire_dir/stdout" 2>/dev/null)',
        "  __inspire_size=${__inspire_size:-0}",
        "  __inspire_size=$((__inspire_size + 0))",
        '  __inspire_code=$(cat "$__inspire_dir/status" 2>/dev/null)',
        '  __inspire_pid=$(cat "$__inspire_dir/pid" 2>/dev/null)',
        '  __inspire_pty=$(cat "$__inspire_dir/pty" 2>/dev/null)',
        '  if [ -n "$__inspire_code" ]; then',
        "    __inspire_state=exited",
        '  elif [ -n "$__inspire_pid" ] && kill -0 "$__inspire_pid" 2>/dev/null; then',
        "    __inspire_state=running",
        '  elif [ -n "$__inspire_pid" ]; then',
        "    __inspire_state=vanished",
        "  else",
        "    __inspire_state=starting",
        "  fi",
        "}",
        'if [ ! -d "$__inspire_dir" ]; then',
        "  printf '%s state missing\\n' \"$__inspire_tag\"",
        "  printf '%s body\\n' \"$__inspire_tag\"",
        "  exit 0",
        "fi",
        "__inspire_scan",
    ]
    if wait > 0:
        lines += [
            f"__inspire_poll={READ_POLL_SECONDS}",
            f"__inspire_left={int(math.ceil(wait / READ_POLL_SECONDS))}",
            'while [ "$__inspire_left" -gt 0 ]; do',
            '  case "$__inspire_state" in exited|vanished) break ;; esac',
            '  if [ "$__inspire_size" -gt "$__inspire_after" ]; then break; fi',
            '  sleep "$__inspire_poll"',
            "  __inspire_left=$((__inspire_left - 1))",
            "  __inspire_scan",
            "done",
        ]
    lines += [
        "__inspire_avail=$((__inspire_size - __inspire_after))",
        "__inspire_error=''",
        'if [ "$__inspire_avail" -lt 0 ]; then',
        "  __inspire_error=offset-beyond-end",
        "  __inspire_avail=0",
        "fi",
        "__inspire_len=$__inspire_avail",
        'if [ "$__inspire_len" -gt "$__inspire_chunk" ]; then',
        "  __inspire_len=$__inspire_chunk",
        "fi",
        "__inspire_next=$((__inspire_after + __inspire_len))",
        "__inspire_more=false",
        'if [ "$__inspire_size" -gt "$__inspire_next" ]; then',
        "  __inspire_more=true",
        "fi",
        'printf \'%s state %s\\n\' "$__inspire_tag" "$__inspire_state"',
        'if [ -n "$__inspire_code" ]; then',
        '  printf \'%s code %s\\n\' "$__inspire_tag" "$__inspire_code"',
        "fi",
        # Written by the runner before the pid, so a record that is observable at all already says
        # whether its bytes came off a PTY. Absent means the answer is not "no PTY" but "not said",
        # which the reader reports as the plain merged model rather than guessing.
        'if [ -n "$__inspire_pty" ]; then',
        '  printf \'%s pty %s\\n\' "$__inspire_tag" "$__inspire_pty"',
        "fi",
        'if [ -n "$__inspire_error" ]; then',
        '  printf \'%s error %s\\n\' "$__inspire_tag" "$__inspire_error"',
        "fi",
        'printf \'%s size %s\\n\' "$__inspire_tag" "$__inspire_size"',
        'printf \'%s after %s\\n\' "$__inspire_tag" "$__inspire_after"',
        'printf \'%s next %s\\n\' "$__inspire_tag" "$__inspire_next"',
        'printf \'%s more %s\\n\' "$__inspire_tag" "$__inspire_more"',
        "printf '%s body\\n' \"$__inspire_tag\"",
        'if [ "$__inspire_len" -gt 0 ]; then',
        '  tail -c +$((__inspire_after + 1)) -- "$__inspire_dir/stdout" 2>/dev/null '
        "| head -c \"$__inspire_len\" | base64 | tr -d '\\n'",
        "  printf '\\n'",
        "fi",
        "",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class AttachWindow:
    """One read of a detached exec's record.

    ``data`` is exactly ``next_offset - after`` bytes of the record, and the only thing that makes
    ``next_offset`` usable as a resume cursor. ``held_back`` is the tail of that payload this call
    could not decode as complete UTF-8: showing it would print a replacement character for half a
    character, so it stays behind and ``next_offset`` is pulled back before it, which costs a
    re-read of at most three bytes and never loses or repeats one.

    ``pty`` is what the runner said the command ran under, read out of the record rather than
    remembered by whoever started it - which is what lets a read from a different process report the
    stream model truthfully. ``None`` means the record never said (``--detach`` never asks, and a
    record left by an older client predates the marker), which is a plain pipe but not the runner
    *saying* the image could not give a PTY; only ``False`` is that, and only ``False`` earns the
    warning that says so.
    """

    exec_id: str
    state: str
    exit_code: Optional[int]
    after: int
    next_offset: int
    size: int
    more: bool
    data: bytes
    held_back: bytes = b""
    pty: Optional[bool] = None
    error: Optional[str] = None

    @property
    def text(self) -> str:
        """The part of this window that is complete UTF-8, decoded for display.

        Genuinely invalid bytes (binary output, not a truncated character) decode with
        replacements: text mode is for a human, and :attr:`data` and the JSON ``stdout_b64`` carry
        the exact bytes.
        """
        return self.data.decode("utf-8", errors="replace")

    @property
    def finished(self) -> bool:
        return self.state == "exited"

    @property
    def unresolved(self) -> bool:
        """A run this call can neither see ending nor prove has ended."""
        return self.state == "vanished"


def new_read_tag() -> str:
    """A header tag for one read: underscores keep it out of base64, hex keeps it unique."""
    return f"__INSPIRE_EXEC_{secrets.token_hex(8)}__"


def _split_complete_utf8(data: bytes) -> tuple[bytes, bytes]:
    """Split *data* into (complete, incomplete-tail) so ``--after`` never lands mid-character."""
    if not data:
        return data, b""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        return data, b""
    for cut in range(1, min(4, len(data)) + 1):
        head = data[: len(data) - cut]
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            continue
        return head, data[len(data) - cut :]
    return data, b""


#: Longest tail held back waiting for an escape sequence to arrive. A CSI sequence - colours, cursor
#: moves, the whole vocabulary a PTY program writes - is a handful of bytes; a longer run of framing
#: is data, and printing it beats withholding it indefinitely.
_FRAME_HOLD_MAX = 16

#: An ESC, an optional ``[``, and parameters/intermediates - with the final byte that ends the
#: sequence not yet arrived. (``[0-?]`` is the parameter range, ``[ -/]`` the intermediates; a
#: sequence that had reached its final byte in ``[@-~]`` cannot match this to the end of data.)
_INCOMPLETE_CSI_RE = re.compile(rb"\x1b\[?[0-?]*[ -/]*$")


def _split_complete_frame(data: bytes) -> tuple[bytes, bytes]:
    """Split off an unfinished escape sequence at the end of *data*.

    A window boundary can land between ``\\x1b[3`` and the ``8m`` that completes it. That byte pair
    is not displayable and not strippable (the pattern has no final byte yet), so it is held back and
    re-read with whatever follows - the same mechanism :func:`_split_complete_utf8` uses for a
    character, for the same reason: showing half of it is worse than reading it twice.
    """
    if not data:
        return data, b""
    tail = data[-_FRAME_HOLD_MAX:]
    start = tail.rfind(b"\x1b")
    if start < 0:
        return data, b""
    candidate = tail[start:]
    if candidate == b"\x1b" or _INCOMPLETE_CSI_RE.match(candidate):
        return data[: len(data) - len(candidate)], candidate
    return data, b""


#: A record in one of these states produces no more bytes, so nothing may be held back for a
#: continuation that will never come: the held-back tail would be dropped rather than delayed.
FINAL_STATES = ("exited", "vanished")


def _split_incomplete_tail(data: bytes, state: str) -> tuple[bytes, bytes]:
    """The bytes of *data* that may be shown now, and the tail that must be read again."""
    if state in FINAL_STATES:
        return data, b""
    head, held = _split_complete_utf8(data)
    head, held_frame = _split_complete_frame(head)
    return head, held_frame + held


def parse_attach_output(text: str, *, exec_id: str, tag: str) -> AttachWindow:
    """Read back one window from the text the terminal handed back.

    Header lines are recognised only while they carry this call's tag; the first line that does not
    ends the header, and everything after it is the payload. Since the payload is base64 - an
    alphabet with no space and no underscore in it - no content the record holds can forge a header.
    """
    fields: dict[str, str] = {}
    body: list[str] = []
    header_over = False
    field_re = re.compile(rf"^{re.escape(tag)} ([a-z][a-z-]*) (.*)$")
    for line in str(text or "").split("\n"):
        if header_over:
            body.append(line)
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == f"{tag} body":
            header_over = True
            continue
        match = field_re.match(stripped)
        if match:
            fields[match.group(1)] = match.group(2).strip()
            continue
        raise AttachProtocolError(
            ERROR_UNREADABLE_RESPONSE,
            f"the read came back without a {tag} header: {stripped[:120]!r}",
        )
    if not header_over or "state" not in fields:
        raise AttachProtocolError(
            ERROR_UNREADABLE_RESPONSE,
            f"the read carried no {tag} state line",
        )

    state = fields["state"]
    error = fields.get("error") or None
    raw_pty = str(fields.get("pty", "")).strip()
    pty = None if raw_pty not in ("0", "1") else raw_pty == "1"
    if state == "missing":
        error = error or ERROR_NO_SUCH_RECORD
    size = _as_int(fields.get("size"), 0)
    after = _as_int(fields.get("after"), 0)
    declared_next = _as_int(fields.get("next"), after)
    more = str(fields.get("more", "")).strip().lower() == "true"
    exit_code: Optional[int] = None
    if "code" in fields:
        raw_code = fields["code"].strip()
        try:
            exit_code = int(raw_code)
        except ValueError as exc:
            raise AttachProtocolError(
                ERROR_UNREADABLE_RESPONSE, f"status file holds {raw_code!r}, not an exit code"
            ) from exc

    payload = re.sub(r"\s+", "", "\n".join(body))
    try:
        data = base64.b64decode(payload, validate=True) if payload else b""
    except (binascii.Error, ValueError) as exc:
        raise AttachProtocolError(
            ERROR_CORRUPT_PAYLOAD, f"the payload is not base64 ({exc})"
        ) from exc
    if len(data) != declared_next - after:
        raise AttachProtocolError(
            ERROR_LENGTH_MISMATCH,
            f"the read delivered {len(data)} bytes for a window of "
            f"{declared_next - after}; refusing to move the cursor",
        )

    complete, held_back = _split_incomplete_tail(data, state)
    return AttachWindow(
        exec_id=validate_exec_id(exec_id),
        state=state,
        exit_code=exit_code,
        after=after,
        # The cursor only ever advances over bytes the caller was handed complete: the held-back
        # tail is re-read by the next call rather than shown as half a character.
        next_offset=after + len(complete),
        size=size,
        more=more or bool(held_back),
        data=complete,
        held_back=held_back,
        pty=pty,
        error=error,
    )


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


__all__ = [
    "ATTACH_CHUNK_BYTES",
    "ATTACH_STATES",
    "AttachProtocolError",
    "AttachWindow",
    "READ_POLL_SECONDS",
    "RECORD_ROOT",
    "build_attach_command",
    "build_detach_command",
    "new_exec_id",
    "new_read_tag",
    "parse_attach_output",
    "record_dir",
    "validate_exec_id",
]
