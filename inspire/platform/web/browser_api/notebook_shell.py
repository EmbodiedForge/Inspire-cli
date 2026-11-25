"""Reliable shell command execution through a notebook Jupyter terminal."""

from __future__ import annotations

import base64
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from inspire.platform.web.browser_api.playwright_notebooks import run_command_in_notebook
from inspire.platform.web.session import WebSession

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1b\\)|[@-_])")

#: The transport's ``exitReason`` values that mean "the channel ended" rather than "our clock ran
#: out". The transport records this because the caller cannot recover it from anything else: a
#: closed socket and an expired deadline both end the read loop with the same partial buffer.
CHANNEL_LOST_REASONS = frozenset({"channel-closed", "send-failed"})
#: The transport's own words for "the read's clock ran out while the socket was still there". It is
#: the only evidence that survives a read whose output never reached us, which is why the failure
#: below is sorted by this before it is sorted by anything in the captured text.
DEADLINE_EXIT_REASON = "deadline"


def _elapsed_ms(diagnostics: Mapping[str, Any]) -> Optional[int]:
    """The transport's own elapsed reading, or None when it did not take one.

    Read from the transport rather than from this wrapper's clock, because the number a caller needs
    is how long the channel stayed open, and this wrapper only sees the whole call.
    """
    raw = diagnostics.get("elapsed_ms")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


@dataclass(frozen=True)
class NotebookShellResult:
    """Normalized result from a Jupyter terminal shell command.

    ``stderr`` is None, never an empty string: the Jupyter terminal is a PTY over one WebSocket, so the
    shell's stderr is merged into the captured text before we see it and there is no separate channel to
    report. ``stream_model`` says so wherever the result is published (issue #51).
    """

    notebook_id: str
    stdout: str
    stderr: str | None
    returncode: int
    duration_ms: int
    diagnostics: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "notebook_id": self.notebook_id,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stream_model": "pty-merged",
            "returncode": self.returncode,
            "duration_ms": self.duration_ms,
            "diagnostics": self.diagnostics,
        }


def _strip_ansi(value: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", value).replace("\r", "")


def strip_terminal_framing(value: str) -> str:
    """One merged terminal stream in the form this CLI has always handed a caller.

    Public because the record-backed tail (``notebook exec`` without flags) serves PTY output from a
    file and must give back the same shape the live terminal stream gave back: colours and cursor
    moves were emitted *because* the program had a terminal, and the caller asked for the program's
    output, not its terminal's.
    """
    return _strip_ansi(value)


def _build_marked_command(command: str, marker: str) -> str:
    command_b64 = base64.b64encode(command.encode("utf-8")).decode("ascii")
    marker_b64 = base64.b64encode(marker.encode("ascii")).decode("ascii")
    return (
        f"__inspire_marker=$(printf %s {marker_b64} | base64 -d); "
        f"__inspire_command=$(printf %s {command_b64} | base64 -d); "
        "printf '%sS\\n' \"$__inspire_marker\"; "
        "if [ -x /bin/bash ]; then "
        '/bin/bash --noprofile --norc -lc "$__inspire_command"; '
        'else /bin/sh -lc "$__inspire_command"; fi; '
        "__inspire_rc=$?; "
        'printf \'\\n%sE %s\\n\' "$__inspire_marker" "$__inspire_rc"'
    )


class NotebookShellError(RuntimeError):
    """A notebook exec failure that already knows its own remedy."""


def _with_output(detail: str, output: str) -> str:
    if output.strip():
        return f"{detail}; output so far: {_clip(output)}"
    return detail


class NotebookShellTimeout(NotebookShellError):
    """The read's own clock ran out before the command reported completion.

    Named separately because the previous behaviour printed the start marker plus "is the notebook
    RUNNING?", which sends the reader to check a thing that is demonstrably working: the marker arriving
    *is* proof the session is alive and the command is running (issue #22).

    ``start_marker`` says whether the terminal ever echoed that marker. A read that spent its whole
    allowance without seeing one cannot claim the command started, and must not: it says what it got
    instead, which is nothing.
    """

    def __init__(self, timeout: int, output: str = "", *, start_marker: bool = True) -> None:
        self.timeout = timeout
        self.output = output
        self.start_marker = start_marker
        super().__init__(self._detail(timeout, output))

    def _detail(self, timeout: int, output: str) -> str:
        if not self.start_marker:
            return _with_output(
                f"the terminal sent nothing back for the whole {timeout}s the read was allowed, "
                "and never echoed the command's start",
                output,
            )
        return _with_output(f"the command started but did not finish within {timeout}s", output)


class NotebookShellChannelLost(NotebookShellTimeout):
    """The terminal stopped answering while the command was still running.

    A subclass of :class:`NotebookShellTimeout` so every caller that already treats a lost notebook
    exec as "not answered yet" (exit 14) keeps doing that; what changes is the sentence. The two
    answers send the caller to different remedies and one of them is not a remedy at all: measured
    on 2026-11-07 a silent command was abandoned after 267s of a 600s ``--timeout``, and the exec two
    seconds later showed the command dead - giving up deletes the terminal, and the command runs in
    that terminal's shell. Telling that caller to "raise --timeout" sends it away from the one knob
    that was never the problem.
    """

    def __init__(
        self,
        *,
        elapsed_ms: Optional[int],
        reason: str,
        channel_error: str = "",
        timeout: int = 0,
        output: str = "",
        start_marker: bool = True,
    ) -> None:
        self.elapsed_ms = elapsed_ms
        self.reason = reason
        self.channel_error = channel_error
        super().__init__(timeout, output, start_marker=start_marker)

    def _detail(self, timeout: int, output: str) -> str:
        if self.elapsed_ms is None:
            # No elapsed reading from the transport: say the channel closed and that the clock had
            # not run out, rather than printing the requested timeout where an elapsed time belongs.
            running = (
                "while the command was running"
                if self.start_marker
                else "before the command echoed anything"
            )
            head = (
                f"the notebook terminal channel closed {running} "
                f"(the {timeout}s --timeout had not expired)"
            )
        else:
            head = (
                "the notebook terminal channel closed after "
                f"{_format_seconds(self.elapsed_ms)} of the {timeout}s --timeout"
            )
        if self.channel_error:
            head = f"{head}: {self.channel_error}"
        return _with_output(head, output)


class NotebookShellUnreachable(NotebookShellError):
    """The terminal never echoed the start marker: the session, not the command, is the problem."""


def _clip(value: str, limit: int = 400) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit // 2]} … {collapsed[-limit // 2 :]}"


def _format_seconds(elapsed_ms: int) -> str:
    """An elapsed duration, spelled so it cannot be mistaken for a requested timeout."""
    seconds = max(int(elapsed_ms), 0) / 1000.0
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def _strip_markers(text: str, marker: str) -> str:
    """Remove our own framing. Markers are protocol, never program output.

    Also drops the terminal's echo of the wrapper itself. That echo carries `__inspire_command=$(printf %s
    <base64>)`, and the base64 decodes to the `[remote_env]` prefix this project promises never to echo -
    base64 of a secret is the secret. Live-verified leaking on 2026-09-28.
    """
    cleaned = re.sub(rf"\n?{re.escape(marker)}E [0-9]+\n?", "\n", text)
    cleaned = cleaned.replace(f"{marker}S", "")
    kept = [
        line
        for line in cleaned.split("\n")
        if "__inspire_marker=" not in line and "__inspire_command=" not in line
    ]
    return "\n".join(kept)


def _after_start_marker(cleaned: str, marker: str) -> str:
    """Everything the command produced: strictly after our start marker.

    The PTY echoes the input line before the command runs, so pre-marker text is terminal framing that may
    contain the encoded command. The success path has always sliced here; the partial-output path must too.
    """
    start = cleaned.rfind(f"{marker}S")
    if start < 0:
        return ""
    return cleaned[start + len(marker) + 1 :]


def _partial_output(cleaned: str, marker: str, start_marker: bool) -> str:
    """What the command itself had produced when a read ended, or "" when nothing says so.

    Sliced from after the start marker, because everything before it is terminal framing that may hold
    the base64 of the `[remote_env]` prefix (:func:`_strip_markers`). A read that never saw the marker
    reports no output rather than the terminal's own banner: nothing on that path is evidence the
    command wrote anything.
    """
    if not start_marker:
        return ""
    return _strip_markers(_after_start_marker(cleaned, marker), marker)


def _parse_marked_output(output: str, marker: str, timeout: int | None = None) -> tuple[str, int]:
    cleaned = _strip_ansi(output)
    start_marker = f"{marker}S\n"
    end_re = re.compile(rf"\n?{re.escape(marker)}E ([0-9]+)(?:\n|$)")
    start = cleaned.rfind(start_marker)
    if start < 0:
        raise NotebookShellUnreachable("Jupyter command start marker was not observed.")
    body_start = start + len(start_marker)
    end_match = end_re.search(cleaned, body_start)
    if end_match is None:
        raise NotebookShellTimeout(timeout or 0, _strip_markers(cleaned[body_start:], marker))
    stdout = cleaned[body_start : end_match.start()]
    return stdout, int(end_match.group(1))


def execute_notebook_shell_command(
    *,
    notebook_id: str,
    command: str,
    session: WebSession,
    timeout: int,
    run_command_fn: Callable[..., dict[str, Any]] = run_command_in_notebook,
) -> NotebookShellResult:
    """Execute *command* through Jupyter and preserve output and exit status.

    ``run_command_in_notebook`` remains the single WebSocket implementation.
    This wrapper adds an encoded marker protocol so terminal input echo cannot
    be mistaken for command completion.
    """
    marker = f"_INSPIRE_{uuid.uuid4().hex}_"
    wrapped = _build_marked_command(command, marker)
    started = time.monotonic()
    result = run_command_fn(
        notebook_id=notebook_id,
        command=wrapped,
        session=session,
        timeout=timeout,
        completion_marker=f"{marker}E ",
        capture_output=True,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    output = str(result.get("stdout") or "")
    if not result.get("success"):
        detail = str(result.get("error") or "Jupyter terminal command failed.")
        cleaned = _strip_ansi(output)
        started_marker = f"{marker}S" in cleaned
        finished_marker = re.search(rf"{re.escape(marker)}E [0-9]+", cleaned) is not None
        transport = dict(result.get("diagnostics") or {})
        reason = str(transport.get("exitReason") or "")
        # The transport watched both its socket and its own clock and says which one ended the read;
        # the captured text cannot say either, because a read the terminal answered perfectly well can
        # still lose the start marker - `rtunnel.transports._wait_for_prompt` receives the first frames
        # into a buffer it never publishes, so on a short read the echo can be all that is left.
        # Sorting by the reason first is what keeps "our clock ran out" from arriving at the caller as
        # "the session is the problem": a different failure, a different remedy, and until now a
        # complaint with nothing after its colon.
        if reason in CHANNEL_LOST_REASONS:
            partial = _partial_output(cleaned, marker, started_marker)
            raise NotebookShellChannelLost(
                elapsed_ms=_elapsed_ms(transport),
                reason=reason,
                channel_error=str(transport.get("channelError") or ""),
                timeout=timeout,
                output=partial,
                start_marker=started_marker,
            )
        if not finished_marker and (reason == DEADLINE_EXIT_REASON or started_marker):
            # Either the transport's clock or the command's silence ended the read, and the terminal
            # never said the channel went. The start marker is proof the session was alive at least
            # once, so "is the notebook RUNNING?" is the wrong thing to ask either way (issue #22).
            raise NotebookShellTimeout(
                timeout,
                _partial_output(cleaned, marker, started_marker),
                start_marker=started_marker,
            )
        if output.strip():
            clipped = _clip(_strip_markers(cleaned, marker))
            # "…: " with nothing after the colon is a sentence that stops, not a sentence. The
            # capture here is the echoed wrapper, which `_strip_markers` removes, so say the failure
            # once instead of saying it with a dangling colon.
            if clipped:
                detail = f"{detail}: {clipped}"
        raise NotebookShellUnreachable(detail)

    diagnostics = dict(result.get("diagnostics") or {})
    if diagnostics.get("stdoutTruncated"):
        raise RuntimeError(
            "Jupyter command output exceeded the capture limit; refusing a partial result."
        )
    stdout, returncode = _parse_marked_output(output, marker, timeout)
    return NotebookShellResult(
        notebook_id=notebook_id,
        stdout=stdout,
        # No stderr to assign: `run_command_in_notebook` hands back one merged PTY stream, never a second one.
        stderr=None,
        returncode=returncode,
        duration_ms=duration_ms,
        diagnostics=diagnostics,
    )


__all__ = [
    "CHANNEL_LOST_REASONS",
    "DEADLINE_EXIT_REASON",
    "NotebookShellChannelLost",
    "NotebookShellError",
    "NotebookShellResult",
    "NotebookShellTimeout",
    "NotebookShellUnreachable",
    "execute_notebook_shell_command",
    "strip_terminal_framing",
]
