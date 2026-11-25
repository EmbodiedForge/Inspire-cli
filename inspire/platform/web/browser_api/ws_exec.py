"""PTY WebSocket command execution for platform job/serving instances."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterator

import websocket

from inspire.platform.exec_frame import STATUS_VAR, frame_exit_code
from inspire.platform.tls_policy import ssl_options
from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url
from inspire.platform.web.browser_api.rtunnel.logging import _redact_text
from inspire.platform.web.session import WebSession
from inspire.platform.web.session.requests import build_requests_session, cookie_header_for_url

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1b\\)|[@-_])")
_DEFAULT_HANDSHAKE_TIMEOUT_SECONDS = 60.0
_INIT_TIMEOUT_SECONDS = 10.0
_KEEPALIVE_INTERVAL_SECONDS = 30.0
# The idle window this channel tolerates is measured, not guessed (§12 ③ of
# docs/EXEC-SESSION-DAEMON-ANALYSIS.md): the same pod answered instantly after 2 min of silence, answered
# 118 s late after ~3 min, and was gone after ~10. A keepalive that first fires at 180 s of silence starts
# *after* the window it exists to protect, so it moved inside it.
# What it does NOT buy: lifetime. Measured on one pod (docs/EXEC-SESSION-DAEMON-ANALYSIS.md §13), a silent
# command through the local HTTP proxy and the same command direct were both closed 330 s after opening
# while every ping was answered - so this protects a *quiet-but-alive* session from an idle cutoff and
# nothing more. A command with a long answer belongs in `job exec --detach`.
_KEEPALIVE_IDLE_SECONDS = 45.0
# Establishing the session is the step this transport loses: twelve commands two seconds apart over ONE
# session all completed while the per-command path lost the channel repeatedly the same morning, and
# back-to-back establishments at 1 s broke after two (LESSONS-ARCHIVE rule 11). Nothing has been sent to
# the pod at this point, so re-establishing is the one retry that cannot duplicate a side effect.
_ESTABLISH_ATTEMPTS = 2
_ESTABLISH_RETRY_PAUSE_SECONDS = 1.0
# How long one exec channel actually lives, measured on a live pod (docs/EXEC-SESSION-DAEMON-ANALYSIS.md
# §13): four sessions on the SAME pod, two silent and two printing a line every 30 s, two riding the local
# HTTP proxy (client socket to 127.0.0.1:7890) and two direct to the platform (to 10.252.252.3:443), were
# every one closed 330 s after opening - same second, same error - with a WebSocket ping answered on every
# keepalive cycle (ss byte counters: +28 sent, +24 received per cycle until the cut). Route and traffic are
# therefore not the lever: this is a hard connection lifetime, not an idle timeout, and no client-side
# keepalive extends it. The channel is a ~5-minute courier, so anything that must outlive it belongs in
# `job exec --detach`, whose answer lands in files on the pod instead of in a stream we can no longer read.
_MEASURED_CHANNEL_CEILING_SECONDS = 330.0
# How long past the deadline the watchdog waits before force-shutting the socket: the loop
# already exits on its own whenever recv() returns, so this only covers a recv() stuck
# mid-frame on a trickling channel (#78) and stays small enough that "timeout bounds the
# command" keeps its meaning.
_DEADLINE_WATCHDOG_GRACE_SECONDS = 2.0
# Ceiling on what one command's answer may grow to in memory. The notebook transport caps its capture at
# 1 MiB (see `_STDOUT_TAIL_CAP` in `rtunnel/transports.py`) and refuses a partial result when it is exceeded;
# this path cannot refuse - the command has already run and the channel is a merged stream we cannot replay -
# so it keeps the *tail*, which is where the answer to a runaway log is, and publishes `truncated` and
# `bytes_total` instead of handing back a head-truncated buffer an agent reads as the whole answer (#53).
_OUTPUT_CAP_BYTES = 1024 * 1024
# How much of an interrupted answer travels with the error. Deliberately far below `_OUTPUT_CAP_BYTES`:
# the cap on a *result* is about not losing a runaway log, while this is about an error path staying
# readable - the tail of what the pod printed is what tells the reader whether the work was half done.
_LOSS_TAIL_CHARS = 8192


class PlatformWsExecError(RuntimeError):
    """Raised when the platform PTY WebSocket cannot run the command."""


class PlatformWsUnavailableError(PlatformWsExecError):
    """The session could not be established, so **no command reached the pod**.

    Losing a session and losing an answer are different findings and this codebase has paid for
    confusing them (issue #27: losing contact with a running command never meant it did not run). Every
    raise site of this class is before the caller's command is written to the PTY - the upgrade failed,
    or the channel closed while the shell was being prepared - which is what licenses both the retry in
    `PlatformWsSession.open` and the "retrying is safe" advice at the CLI.
    """


class PlatformWsChannelLostError(PlatformWsExecError):
    """The channel closed **after** the command was written, so the outcome is unknown.

    Deliberately a sibling of `PlatformWsUnavailableError`, never its parent or child: the command reached
    the PTY, so "retrying cannot duplicate anything" would be a lie that duplicates writes (#27).

    `seconds_open` is what makes the diagnosis stage-aware. A channel that died in the first seconds is the
    flaky-establishment shape the retry and the old "often transient" hint were written for; a channel that
    died near `_MEASURED_CHANNEL_CEILING_SECONDS` hit a lifetime ceiling that re-running the same long
    command reproduces exactly, and the reader's way out of that is `--detach`, not another attempt.
    """

    def __init__(self, message: str, *, seconds_open: float, partial_output: str = "") -> None:
        super().__init__(message)
        self.seconds_open = float(seconds_open)
        # What the pod had already printed when the channel died. A lost answer is not an empty answer:
        # the measured chatty session had printed a line every 30 s for 330 s and used to report none of it.
        self.partial_output = partial_output


#: Failures that mean "there was no channel", as opposed to an answer from the platform explaining why
#: it refused. A handshake the gateway answered with a status is a verdict - `_connect_platform_ws`
#: already refreshes the web session once for the expired-cookie shape and must not then hammer it.
_TRANSIENT_CHANNEL_ERRORS = (
    websocket.WebSocketConnectionClosedException,
    websocket.WebSocketTimeoutException,
    OSError,
)


@dataclass
class PlatformWsExecResult:
    """Normalized result from a platform PTY WebSocket exec."""

    instance_name: str
    output: str
    exit_code: int | None
    raw_status: dict[str, Any]
    # `truncated` is about the buffer, not the command: the pod sent more than the capture ceiling and what is
    # in `output` is its tail. `bytes_total` counts everything the channel carried, so a reader can tell
    # "the last 1 MiB of a 40 MiB log" from "the whole answer" (#53).
    truncated: bool = False
    bytes_total: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance_name": self.instance_name,
            "stdout": self.output,
            # Null, because the transport is a PTY: the kernel merges both streams before we ever see them, so
            # there is no stderr to report. Publishing "" would be the worst variant - a field that promises a
            # distinction the channel cannot make, so a caller that treats an empty stderr as "nothing went
            # wrong" is reassured by an artefact of the transport; null reads as "this channel has no stderr",
            # which is the claim we can make. Splitting the streams for real is #51 and needs a live pod.
            "stderr": None,
            "stream_model": "pty-merged",
            "exit_code": self.exit_code,
            "truncated": self.truncated,
            "bytes_total": self.bytes_total,
            "raw_status": self.raw_status,
        }


def _strip_ansi(value: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", value)


def _loss_partial_output(parts: list[str], start_tag: str) -> str:
    """What the pod had already answered when the channel died, in the shape a normal answer takes.

    The same trimming the no-end-marker result path applies - ANSI stripped, CR folded, and everything up
    to the command's own start tag discarded because it is the PTY echo of the wrapped script rather than
    output - then the *tail*, capped: the tail is what a reader acts on, and an error must not become the
    thing that fills the caller's terminal. Measured need: the chatty A/B session printed a line every
    30 s for the 330 s before the cutoff, and the command reported none of it.
    """
    combined = _strip_ansi("".join(parts))
    combined = combined.replace("\r\n", "\n").replace("\r", "")
    start = combined.rfind(start_tag)
    if start != -1:
        combined = combined[start + len(start_tag) :]
    return combined[-_LOSS_TAIL_CHARS:].strip()


def _cookie_header_from_session(session: WebSession, base_url: str, ws_url: str) -> str:
    """Cookies for the handshake, scoped to `ws_url` the way requests scopes them to a host.

    The jar is built from `base_url` because that is the host that fills in a domain for a cookie
    the cached session stored without one; the scoping runs against `ws_url`, whose path is the API
    path the upgrade actually goes to.
    """
    http = build_requests_session(session, base_url)
    try:
        return cookie_header_for_url(http, ws_url)
    finally:
        http.close()


def _platform_ws_url(endpoint_path: str, query: dict[str, str]) -> str:
    base_url = _get_base_url().rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    api_path = _browser_api_path(endpoint_path)
    path = f"{base_path}{api_path}" if base_path else api_path
    return urllib.parse.urlunsplit((scheme, parsed.netloc, path, urllib.parse.urlencode(query), ""))


logger = logging.getLogger(__name__)


def _ws_host(ws_url: str) -> str:
    return urllib.parse.urlsplit(ws_url).hostname or ""


def _inherited_proxy(ws_url: str) -> str | None:
    """The proxy the socket would be dialled through from the environment, or None.

    `create_connection` takes its proxy from `https_proxy`/`http_proxy` when no proxy option is given, so
    on a machine that exports a local proxy for everything - the ordinary state on a laptop that needs a
    mirror for source control - the exec PTY rides an HTTP CONNECT tunnel through that proxy without the
    CLI or the user having chosen it. The portal here answers directly in 35 ms and through the local
    proxy too, so nothing but the socket's own lifetime distinguishes the two routes, and an upgrade
    carried by a middlebox with its own idle timer is exactly how a pod that is provably running answers
    `Connection to remote host was lost`.

    Diagnostic only: the credential part of the URL is never reproduced.
    """
    parsed = urllib.parse.urlsplit(ws_url)
    host = parsed.hostname or ""
    if not host:
        return None
    key = "https_proxy" if parsed.scheme == "wss" else "http_proxy"
    value = (os.environ.get(key) or os.environ.get(key.upper()) or "").strip()
    if not value:
        return None
    no_proxy = (os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or "").replace(" ", "")
    entries = [entry for entry in no_proxy.split(",") if entry]
    if host in entries or any(
        entry.lstrip(".") and host.endswith(entry.lstrip(".")) for entry in entries
    ):
        return None
    try:
        proxy = urllib.parse.urlsplit(value if "//" in value else f"//{value}")
    except ValueError:
        return "an environment HTTP proxy"
    # `rsplit` on the userinfo, not a re-parse: whatever was in front of the `@` is a credential.
    host_port = proxy.netloc.rsplit("@", 1)[-1]
    return f"{proxy.scheme}://{host_port}" if proxy.scheme else host_port


@contextlib.contextmanager
def _direct_route(ws_url: str) -> Iterator[None]:
    """Make one connection attempt ignore an environment proxy for this host.

    The library reads `no_proxy` from the environment at connect time and offers no way to say "no proxy"
    as an option, so the env is widened for the length of the attempt and put back exactly as found.
    Callers run this from several threads when fanning out; the worst interleaving is a restore that
    drops another thread's entry while it is connecting, which puts that attempt back on the route it
    would have taken anyway.
    """
    host = _ws_host(ws_url)
    if not host or not _inherited_proxy(ws_url):
        yield
        return
    keys = ("no_proxy", "NO_PROXY")
    original = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            entries = [
                entry for entry in (os.environ.get(key) or "").replace(" ", "").split(",") if entry
            ]
            if host not in entries:
                os.environ[key] = ",".join([*entries, host])
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _connect_platform_ws(
    ws_url: str,
    *,
    session: WebSession,
    timeout: float,
    retried: bool = False,
    direct: bool = False,
):
    base_url = _get_base_url().rstrip("/")
    cookie_header = _cookie_header_from_session(session, base_url, ws_url)
    try:
        # This socket carries the session cookie, so it verifies by default; CERT_NONE arrives
        # only through api.skip_ssl_verify, which prints a warning on stderr (`ssl_options`).
        with _direct_route(ws_url) if direct else contextlib.nullcontext():
            return websocket.create_connection(
                ws_url,
                cookie=cookie_header,
                timeout=timeout,
                sslopt=ssl_options(),
            )
    except websocket.WebSocketBadStatusException as exc:
        status_code = getattr(exc, "status_code", None)
        # The web session expires while a shell session would happily live on, and the platform answers an
        # upgrade with an expired cookie by 302-ing it to Keycloak - which surfaces here as a 401 on the
        # WebSocket handshake. The HTTP layer has refreshed and retried that case for a long time; this path
        # only ever retried status 200, so an expired session looked like a broken pod. Refresh in place and
        # try once, exactly like the HTTP layer does, before blaming the transport.
        if status_code in (200, 401, 403) and not retried:
            if status_code in (401, 403):
                try:
                    from inspire.platform.web.session import get_web_session as _fresh_session
                    from inspire.platform.web.session import _refresh_session_in_place

                    refreshed = _fresh_session(
                        force_refresh=True, account=getattr(session, "account_alias", None)
                    )
                    _refresh_session_in_place(session, refreshed)
                except (
                    Exception
                ):  # noqa: BLE001 - the retry below reports the real handshake failure
                    pass
            else:
                time.sleep(2)
            return _connect_platform_ws(
                ws_url,
                session=session,
                timeout=timeout,
                retried=True,
            )
        details = _redact_text(str(exc))
        raise PlatformWsExecError(
            f"WebSocket upgrade failed: HTTP {status_code}: {details}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # Classify before wrapping: once the cause is behind a PlatformWsExecError the caller cannot
        # tell "the gateway refused" from "there was no channel", and only the second is retryable.
        message = f"WebSocket connection failed: {_redact_text(str(exc))}"
        if isinstance(exc, _TRANSIENT_CHANNEL_ERRORS):
            raise PlatformWsUnavailableError(message) from exc
        raise PlatformWsExecError(message) from exc


def _wrap_exec_script(marker: str, command: str) -> str:
    """Frame one command so its end markers bracket the answer and `$__inspire_status` holds its exit code.

    The exit code comes from `exec_frame.frame_exit_code`, not from a bare `EC=$?`: `$?` is the last stage of
    a pipeline, so `pytest -q | tail -20` reported 0 through this channel on a failing suite (#52). The
    command still runs in the session's own shell - no subshell - because `job upload` cds and exports in one
    command and relies on them in the next.
    """
    command_body = command.rstrip("\r\n")
    return f"echo {marker}S\n{frame_exit_code(command_body)}\necho\necho {marker}E ${STATUS_VAR}\n"


class PlatformWsSession:
    """One platform PTY session, many commands.

    Measured on a live training pod: twelve commands sent 2 seconds apart over a single session all completed,
    while the per-command path (`open a websocket, run one command, close`) lost the channel repeatedly through
    the same morning. That localises the instability to session establishment, not command execution - and it
    means a caller that needs ten round trips should pay for one session, not ten.

    Use it as a context manager so the socket cannot outlive the caller:

        with PlatformWsSession.open(endpoint_path=..., query=..., instance_name=..., session=s) as ws_session:
            first = ws_session.run("echo a")
            second = ws_session.run("echo b")
    """

    def __init__(self, ws: Any, instance_name: str) -> None:
        self._ws = ws
        self.instance_name = instance_name
        self.commands_run = 0
        # Whether the pod's shell answered the ready marker. A session that never saw it still runs its
        # commands - the marker is echo we control, and refusing to proceed on a pod that prints it
        # oddly would break working pods - but every answer from it is reported with this, because
        # "the shell never confirmed ready" and "the shell was ready and then dropped" are different
        # things to diagnose.
        self.shell_ready = False

    @classmethod
    def open(
        cls,
        *,
        endpoint_path: str,
        query: dict[str, str],
        instance_name: str,
        session: WebSession,
        handshake_timeout: float | None = None,
        attempts: int | None = None,
        retry_pause: float | None = None,
    ) -> "PlatformWsSession":
        """Establish one PTY session, re-establishing it while the channel is simply not there.

        Only establishment is retried, and only for `PlatformWsUnavailableError` - the shape that means
        there was no channel rather than an answer explaining a refusal. That restriction is what makes
        the retry safe: the caller's command has not been written to the PTY yet, so a second attempt
        cannot duplicate a write, an install or a training launch. `run()` deliberately keeps no such
        retry, and must not gain one.

        `attempts` counts whole tries, so the default of 2 sends the establishment twice at most;
        the pause between them is not decoration - establishments back-to-back at 1 s are the measured
        pattern that breaks the channel (LESSONS-ARCHIVE rule 11).
        """
        timeout = (
            _DEFAULT_HANDSHAKE_TIMEOUT_SECONDS
            if handshake_timeout is None
            else max(float(handshake_timeout), 1.0)
        )
        ws_url = _platform_ws_url(endpoint_path, query)
        tries = _ESTABLISH_ATTEMPTS if attempts is None else max(1, int(attempts))
        pause = (
            _ESTABLISH_RETRY_PAUSE_SECONDS if retry_pause is None else max(0.0, float(retry_pause))
        )
        last_error: PlatformWsUnavailableError | None = None
        proxy = _inherited_proxy(ws_url)
        for attempt_index in range(tries):
            # The first try goes wherever the environment says it goes, so a network that genuinely needs
            # its proxy keeps working. A retry changes route instead of repeating the attempt, because the
            # routes are not the same socket: a PTY upgrade goes through a proxy's CONNECT handling, its
            # connection limits and possibly a different egress path. Be honest about the strength of the
            # evidence - §13 measured that the route does NOT decide how long a channel lives, so this is
            # a cheap diversity bet on the establishment step, not a measured fix, which is also why it
            # stays second: a proxy-only network must never lose its first attempt.
            direct = attempt_index > 0 and proxy is not None
            if attempt_index:
                time.sleep(pause)
            if direct:
                logger.info(
                    "exec session: retrying %s without the environment proxy %s",
                    _ws_host(ws_url),
                    proxy,
                )
            try:
                ws = _connect_platform_ws(ws_url, session=session, timeout=timeout, direct=direct)
            except PlatformWsUnavailableError as exc:
                last_error = exc
                continue
            handle = cls(ws, instance_name)
            try:
                handle._prepare_shell()
            except PlatformWsUnavailableError as exc:
                handle.close()
                last_error = exc
                continue
            except Exception:
                handle.close()
                raise
            return handle
        assert last_error is not None  # the loop either returns a handle or records why it did not
        if proxy:
            routes = f"via the environment proxy {proxy}" + (", then direct" if tries > 1 else "")
        else:
            routes = "no environment proxy in play"
        raise PlatformWsUnavailableError(
            f"could not establish a pod shell session in {tries} attempt"
            f"{'s' if tries != 1 else ''} ({routes}); no command was sent: {last_error}"
        ) from last_error

    def _prepare_shell(self) -> None:
        """Start a clean bash with no echo and no prompts, then wait for a ready marker.

        Without this the PTY echoes every line we send, and an echoed base64 chunk is the file's contents
        leaking into a terminal or a log - which is a bug we have actually shipped once.

        A channel that closes here is named `PlatformWsUnavailableError`, not left as a raw websocket
        exception: the caller has to be able to tell "the pod closed while its shell was starting" from
        "we lost the answer", and only the second one may have run anything.
        """
        marker = f"_INSPIRE{int(time.time() * 1000) % 100000000}_"
        try:
            self._ws.send(
                "if [ -x /bin/bash ]; then exec /bin/bash; "
                "elif command -v bash >/dev/null 2>&1; then exec bash; "
                "else exec /bin/sh; fi\n"
            )
            time.sleep(0.3)
            self._ws.send(
                "stty -echo 2>/dev/null; "
                "bind 'set enable-bracketed-paste off' 2>/dev/null; "
                "stty columns 200 rows 50 2>/dev/null; "
                "unset PROMPT_COMMAND; PS0=; PS1=; PS2=; "
                f"echo {marker}RDY\n"
            )
            deadline = time.time() + _INIT_TIMEOUT_SECONDS
            buffer = ""
            while time.time() < deadline:
                self._ws.settimeout(min(5.0, max(0.1, deadline - time.time())))
                try:
                    data = self._ws.recv()
                except websocket.WebSocketTimeoutException:
                    break
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                buffer += str(data)
                if f"{marker}RDY" in buffer:
                    self.shell_ready = True
                    break
            # Drain whatever the shell still had buffered so it cannot bleed into the first command's output.
            while True:
                self._ws.settimeout(0.3)
                try:
                    self._ws.recv()
                except websocket.WebSocketTimeoutException:
                    break
        except websocket.WebSocketConnectionClosedException as exc:
            raise PlatformWsUnavailableError(
                "the pod closed the channel while its shell was starting "
                f"({_redact_text(str(exc))})"
            ) from exc

    def run(
        self, command: str, timeout: float | None = None, *, max_bytes: int | None = None
    ) -> dict[str, Any]:
        """Run one command on this session and return the same dict shape as execute_platform_ws_command.

        `max_bytes` overrides `_OUTPUT_CAP_BYTES` for this call; the ceiling is on what we *keep*, not on what
        we wait for - the loop still runs to the end marker, so the exit code survives a capped capture.
        """
        normalized = command.rstrip("\r\n")
        if not normalized:
            raise ValueError("command is required")
        self.commands_run += 1

        marker = f"_INSPIRE{int(time.time() * 1000) % 100000000}_"
        start_tag, end_tag = f"{marker}S", f"{marker}E"
        end_pattern = re.compile(rf"{re.escape(end_tag)}\s+(\d+)")

        try:
            self._ws.send(_wrap_exec_script(marker, normalized))
        except _TRANSIENT_CHANNEL_ERRORS as exc:
            # A failed write is NOT proof that nothing reached the pod: the frame may have gone out in
            # pieces, and the wrapped script is a shell script whose earlier lines can already have run.
            # So this is the outcome-unknown class, not `PlatformWsUnavailableError`, and `seconds_open`
            # says plainly that the channel died on the way in rather than at the lifetime ceiling.
            raise PlatformWsChannelLostError(
                "the exec channel closed while the command was being written, so how much of it "
                f"reached the pod is unknown ({_redact_text(str(exc))})",
                seconds_open=0.0,
            ) from exc

        stop_keepalive = threading.Event()
        command_sent_at = time.time()
        last_data_time = command_sent_at
        lock = threading.Lock()

        def _keepalive() -> None:
            while not stop_keepalive.wait(_KEEPALIVE_INTERVAL_SECONDS):
                with lock:
                    idle = time.time() - last_data_time
                if idle > _KEEPALIVE_IDLE_SECONDS:
                    try:
                        # A ping *frame*, never a byte of shell input. The idle timer that drops this
                        # channel is the proxy's read timer on the upstream direction, and only the
                        # peer's pong answers it - the single space this used to write reset the
                        # client-to-server direction only, so it could not save a silent command, while
                        # typing a character into whatever the session happened to be reading. RFC 6455
                        # §5.5.3 makes the pong a must for a peer that is not closing, so keeping the
                        # line alive asks nothing of the pod's shell.
                        self._ws.send(b"", opcode=websocket.ABNF.OPCODE_PING)
                    except Exception:
                        break

        thread = threading.Thread(target=_keepalive, daemon=True)
        thread.start()

        cap = _OUTPUT_CAP_BYTES if max_bytes is None else max(0, int(max_bytes))
        parts: list[str] = []
        buffered = 0
        bytes_total = 0
        truncated = False

        # The socket timeout bounds each *sub-read*, not a whole recv(): a channel that
        # trickles bytes (or pongs) can keep one recv() alive long past the deadline, which
        # is how run(timeout=30) once blocked 295s and ended in a connection error instead
        # of the documented "deadline expires -> stop reading -> exit_code None" (#78). The
        # watchdog force-shuts the socket shortly after the deadline; the loop then treats
        # the resulting closed-connection error as the timeout it caused.
        deadline_exceeded = threading.Event()
        stop_watchdog = threading.Event()

        def _deadline_watchdog(deadline_ts: float) -> None:
            remaining = deadline_ts - time.time() + _DEADLINE_WATCHDOG_GRACE_SECONDS
            if stop_watchdog.wait(max(remaining, 0.0)):
                return
            deadline_exceeded.set()
            try:
                self._ws.shutdown()
            except Exception:
                pass

        try:
            deadline = None if timeout is None else time.time() + max(float(timeout), 1.0)
            if deadline is not None:
                threading.Thread(target=_deadline_watchdog, args=(deadline,), daemon=True).start()
            while deadline is None or time.time() < deadline:
                recv_timeout = (
                    30.0 if deadline is None else min(30.0, max(0.1, deadline - time.time()))
                )
                self._ws.settimeout(recv_timeout)
                try:
                    data = self._ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except websocket.WebSocketConnectionClosedException as exc:
                    if deadline_exceeded.is_set():
                        break
                    # Everything the pod already answered is in `parts`, and today it goes nowhere: the
                    # chatty A/B run printed a line every 30 s for five and a half minutes and the command
                    # reported none of it. A lost answer is not an empty answer.
                    raise PlatformWsChannelLostError(
                        f"Connection to remote host was lost. ({int(time.time() - command_sent_at)} s "
                        "after the command was written, with its answer still unreceived: the pod, or a "
                        f"hop between us and it, closed the channel) {_redact_text(str(exc))}",
                        seconds_open=time.time() - command_sent_at,
                        partial_output=_loss_partial_output(parts, start_tag),
                    ) from exc
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                chunk = str(data)
                bytes_total += len(chunk.encode("utf-8", errors="replace"))
                parts.append(chunk)
                buffered += len(chunk)
                if buffered > cap:
                    # Keep the last max(cap, newest frame) bytes. The tail is where the answer to a
                    # runaway log is, and the end marker with its exit code is the stream's final
                    # bytes, so a byte-level tail cut always preserves them. The floor at the
                    # newest frame keeps the older rule that a zero ceiling still reports the exit
                    # code. The frame-granular cut this replaces had a failure shape measured live
                    # (#28): one early frame larger than the cap (the PTY echo of the wrapped
                    # script is exactly that) was evicted whole and took every output frame with
                    # it, answering `truncated: true` with zero bytes of output.
                    truncated = True
                    keep = max(cap, len(parts[-1]))
                    tail = "".join(parts)[-keep:]
                    parts = [tail]
                    buffered = len(tail)
                with lock:
                    last_data_time = time.time()
                if end_pattern.search("".join(parts)):
                    break
        finally:
            stop_keepalive.set()
            stop_watchdog.set()

        combined = _strip_ansi("".join(parts))
        combined = combined.replace("\r\n", "\n").replace("\r", "")
        match = end_pattern.search(combined)

        if match is None:
            tail = combined
            any_start = combined.rfind(start_tag)
            if any_start != -1:
                tail = combined[any_start + len(start_tag) :]
            return PlatformWsExecResult(
                instance_name=self.instance_name,
                output=tail.strip(),
                exit_code=None,
                truncated=truncated,
                bytes_total=bytes_total,
                raw_status={
                    "transport": "platform_pty_ws",
                    "end_marker_seen": False,
                    "timed_out": timeout is not None,
                    "session_commands": self.commands_run,
                    "shell_ready": self.shell_ready,
                },
            ).as_dict()

        end_index = match.start()
        start_index = combined.rfind(start_tag, 0, end_index)
        if start_index == -1:
            # The start marker scrolled out of the kept tail, so everything still in the buffer before the
            # end marker is output. The exit code is readable from the end marker regardless, and a reader
            # told `truncated` knows the head of the stream is gone.
            truncated = True
            output_start = 0
        else:
            output_start = start_index + len(start_tag)

        return PlatformWsExecResult(
            instance_name=self.instance_name,
            output=combined[output_start:end_index].strip(),
            exit_code=int(match.group(1)),
            truncated=truncated,
            bytes_total=bytes_total,
            raw_status={
                "transport": "platform_pty_ws",
                "end_marker_seen": True,
                "timed_out": False,
                "session_commands": self.commands_run,
                "shell_ready": self.shell_ready,
            },
        ).as_dict()

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def __enter__(self) -> "PlatformWsSession":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def execute_platform_ws_command(
    *,
    endpoint_path: str,
    query: dict[str, str],
    instance_name: str,
    command: str,
    timeout: float | None,
    session: WebSession,
    handshake_timeout: float | None = None,
) -> dict[str, Any]:
    """Execute one shell command through the platform PTY WebSocket.

    Kept as the simple entry point, but it is one session per command. Callers that need several commands on
    the same pod should open a PlatformWsSession once; see the class docstring for the measurement behind that.

    `timeout` bounds the command (`run`), which is what a caller's `--timeout` means. Opening the socket is a
    different wait with different failure modes, so it gets its own `handshake_timeout` and otherwise falls back
    to the transport default. Passing the command timeout through both, as this used to do, meant a caller could
    not tell a socket that never opened from a command that ran too long (#50).
    """
    normalized_command = command.rstrip("\r\n")
    if not normalized_command:
        raise ValueError("command is required")
    with PlatformWsSession.open(
        endpoint_path=endpoint_path,
        query=query,
        instance_name=instance_name,
        session=session,
        handshake_timeout=handshake_timeout,
    ) as handle:
        return handle.run(normalized_command, timeout)


__all__ = [
    "PlatformWsExecError",
    "PlatformWsSession",
    "PlatformWsUnavailableError",
    "PlatformWsChannelLostError",
    "PlatformWsExecResult",
    "execute_platform_ws_command",
]
