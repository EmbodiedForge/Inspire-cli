"""Install and drive a pod-side execd through the platform PTY channel.

Why this module exists
----------------------
`exec_records.py` made detached exec dependable by putting state on the pod and turning every question into
an idempotent file read. execd is the next step: one daemon inside the pod owns the records and answers over a
Unix socket, so the caller pays a shell round trip only to ask a question. The local side has exactly two jobs
- get `agent.py` onto the pod and prove it is the bytes we shipped, then frame requests and answers around a
channel that merges stdout with stderr and drops without warning.

There is no direct route to that socket from a laptop: the only channel into a training pod is the platform PTY
WebSocket (`web/browser_api/ws_exec.py`), whose identity is ``(job_id, instance_name)`` with no reconnectable
session id. So every RPC here is one shell command that runs the *same* ``agent.py`` in its ``rpc`` mode, which
reads the daemon token off the pod's own file system and talks to the socket locally. That is also why the
token never appears here: it is the one secret on this path that never crosses the browser PTY, and nothing in
this module asks for it.

Fail loud, never fall back
--------------------------
A bootstrap failure raises `ExecdError` with a stable code. It does not quietly downgrade to the legacy
`exec_records` path: a caller that asked for execd and silently got the old channel would lose exactly the
properties it asked for - offset resume, named sessions, one daemon per pod - while looking like it kept them.
Choosing a fallback is the CLI's decision to make out loud, never this module's to make quietly.

One session, numbered chunks, verified bytes
--------------------------------------------
The whole install reuses the ONE ``PlatformWsSession`` it is handed. Per-command sessions were measured losing
the channel on a live pod while a single session then carried 83 commands and two verified uploads
(`transport/exec_upload.py`), so an install that opened a session per chunk would manufacture its own failure.

Bytes move as numbered chunk files, each written to ``<name>.tmp`` and renamed into place, which is what makes
a retry safe: re-sending chunk 7 replaces chunk 7 byte for byte, so a lost answer costs a re-ask instead of a
corrupt file. What is never done is a blind retry of an *append* - the failure `exec_upload` documents, where a
retried ``>>`` duplicates bytes silently and a corrupted upload still looks like a success. Chunks already
present at the right size are skipped, so an interrupted install resumes where it stopped, and the whole-file
sha256 is compared before the payload is renamed into its versioned path. A destination whose digest already
matches is not rewritten at all. Nothing is deleted but our own staging files, by exact name.

The agent CLI this module drives (see ``agent.py``)
---------------------------------------------------
``python3 <versioned>/agent.py serve --state-dir <state>``
    Run the daemon on ``<state>/daemon.sock``. It exits with ``already-running`` if another daemon holds the
    state directory, and reuses the daemon token across restarts.

``python3 <versioned>/agent.py rpc --state-dir <state> --timeout S [--wait S] [--json P|-] <method>``
    Send one request, print one JSON line of response. Exit 0 on success, 1 on an error response, 2 on a
    transport failure, 3 on a startup failure - the response line is printed in every one of those cases, so
    the frame below is what the client trusts and the exit code is only evidence about the *channel*.

What the agent does not do is frame its own answer, and the channel needs framing: a response is one JSON line
that can be 100 KiB, on a transport that merges streams and loses answers. So this module spools it. Each RPC
answers into ``<root>/rpc/<ns>/<rpc-id>.resp`` and every read - the first and every re-read - is the same
command, which runs the RPC only when *this* invocation creates that file (under ``noclobber``, so exactly one
invocation can) and then prints one frame::

    EXECD-RPC <rpc-id> total=<bytes> sha256=<hex> offset=<from>
    <base64 of response[from:from+limit], whitespace-insignificant>
    EXECD-RPC-END <rpc-id> offset=<next> bytes=<emitted>

When there is no answer to page, the pager says so instead of leaving the caller to guess::

    EXECD-RPC-NODATA <rpc-id> rc=<exit> state=empty|interrupted|missing err=<tail of .err>

The file's *existence* is what proves an RPC already ran - not its content. The first version of this guard
tested ``[ -s spool ]``, which read an empty response file as "never ran" and would execute the RPC again;
an RPC that answered with nothing, or that was cut off mid-write, is now re-read rather than re-run.
``<rpc-id>.resp.done`` records the RPC's exit status once the process finishes and ``.err`` keeps its stderr,
which is how an attempt that died is told apart from one that completed and answered nothing. Both files are
evidence and neither is re-executed: a lost answer is reported, and only a new request id runs it again.

Three properties fall out of that, and they are the reason for the design rather than decorations of it: a
response we lost is re-*read*, from the offset we reached, without asking the daemon anything again; the sha256
of the whole answer is checked after reassembly, so a spliced or truncated read cannot pass; and since the
spool is keyed by a client-generated id whose creation is the execution token, the only way to run an RPC twice
is to ask for a new id. A new id is exactly what a deliberate retry does, and the agent's own ``run`` is
idempotent per ``exec_id``, so even that does not start a command twice. What this does *not* buy: if some other
process deletes a spool, the next read of that id runs the RPC again, which is why pruning never touches the
spool, ``.err`` or ``.done`` a call may still need.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import posixpath
import re
import secrets
import shlex
import time
from dataclasses import dataclass, replace
from importlib import resources
from typing import Any, Callable

from inspire.platform.execd.protocol import (
    AGENT_VERSION,
    PROTOCOL_VERSION,
    ExecdError,
    unwrap_response,
)

#: Everything execd owns on the pod, under one private directory. It deliberately does NOT share
#: `/tmp/.inspire-exec` (the legacy `exec_records` records): installing or upgrading execd must never be able
#: to disturb records the old path owns, and cleanup on both sides names exact paths only.
EXECD_ROOT = "/tmp/.inspire-execd"
#: The daemon's own directory: `daemon.sock`, `daemon.token`, `runs/`, `sessions/`. Kept separate from the
#: install staging so an execd cleanup never reaches our payload files, or vice versa.
STATE_DIR = posixpath.join(EXECD_ROOT, "state")
VERSIONS_DIR = posixpath.join(EXECD_ROOT, "versions")
XFER_DIR = posixpath.join(EXECD_ROOT, "xfer")
RPC_DIR = posixpath.join(EXECD_ROOT, "rpc")
LOG_DIR = posixpath.join(EXECD_ROOT, "logs")

AGENT_FILENAME = "agent.py"
PYTHON3 = "python3"

#: Per-command wall clock. Preflight and chunk writes are short; an RPC may need longer, and a long-polling
#: `attach` needs the shim and the PTY read to outlast the daemon's own wait.
DEFAULT_COMMAND_TIMEOUT = 60

#: Raw bytes per chunk command. The ceiling is the PTY's canonical-mode line limit (`MAX_CANON`, typically
#: 4096 bytes), which a live pod has already refused at 64 KiB. 2048 raw bytes base64 to 2732 characters, so a
#: chunk command line stays under that ceiling with margin; `exec_upload`'s 3 KiB sits at exactly 4096 encoded
#: characters, which is a thinner margin than a multi-kilobyte install needs.
DEFAULT_CHUNK_BYTES = 2048

#: Params of at most this many raw bytes travel as one `--json` argument. Anything bigger is pushed as
#: numbered chunk files and handed to the shim on stdin (`--json -`), because a request that grows with the
#: caller's data is exactly the failure mode the chunked uploader exists to prevent.
MAX_INLINE_REQUEST_BYTES = 1200

#: Response bytes emitted per frame, and the (larger) ceiling on what we let the transport keep for the read.
#: Hitting the read ceiling returns the *tail* of a frame, which is why the two values differ.
DEFAULT_RESPONSE_LIMIT = 48 * 1024
RESPONSE_READ_CAP = 192 * 1024

#: Largest payload this module will push. The exec channel is not a bulk transfer path.
DEFAULT_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024

#: The free-space and free-inode floors the daemon will enforce on every `run` by default. Checked at install
#: time so "this pod cannot host executions" is said before the first command, not after it.
MIN_FREE_BYTES = 64 * 1024 * 1024
MIN_FREE_INODES = 512
_MIN_TRANSFER_MARGIN_BYTES = 4096

#: How long to wait for a freshly started daemon to answer health, and how long each probe keeps trying to
#: connect. The daemon writes its socket before it answers, so a connect failure right after launch is normal.
DEFAULT_START_TIMEOUT = 25.0
DEFAULT_HEALTH_POLL = 0.5
DEFAULT_CONNECT_WAIT = 2.0

#: Re-reads of the same offset within one session before the session itself is treated as dead.
DEFAULT_LOST_READ_ATTEMPTS = 2

#: Codes that prove no daemon is answering. Only an *answer* may license starting one: a lost read proves
#: nothing, and a second daemon would exit on the lock anyway - after we misreported the state to the caller.
#: `no-token` counts because the token file is written by the daemon's own first start.
NOT_RUNNING_CODES = frozenset({"daemon-unreachable", "no-token"})

#: Transport-shaped codes, shared with the client: "we did not hear", where re-asking is correct.
TRANSPORT_CODES = frozenset({"transport-lost", "response-lost", "health-unconfirmed"})

_FRAME_BEGIN_RE = re.compile(
    r"EXECD-RPC (?P<id>[0-9a-f]{8,64}) total=(?P<total>\d+) sha256=(?P<sha>[0-9a-f]{64}) "
    r"offset=(?P<offset>\d+)"
)
_FRAME_END_RE = re.compile(
    r"EXECD-RPC-END (?P<id>[0-9a-f]{8,64}) offset=(?P<offset>\d+) bytes=(?P<bytes>\d+)"
)
_FRAME_NODATA_RE = re.compile(
    r"EXECD-RPC-NODATA (?P<id>[0-9a-f]{8,64}) rc=(?P<rc>-?\d+) state=(?P<state>[a-z]+) "
    r"err=(?P<err>.*)"
)
_PREFLIGHT_RE = re.compile(
    r"EXECD-PREFLIGHT root=(?P<root>\d) tmp_w=(?P<tmp_w>\d) tmp_x=(?P<tmp_x>\d) "
    r"afunix=(?P<afunix>\d) free=(?P<free>\d+) inodes=(?P<inodes>\d+)"
)
_CHUNK_SIZE_RE = re.compile(r"^\s*(?P<size>\d+)\s+(?P<path>\S+)$")

#: Error-data keys whose value is a secret by name, not by shape, and so must be masked whole.
_SECRET_KEY_RE = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|authorization|cookie|credential|bearer)"
)

_HEALTH_ANSWERED = "answered"
_HEALTH_ABSENT = "absent"
_HEALTH_UNKNOWN = "unknown"


@dataclass(frozen=True)
class AgentHandle:
    """Where the agent lives on the pod, and what this bootstrap call actually did."""

    path: str
    digest: str
    state_dir: str
    socket_path: str
    agent_version: str
    #: False when the destination already held these bytes - the skip is the point, not an accident.
    uploaded: bool
    #: False when an already-running daemon was adopted instead of a new one started.
    started: bool
    chunks_sent: int
    chunks_reused: int
    free_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.digest,
            "state_dir": self.state_dir,
            "socket": self.socket_path,
            "agent_version": self.agent_version,
            "protocol": PROTOCOL_VERSION,
            "uploaded": self.uploaded,
            "started": self.started,
            "chunks_sent": self.chunks_sent,
            "chunks_reused": self.chunks_reused,
            "free_bytes": self.free_bytes,
        }


@dataclass(frozen=True)
class _Frame:
    """One decoded response frame."""

    total: int
    sha256: str
    offset: int
    next_offset: int
    payload: bytes


def _redact(text: str) -> str:
    """Mask secrets with the transport's own redactor, imported lazily to keep this module cheap to import."""
    try:
        from inspire.platform.web.browser_api.rtunnel.logging import _redact_text

        return _redact_text(str(text or ""))
    except Exception:  # noqa: BLE001 - redaction must never become the failure
        return str(text or "")


def _redact_error(exc: ExecdError) -> ExecdError:
    """The same error, with its message and string data fields run through the transport's redactor.

    An error line from the daemon can quote whatever a recorded command printed, and a command that echoes a
    token is exactly the case that ends up in an exception, then a terminal, then an agent transcript (#56 in
    the exec-records line of work). Codes and numbers stay verbatim; text is masked.

    A data field under a secret-shaped *key* is masked whole rather than re-scan as text: the redactor looks
    for `token=…` and `Authorization: Bearer …`, and a value that is nothing but the secret matches neither,
    so scanning it would leave the secret intact in the one place it was already filed under its own name.
    """
    message = _redact(str(exc))
    data = {}
    for key, value in (exc.data or {}).items():
        if not isinstance(value, str):
            data[key] = value
        elif _SECRET_KEY_RE.search(str(key)):
            data[key] = "<redacted>"
        else:
            data[key] = _redact(value)
    if message == str(exc) and data == (exc.data or {}):
        return exc
    return ExecdError(exc.code, message, data=data)


def _clip(value: str, limit: int = 200) -> str:
    collapsed = " ".join(str(value or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit // 2]} … {collapsed[-limit // 2 :]}"


def _q(value: str) -> str:
    return shlex.quote(value)


def read_agent_payload() -> bytes:
    """The pod-side agent, read out of the installed package.

    A missing payload is an explicit failure, not a silent no-op: an install that quietly had nothing to
    install would leave the caller talking to whatever agent an older version left behind.
    """
    try:
        data = resources.files("inspire.platform.execd").joinpath(AGENT_FILENAME).read_bytes()
    except Exception as exc:  # noqa: BLE001 - resources raises a different shape per backend
        raise ExecdError(
            "agent-payload-missing",
            f"the packaged execd agent was not found ({type(exc).__name__}: {_redact(_clip(str(exc)))}); "
            "this install cannot proceed, and it will not fall back to the legacy exec path",
        ) from exc
    if not data:
        raise ExecdError(
            "agent-payload-missing",
            "the packaged execd agent is empty; refusing to install a zero-byte agent",
        )
    return data


def agent_path(digest: str) -> str:
    """The versioned install path for one payload digest."""
    return posixpath.join(VERSIONS_DIR, digest, AGENT_FILENAME)


def log_path(digest: str) -> str:
    return posixpath.join(LOG_DIR, f"serve-{digest[:12]}.log")


# ---------------------------------------------------------------------------------------------
# transport primitives
# ---------------------------------------------------------------------------------------------


def _session_run(
    session: Any,
    command: str,
    timeout: int,
    *,
    what: str,
    max_bytes: int | None = None,
) -> tuple[int | None, str, bool]:
    """Run one command on the caller's session. Never quotes the command: it can carry payload bytes.

    Returns ``(exit_code, stdout, truncated)``. A ``None`` exit code means *we stopped waiting*, which is a
    statement about the transport and not a command failure - the distinction `ws_exec` publishes and this
    module has to keep, because reading "no answer" as "it worked" is how a half-installed agent looks
    installed.
    """
    try:
        result = session.run(command, timeout, max_bytes=max_bytes)
    except ExecdError:
        raise
    except Exception as exc:  # noqa: BLE001 - a dropped PTY raises whatever it likes
        raise ExecdError(
            "transport-lost",
            f"{what}: the exec session dropped ({_redact(_clip(str(exc)))})",
            data={"retryable": True},
        ) from exc
    payload = result if isinstance(result, dict) else {}
    raw_code = payload.get("exit_code")
    code = None if raw_code is None else int(raw_code)
    out = str(payload.get("stdout") or payload.get("output") or "")
    return code, out, bool(payload.get("truncated"))


def _command(
    session: Any,
    command: str,
    timeout: int,
    *,
    what: str,
    max_bytes: int | None = None,
) -> str:
    """Run one command that must succeed. A failure names `what`, the exit code and a redacted tail."""
    code, out, _ = _session_run(session, command, timeout, what=what, max_bytes=max_bytes)
    if code is None:
        raise ExecdError(
            "transport-lost",
            f"{what}: the exec session returned no exit code, so the outcome is unknown",
            data={"retryable": True, "observed": _redact(_clip(out))},
        )
    if code != 0:
        raise ExecdError(
            "remote-command-failed",
            f"{what} failed (exit {code}): {_redact(_clip(out))}",
            data={"exit_code": code, "observed": _redact(_clip(out))},
        )
    return out


# ---------------------------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------------------------

# No single quotes anywhere below: the script travels inside a shell single-quoted argument.
_PREFLIGHT_SCRIPT = (
    "import os, shutil, socket, sys\n"
    "def _afunix(root):\n"
    "    p = os.path.join(root, '.probe-' + str(os.getpid()) + '.sock')\n"
    "    s = None\n"
    "    try:\n"
    "        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
    "        s.bind(p)\n"
    "        return 1\n"
    "    except OSError:\n"
    "        return 0\n"
    "    finally:\n"
    "        if s is not None:\n"
    "            s.close()\n"
    "        try:\n"
    "            os.unlink(p)\n"
    "        except OSError:\n"
    "            pass\n"
    "root = sys.argv[1]\n"
    "try:\n"
    "    os.makedirs(root, exist_ok=True)\n"
    "except OSError as exc:\n"
    "    print('EXECD-PREFLIGHT root=0 tmp_w=0 tmp_x=0 afunix=0 free=0 inodes=0 error='"
    " + type(exc).__name__)\n"
    "    sys.exit(0)\n"
    "try:\n"
    "    usage = shutil.disk_usage(root)\n"
    "    inodes = int(os.statvfs(root).f_ffree)\n"
    "except OSError as exc:\n"
    "    print('EXECD-PREFLIGHT root=1 tmp_w=0 tmp_x=0 afunix=0 free=0 inodes=0 error='"
    " + type(exc).__name__)\n"
    "    sys.exit(0)\n"
    "print('EXECD-PREFLIGHT root=1 tmp_w=%d tmp_x=%d afunix=%d free=%d inodes=%d' % (\n"
    "    1 if os.access('/tmp', os.W_OK) else 0,\n"
    "    1 if os.access('/tmp', os.X_OK) else 0,\n"
    "    _afunix(root),\n"
    "    int(usage.free),\n"
    "    inodes))\n"
)

_PREFLIGHT_FAILURES = (
    ("root", "execd-dir-unusable", f"cannot create the execd directory {EXECD_ROOT}"),
    ("tmp_w", "tmp-not-writable", "/tmp is not writable by the pod user"),
    (
        "tmp_x",
        "tmp-not-executable",
        "/tmp is not an executable directory, so no path under it can be executed",
    ),
    (
        "afunix",
        "unix-sockets-unavailable",
        "AF_UNIX sockets cannot be bound under /tmp on this pod",
    ),
)


def _preflight(session: Any, *, needed_bytes: int, timeout: int) -> int:
    """Prove the pod can host a daemon before a byte is sent, and name which prerequisite failed.

    Two commands. The first only asks whether `python3` exists, because every other prerequisite is checked
    *with* python3 - the agent needs it, so there is no fallback probe. The second reports the directory,
    /tmp's mode, whether an AF_UNIX socket can actually be bound (some shared mounts refuse it, which no amount
    of retrying fixes), and the space and inodes available. The floors are the daemon's own: it refuses a new
    execution below them, so an install that proceeds under them installs something that cannot run anything.
    """
    probe = _command(
        session,
        f"{PYTHON3} -V >/dev/null 2>&1 && echo EXECD-PY-OK || echo EXECD-NO-PY",
        timeout,
        what="python3 availability check",
    )
    if "EXECD-PY-OK" not in probe:
        raise ExecdError(
            "python3-missing",
            "the pod has no python3, which execd needs to run; refusing to fall back to the legacy "
            "shell exec path",
        )

    out = _command(
        session,
        f"{PYTHON3} -c {_q(_PREFLIGHT_SCRIPT)} {_q(EXECD_ROOT)}",
        timeout,
        what="execd preflight",
    )
    match = _PREFLIGHT_RE.search(out)
    if match is None:
        raise ExecdError(
            "preflight-unparseable",
            f"execd preflight answered without its marker: {_redact(_clip(out))}",
        )
    flags = match.groupdict()
    for key, code, message in _PREFLIGHT_FAILURES:
        if flags.get(key) != "1":
            raise ExecdError(code, f"{message}; execd will not run here")
    free = int(flags["free"])
    if free < needed_bytes:
        raise ExecdError(
            "tmp-out-of-space",
            f"/tmp has {free} bytes free and this install needs {needed_bytes}; nothing was uploaded",
            data={"free_bytes": free, "needed_bytes": needed_bytes},
        )
    inodes = int(flags["inodes"])
    if inodes < MIN_FREE_INODES:
        raise ExecdError(
            "tmp-out-of-inodes",
            f"/tmp has {inodes} free inodes and execd needs {MIN_FREE_INODES} to hold records; nothing was "
            "uploaded",
            data={"free_inodes": inodes, "needed_inodes": MIN_FREE_INODES},
        )
    return free


# ---------------------------------------------------------------------------------------------
# numbered-chunk transfer
# ---------------------------------------------------------------------------------------------


def _existing_chunks(session: Any, directory: str, timeout: int) -> dict[str, int]:
    """Sizes of the chunk files already on the pod. Read-only, so a drop costs a re-ask."""
    out = _command(
        session, f"wc -c {directory}/part.* 2>/dev/null || true", timeout, what="chunk probe"
    )
    sizes: dict[str, int] = {}
    for line in out.splitlines():
        match = _CHUNK_SIZE_RE.match(line.strip())
        if not match:
            continue
        name = posixpath.basename(match.group("path"))
        if name.startswith("part."):
            sizes[name] = int(match.group("size"))
    return sizes


#: A transfer namespace becomes one directory name under `XFER_DIR`: the directory chunks are written into, the
#: glob `cat <dir>/part.*` assembles from, and - once a transfer that owns its namespace has finished - the
#: directory that gets removed. All three interpolate it into a shell command, and one of them deletes through
#: it, so it is validated where the path is computed rather than trusted at each call site: a namespace with a
#: slash, a `..`, or a shell metacharacter in it would otherwise be a delete - or a command - outside the
#: transfer area. Both namespaces the code actually uses (a sha256, and `rpc-<hex>`) match it easily.
_NAMESPACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")


def _xfer_directory(namespace: str) -> str:
    """``<root>/xfer/<namespace>``, once `namespace` is proven to be one safe path segment."""
    if not _NAMESPACE_RE.fullmatch(namespace or ""):
        raise ExecdError(
            "invalid-transfer-namespace",
            "a transfer namespace must be one safe path segment (letters, digits, '.', '_' or '-'), "
            f"got {namespace!r}",
        )
    return posixpath.join(XFER_DIR, namespace)


def _cleanup_transfer_command(directory: str) -> str:
    """The command that removes one finished transfer's own files, and nothing that could be another's.

    It names two things only: the numbered chunks and the ``assembled`` staging file, both inside `directory`.
    There is no ``-r`` anywhere, and no form of it that could reach a sibling namespace. The directory itself
    is removed with ``rmdir``, which the kernel grants only for an empty directory - so if anything this call
    does not own is still in there, it stays, and the cleanup is forced not to care (``|| true``). The whole
    point of that shape is that a cleanup can fail to remove something but cannot remove the wrong something.
    """
    return (
        f"rm -f {directory}/part.* {directory}/assembled 2>/dev/null; "
        f"rmdir {directory} 2>/dev/null || true"
    )


def _cleanup_transfer(session: Any, *, namespace: str, timeout: int) -> None:
    """Drop a finished transfer's chunks. Best effort by design: what it follows has already succeeded."""
    try:
        _command(
            session,
            _cleanup_transfer_command(_xfer_directory(namespace)),
            timeout,
            what="transfer cleanup",
        )
    except ExecdError:
        # The cost of a lost cleanup is one directory of inert chunks on /tmp. The cost of reporting it as a
        # failure would be worse: the payload is installed and the caller would be told the operation failed,
        # inviting a re-run of an RPC whose answer it may already hold - which is how one execution becomes
        # two. The chunks are removed on the next successful transfer of the same namespace instead.
        pass


def _push_chunks(
    session: Any,
    *,
    namespace: str,
    data: bytes,
    chunk_bytes: int,
    timeout: int,
    sleep: Callable[[float], None],
    chunk_attempts: int = 3,
    retry_delay: float = 0.5,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """Write `data` into ``<root>/xfer/<namespace>/part.NNNNN`` and report ``(sent, reused)``.

    Each chunk is an independent file written through a temporary name and renamed, so re-sending a chunk whose
    answer we lost replaces it byte for byte. That retires the uploader's hardest rule - "never resume a
    partially appended chunk" - by removing appends from the design entirely.

    Reuse is decided by a size probe, not a per-chunk digest: a checksum call per chunk doubles the round trips,
    which is the part of this channel that fails. The authoritative check is the whole-file digest in
    `_transfer_payload`, after assembly and before anything is installed, so a chunk reused on a size
    coincidence can never reach an installed file.
    """
    directory = _xfer_directory(namespace)
    _command(session, f"mkdir -p {_q(directory)}", timeout, what="transfer directory")
    try:
        existing = _existing_chunks(session, directory, timeout)
    except ExecdError as exc:
        # The probe is an optimisation. A channel that will not answer it must not abort an install that has
        # not written a byte yet, so assume nothing is present and send everything.
        if exc.code not in {"transport-lost", "remote-command-failed"}:
            raise
        existing = {}

    total = len(data)
    blocks = [data[start : start + chunk_bytes] for start in range(0, total, chunk_bytes)] or [b""]
    sent = reused = 0
    for index, block in enumerate(blocks):
        name = f"part.{index:05d}"
        path = posixpath.join(directory, name)
        if existing.get(name) == len(block):
            reused += 1
        else:
            encoded = base64.b64encode(block).decode("ascii")
            # Rename rather than append: a chunk left half-written by a dropped session keeps its `.tmp` name,
            # so the size probe above can never count it as complete and a resume cannot inherit it.
            command = (
                f"printf %s {_q(encoded)} | base64 -d > {_q(path + '.tmp')} && "
                f"mv {_q(path + '.tmp')} {_q(path)} && echo CHUNK {index} $(wc -c < {_q(path)})"
            )
            for attempt in range(1, max(1, chunk_attempts) + 1):
                code, out, _ = _session_run(
                    session, command, timeout, what=f"chunk {index} ({len(block)} bytes)"
                )
                if code == 0 and f"CHUNK {index} {len(block)}" in out:
                    break
                if attempt >= chunk_attempts:
                    raise ExecdError(
                        "transport-lost" if code is None else "remote-command-failed",
                        f"chunk {index} of {len(blocks)} did not land after {attempt} attempts"
                        + (f": {_redact(_clip(out))}" if code not in (None, 0) else "")
                        + f"; partial files were left under {directory} and are inert",
                        data={
                            "chunk": index,
                            "path": path,
                            "retryable": code is None,
                            "observed": _redact(_clip(out)),
                        },
                    )
                sleep(retry_delay)
            sent += 1
        if progress is not None:
            progress(min(total, (index + 1) * chunk_bytes), total)
    return sent, reused


def _digest_matches(session: Any, path: str, expected: bytes, timeout: int) -> bool:
    """True when the remote file already holds exactly `expected`. An unreadable file is not a match."""
    try:
        out = _command(
            session,
            f"sha256sum {_q(path)} 2>/dev/null || openssl dgst -sha256 {_q(path)}",
            timeout,
            what="remote checksum",
        )
    except ExecdError:
        return False
    return _parse_digest(out) == hashlib.sha256(expected).hexdigest()


def _transfer_payload(
    session: Any,
    *,
    namespace: str,
    data: bytes,
    destination: str,
    prepare_directories: tuple[str, ...],
    chunk_bytes: int,
    timeout: int,
    sleep: Callable[[float], None],
    progress: Callable[[int, int], None] | None = None,
    exclusive_namespace: bool = False,
) -> tuple[int, int]:
    """Push `data` as chunks, verify the digest of the whole, then rename it into `destination`.

    Assembly globs zero-padded names, so lexical order *is* numeric order - ``part.1`` through ``part.10`` would
    reassemble out of order and still produce a plausible-looking file, which is the kind of corruption an
    end-of-transfer digest is the last line of defence against rather than the first.

    The rename is the install: both paths live under `EXECD_ROOT`, so it is a same-filesystem rename and a
    reader either sees no agent at that path or the complete one.

    ``exclusive_namespace`` says this namespace belongs to this call alone, and it is what decides whether the
    chunks are swept afterwards. A big RPC stages its request under ``rpc-<id>``, which nothing will ever read
    again once the request file exists, so leaving it behind is one directory of dead chunks per call. The
    agent payload transfers under its own digest instead, which is a content-addressed resume cache: an
    interrupted install reuses those chunks instead of re-uploading 140 KB through a PTY, and another client
    installing the same version may be mid-transfer in the same directory right now. Both meanings are real, so
    the flag states which one the caller meant rather than guessing from the namespace's shape.

    Nothing is removed unless the install succeeded. Every earlier failure leaves its chunks where they are -
    they are the evidence a resume reuses, and the reason a half-finished transfer is worth diagnosing rather
    than erasing.
    """
    directory = _xfer_directory(namespace)
    sent, reused = _push_chunks(
        session,
        namespace=namespace,
        data=data,
        chunk_bytes=chunk_bytes,
        timeout=timeout,
        sleep=sleep,
        progress=progress,
    )
    assembled = posixpath.join(directory, "assembled")
    for target in prepare_directories:
        _command(session, f"mkdir -p {_q(target)}", timeout, what="install directory")
    _command(session, f"cat {directory}/part.* > {_q(assembled)}", timeout, what="chunk assembly")
    digest = hashlib.sha256(data).hexdigest()
    if not _digest_matches(session, assembled, data, timeout):
        raise ExecdError(
            "integrity-check-failed",
            f"the reassembled payload at {assembled} does not match the local sha256 {digest}; it was not "
            "installed, and the chunk files under that directory are inert",
            data={"path": assembled, "expected_sha256": digest},
        )
    _command(session, f"mv {_q(assembled)} {_q(destination)}", timeout, what="atomic install")
    if exclusive_namespace:
        _cleanup_transfer(session, namespace=namespace, timeout=timeout)
    return sent, reused


def _parse_digest(stdout: str) -> str:
    """Read a sha256 out of `sha256sum` or `openssl dgst -sha256` output."""
    for line in (stdout or "").splitlines():
        for token in reversed(line.split()):
            if re.fullmatch(r"[0-9a-f]{64}", token.lower()):
                return token.lower()
    return ""


def decode_output(encoded: Any) -> bytes:
    """Decode a base64 output page. Absent is empty; unparseable is an error, never empty output.

    Whitespace inside the payload is ignored because the bytes travel as text, but nothing else is forgiven: a
    decoder that silently dropped malformed characters would turn a corrupted page into a record that looks like
    it printed less than it did.
    """
    if encoded is None or encoded == "":
        return b""
    if not isinstance(encoded, str):
        raise ExecdError("invalid-response", "an execd output page is not text")
    body = re.sub(r"\s+", "", encoded)
    try:
        return base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ExecdError(
            "invalid-response",
            f"an execd output page is not base64: {type(exc).__name__}",
        ) from exc


# ---------------------------------------------------------------------------------------------
# ensure
# ---------------------------------------------------------------------------------------------


def ensure_agent(
    session: Any,
    *,
    payload: bytes | None = None,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    command_timeout: int = DEFAULT_COMMAND_TIMEOUT,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    start_timeout: float = DEFAULT_START_TIMEOUT,
    poll_interval: float = DEFAULT_HEALTH_POLL,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[int, int], None] | None = None,
) -> AgentHandle:
    """Make sure this pod is running *our* agent, on the session we were handed.

    Order: preflight, digest-skip or install, health. A daemon is started only when health *answered* that no
    daemon is running. Adoption is the common case, starting is the exception, and guessing is never allowed - a
    lost health read does not establish that the state directory is free, and the wrong answer there costs
    whoever is already using this pod their daemon.

    Nothing older than this payload's own versioned path is removed, and no directory is removed at all: the pod
    is shared, and another version's records and logs are somebody else's evidence.
    """
    data = read_agent_payload() if payload is None else payload
    if not data:
        raise ExecdError("agent-payload-missing", "refusing to install an empty agent payload")
    if len(data) > max_payload_bytes:
        raise ExecdError(
            "payload-too-large",
            f"the agent payload is {len(data)} bytes, over the {max_payload_bytes} byte exec-channel limit",
        )
    digest = hashlib.sha256(data).hexdigest()
    path = agent_path(digest)

    free = _preflight(
        session,
        needed_bytes=max(2 * len(data) + _MIN_TRANSFER_MARGIN_BYTES, MIN_FREE_BYTES),
        timeout=command_timeout,
    )

    uploaded = True
    sent = reused = 0
    if _digest_matches(session, path, data, command_timeout):
        uploaded = False
    else:
        sent, reused = _transfer_payload(
            session,
            namespace=digest,
            data=data,
            destination=path,
            prepare_directories=(posixpath.dirname(path), LOG_DIR),
            chunk_bytes=chunk_bytes,
            timeout=command_timeout,
            sleep=sleep,
            progress=progress,
        )

    handle = AgentHandle(
        path=path,
        digest=digest,
        state_dir=STATE_DIR,
        socket_path=posixpath.join(STATE_DIR, "daemon.sock"),
        agent_version=AGENT_VERSION,
        uploaded=uploaded,
        started=False,
        chunks_sent=sent,
        chunks_reused=reused,
        free_bytes=free,
    )

    state, health = _try_health(session, handle, command_timeout)
    if state == _HEALTH_ANSWERED:
        _assert_adoptable(health, digest)
        return replace(handle, agent_version=_reported_version(health))
    if state == _HEALTH_UNKNOWN:
        # Nothing answered us. Absence of a daemon is therefore not established, and starting one is not
        # licensed - the one branch here where retrying would be cheaper and less honest.
        raise ExecdError(
            "health-unconfirmed",
            "the execd agent is installed but its health could not be read, so this install will not start a "
            "daemon on a state directory it cannot prove is free; re-run once the channel recovers",
            data={"path": path, "state_dir": STATE_DIR},
        )

    _start_serving(session, handle, command_timeout)
    deadline = time.monotonic() + max(1.0, float(start_timeout))
    while True:
        state, health = _try_health(
            session, handle, command_timeout, connect_wait=max(poll_interval, DEFAULT_CONNECT_WAIT)
        )
        if state == _HEALTH_ANSWERED:
            _assert_adoptable(health, digest)
            return replace(handle, started=True, agent_version=_reported_version(health))
        if time.monotonic() >= deadline:
            break
        sleep(poll_interval)
    raise ExecdError(
        "agent-start-failed",
        f"the execd daemon did not answer health within {int(start_timeout)}s; it may still be starting, and "
        f"nothing here was removed - check {log_path(digest)}",
        data={"path": path, "state_dir": STATE_DIR, "log": log_path(digest)},
    )


def _reported_version(health: dict[str, Any]) -> str:
    return str(health.get("agent_version") or AGENT_VERSION)


def _assert_adoptable(health: dict[str, Any], digest: str) -> None:
    """Refuse to drive a daemon that cannot prove it is running the bytes we installed.

    Two agents on one pod are not something to settle by killing one: the older daemon may be carrying another
    user's or another agent's running commands, and the deliberate path is a cleanup the caller asks for by
    name, never a side effect of an install.

    So this fails closed. The daemon must report `agent_sha256` equal to this payload's digest. A matching
    `agent_version` alone is not adopted: version strings are shared across builds far more readily than bytes
    are, so agreement on one proves very little next to disagreement on the other. A daemon that reports no
    digest at all is refused rather than adopted, because from here "different agent" and "agent that will not
    say" are the same exposure - and adopting the second is how a client ends up driving an agent whose
    semantics it cannot check. The code says which of the two it was, so a caller can tell the operator to
    upgrade the daemon rather than to stop it.
    """
    reported = str(health.get("agent_sha256") or health.get("sha256") or "")
    version = str(health.get("agent_version") or "")
    if not reported:
        raise ExecdError(
            "agent-identity-unconfirmed",
            "the execd daemon on this pod did not say which agent it is running"
            + (f" (it reports version {version})" if version else "")
            + f", and this client will not drive an agent it cannot match to the payload it installed "
            f"({digest}); an agent that reports no digest is too old for this protocol - upgrade it",
            data={"running_version": version, "local_sha256": digest},
        )
    if reported != digest:
        raise ExecdError(
            "agent-version-mismatch",
            f"a different execd agent is already serving this pod (running {reported}, this client has "
            f"{digest}); stop it deliberately rather than letting an install displace it",
            data={"running_sha256": reported, "local_sha256": digest, "running_version": version},
        )
    if version and version != AGENT_VERSION:
        raise ExecdError(
            "agent-version-mismatch",
            f"a different execd agent version is already serving this pod (running {version}, this client "
            f"ships {AGENT_VERSION})",
            data={"running_version": version, "local_version": AGENT_VERSION},
        )


def _start_serving(session: Any, handle: AgentHandle, timeout: int) -> None:
    """Launch the daemon detached, so it outlives the session that started it.

    `setsid` matters as much as `nohup`: without a new session, a PTY hangup still reaches the process group.
    The log is appended, never rotated away - an older daemon's log is the only record of what it was doing,
    and `serve` prints its one-line `serving` event there.
    """
    log = log_path(handle.digest)
    command = (
        f"( umask 077; mkdir -p {_q(LOG_DIR)} && "
        f"setsid nohup {PYTHON3} {_q(handle.path)} serve --state-dir {_q(handle.state_dir)} "
        f">> {_q(log)} 2>&1 < /dev/null & echo EXECD-STARTED )"
    )
    out = _command(session, command, timeout, what="execd daemon launch")
    if "EXECD-STARTED" not in out:
        raise ExecdError(
            "agent-start-failed",
            "the execd launch command returned without its marker, so it is unknown whether a daemon started; "
            f"check {log} and re-run - this call will not retry blindly",
            data={"log": log},
        )


def _try_health(
    session: Any,
    handle: AgentHandle,
    timeout: int,
    *,
    connect_wait: float = 0.0,
) -> tuple[str, dict[str, Any]]:
    """Ask the daemon how it is, sorting *why* we did not get an answer.

    "The agent said no daemon is running" and "we could not ask" are different facts that license different
    actions, which is why this returns three states instead of raising or returning None.
    """
    try:
        data = run_rpc(session, handle, "health", {}, timeout=timeout, connect_wait=connect_wait)
    except ExecdError as exc:
        if exc.code in NOT_RUNNING_CODES:
            return _HEALTH_ABSENT, {}
        if exc.code in {
            "transport-lost",
            "response-lost",
            "agent-command-failed",
            "rpc-interrupted",
            "invalid-response",
        }:
            return _HEALTH_UNKNOWN, {}
        raise
    return _HEALTH_ANSWERED, data


# ---------------------------------------------------------------------------------------------
# framed RPC over a pod-side spool
# ---------------------------------------------------------------------------------------------

# The pager travels as a `shlex.quote`d argument, so the quotes inside it are escaped by the quoting and are
# not a shell-quoting hazard; keeping the script free of `$` is what actually matters, because the shell
# expands the pager's own arguments (`$rc` below) before python ever sees them.
_PAGER_SCRIPT = (
    "import base64, hashlib, sys\n"
    "path, offset, limit, rid, rc = (sys.argv[1], int(sys.argv[2]), int(sys.argv[3]),\n"
    "                                sys.argv[4], int(sys.argv[5]))\n"
    "def fail(state, code, err):\n"
    "    print('EXECD-RPC-NODATA %s rc=%d state=%s err=%s' % (rid, code, state, err))\n"
    "    sys.exit(0)\n"
    "def tail_of(suffix):\n"
    "    try:\n"
    "        return ' '.join(open(path + suffix, 'rb').read()[-400:].decode('utf-8', 'replace').split())\n"
    "    except OSError:\n"
    "        return ''\n"
    "def recorded_rc():\n"
    "    try:\n"
    "        return int(open(path + '.done', 'rb').read().strip() or 0)\n"
    "    except (OSError, ValueError):\n"
    "        return rc\n"
    "try:\n"
    "    data = open(path, 'rb').read()\n"
    "except OSError as exc:\n"
    "    fail('missing', rc, type(exc).__name__)\n"
    "if not data:\n"
    "    import os\n"
    "    if os.path.exists(path + '.done'):\n"
    "        fail('empty', recorded_rc(), tail_of('.err'))\n"
    "    fail('interrupted', recorded_rc(), tail_of('.err'))\n"
    "total = len(data)\n"
    "chunk = data[offset:offset + limit] if offset < total else b''\n"
    "print('EXECD-RPC %s total=%d sha256=%s offset=%d'\n"
    "      % (rid, total, hashlib.sha256(data).hexdigest(), offset))\n"
    "sys.stdout.write(base64.b64encode(chunk).decode('ascii'))\n"
    "sys.stdout.write('\\n')\n"
    "print('EXECD-RPC-END %s offset=%d bytes=%d' % (rid, offset + len(chunk), len(chunk)))\n"
)


def _spool_paths(handle: AgentHandle, namespace: str, rpc_id: str) -> tuple[str, str, str]:
    directory = posixpath.join(RPC_DIR, re.sub(r"[^A-Za-z0-9_.-]", "-", namespace or "default"))
    spool = posixpath.join(directory, f"{rpc_id}.resp")
    return directory, spool, posixpath.join(directory, f"{rpc_id}.req")


def _prune_command(directory: str, keep: list[str]) -> str:
    """Remove this namespace's older spools, keeping everything this call can still need.

    Namespaced per client and pruned only inside that namespace, because two clients on one pod must not delete
    each other's spools - that would cost the other one its ability to re-read a lost answer, and (since the
    spool file's existence is what proves an RPC already ran) its protection against re-executing it. The
    `.err` and `.done` siblings are kept for the same reason as the spool: they are the only evidence about an
    attempt that produced no answer, and pruning them would rewrite "it ran and failed" into "it never ran".
    The cost of *not* pruning is a spool per poll, which on a long `follow` is hundreds of files on /tmp.
    """
    patterns = "|".join(_q(path) for path in keep)
    return (
        f'for f in {directory}/*; do [ -e "$f" ] || continue; '
        f'case "$f" in {patterns}) : ;; *) rm -f "$f" ;; esac; done; '
    )


def _call_command(
    handle: AgentHandle,
    rpc_id: str,
    method: str,
    *,
    spool_dir: str,
    spool: str,
    request_file: str | None,
    params_json: str | None,
    offset: int,
    limit: int,
    shim_timeout: int,
    connect_wait: float,
) -> str:
    """One command: run the RPC only if this call is the one that creates the spool, then print one frame.

    The execution token is the spool's *existence*, created under `noclobber` so that exactly one invocation can
    claim it. The earlier guard was `[ -s spool ]`, and an empty file was indistinguishable from no file: an
    RPC that ran and answered with nothing would be executed again by the next read. Now the first call to
    claim the id creates the file and runs the RPC, and every later read of that id - whatever the spool
    contains, including nothing - is a read. The pager's offsets are pure reads, which is what makes resume
    across a reconnect possible at all: a re-read that cost an execution would not be a re-read.

    `<spool>.done` holds the RPC's exit status and is written once the RPC process has finished, so an attempt
    that died mid-way is distinguishable from one that completed and answered with nothing; both are reported
    to the caller and neither is retried under this id. A deliberate retry is a new id, and for `run` a new id
    still carries the same client-generated `exec_id`, which the daemon recognises.
    """
    keep = [spool, spool + ".err", spool + ".done"]
    if request_file is not None:
        keep.append(request_file)
    if request_file is not None:
        params_arg = f"--json - < {_q(request_file)}"
    else:
        params_arg = f"--json {_q(params_json or '{}')}"
    wait_arg = f" --wait {float(connect_wait):.2f}" if connect_wait > 0 else ""
    rpc = (
        f"{PYTHON3} {_q(handle.path)} rpc --state-dir {_q(handle.state_dir)} "
        f"--timeout {max(1, int(shim_timeout))}{wait_arg} {params_arg} {_q(method)}"
    )
    pager = (
        f"{PYTHON3} -c {_q(_PAGER_SCRIPT)} {_q(spool)} {max(0, int(offset))} "
        f"{max(1, int(limit))} {_q(rpc_id)} $rc"
    )
    claim = f"( set -o noclobber; : > {_q(spool)} ) 2>/dev/null"
    return (
        f"( umask 077; mkdir -p {_q(spool_dir)}; rc=0; {_prune_command(spool_dir, keep)}"
        f"if {claim}; then {rpc} > {_q(spool)} 2> {_q(spool + '.err')} || rc=$?; "
        f'echo "$rc" > {_q(spool + ".done")}; fi; {pager} )'
    )


def _decode_frame(text: str, rpc_id: str, expected_offset: int) -> _Frame | None:
    """Find one complete frame in a merged PTY stream. Absent, partial or inconsistent is ``None``.

    "Partial" deliberately includes a frame whose head was dropped by the transport's tail cap: the caller
    re-reads the same offset instead of base64-deleting its way through half a response.

    A `NODATA` marker is checked first because it *replaces* a frame: the pager prints it when there is no
    response to page, and reading it as "no frame here" would send the caller re-reading an offset that does
    not exist. `state=interrupted` is kept a separate code from `agent-command-failed` because the two say
    different things about the pod - one answer was never written, the other was written and is an error.
    """
    nodata = _FRAME_NODATA_RE.search(text or "")
    if nodata is not None and nodata.group("id") == rpc_id:
        rc, err = int(nodata.group("rc")), _redact(_clip(nodata.group("err")))
        observed = {"exit_code": rc, "observed": err}
        if nodata.group("state") == "interrupted":
            raise ExecdError(
                "rpc-interrupted",
                "the execd rpc command was cut off before it wrote a response (exit "
                f"{rc}{': ' + err if err else ''}); nothing exists at this response id and re-reading "
                "will not produce one - if the operation is meant to happen it needs a new request id",
                data={**observed, "retryable": False},
            )
        raise ExecdError(
            "agent-command-failed",
            f"the execd rpc command produced no response to read (exit {rc})"
            + (f": {err}" if err else ""),
            data=observed,
        )
    begin = _FRAME_BEGIN_RE.search(text or "")
    if begin is None or begin.group("id") != rpc_id:
        return None
    end = _FRAME_END_RE.search(text or "", begin.end())
    if end is None or end.group("id") != rpc_id:
        return None
    total, sha, offset = int(begin.group("total")), begin.group("sha"), int(begin.group("offset"))
    next_offset, emitted = int(end.group("offset")), int(end.group("bytes"))
    if offset != expected_offset or next_offset < offset or emitted != next_offset - offset:
        raise ExecdError(
            "protocol-mismatch",
            f"execd response frame is self-inconsistent (offset {offset} to {next_offset}, "
            f"declared {emitted} bytes)",
        )
    body = re.sub(r"\s+", "", text[begin.end() : end.start()])
    try:
        payload = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ExecdError(
            "invalid-response",
            f"execd response body is not base64: {type(exc).__name__}",
        ) from exc
    if len(payload) != emitted:
        raise ExecdError(
            "integrity-check-failed",
            f"execd response frame declared {emitted} bytes and carried {len(payload)}",
        )
    return _Frame(total=total, sha256=sha, offset=offset, next_offset=next_offset, payload=payload)


def run_rpc(
    session: Any,
    handle: AgentHandle,
    method: str,
    args: dict[str, Any],
    *,
    timeout: int = DEFAULT_COMMAND_TIMEOUT,
    wait_seconds: float = 0.0,
    connect_wait: float = 0.0,
    namespace: str = "default",
    response_limit: int = DEFAULT_RESPONSE_LIMIT,
    lost_read_attempts: int = DEFAULT_LOST_READ_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """One RPC: run it once, read its framed answer, return the unwrapped ``data``.

    Re-reads resume at the last verified offset and never re-execute, because the spool on the pod is keyed by
    this call's generated id. Re-running an RPC whose answer we lost is how one ``run`` becomes two, so this
    function re-reads instead.

    ``wait_seconds`` is the daemon's own attach long-poll, and both the shim's socket timeout and the PTY read
    window are widened to outlast it: a long-poll that our own deadline cuts short is a long-poll that produced
    an error instead of output.
    """
    rpc_id = secrets.token_hex(8)
    spool_dir, spool, request_file = _spool_paths(handle, namespace, rpc_id)

    encoded = json.dumps(args or {}, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    params_json: str | None = None
    if len(encoded) <= MAX_INLINE_REQUEST_BYTES:
        params_json = encoded.decode("utf-8")
    else:
        # A big request is a payload like any other: numbered chunks, digest-verified, renamed into place, and
        # then handed to the shim on stdin. A 200 KiB command line through a PTY is the failure this whole
        # transfer path exists to prevent. The namespace carries this call's id, so it is ours alone and is
        # swept away once the request file exists - unlike the payload cache, a request nobody will fetch
        # again is not worth keeping, and one directory per large RPC is how /tmp fills up.
        _transfer_payload(
            session,
            namespace=f"rpc-{rpc_id}",
            data=encoded,
            destination=request_file,
            prepare_directories=(posixpath.dirname(request_file),),
            chunk_bytes=DEFAULT_CHUNK_BYTES,
            timeout=timeout,
            sleep=sleep,
            exclusive_namespace=True,
        )

    limit = max(1024, int(response_limit))
    read_window = int(timeout) + max(0, int(wait_seconds)) + 15
    buffer = bytearray()
    offset = 0
    total: int | None = None
    expected_sha = ""
    lost_reads = 0
    while total is None or offset < total:
        command = _call_command(
            handle,
            rpc_id,
            method,
            spool_dir=spool_dir,
            spool=spool,
            request_file=request_file if params_json is None else None,
            params_json=params_json,
            offset=offset,
            limit=limit,
            shim_timeout=int(timeout) + max(0, int(wait_seconds)),
            connect_wait=connect_wait,
        )
        code, out, truncated = _session_run(
            session, command, read_window, what=f"execd rpc {method}", max_bytes=RESPONSE_READ_CAP
        )
        frame = _decode_frame(out, rpc_id, offset) if out else None
        if frame is None:
            if code is None or truncated:
                # Either the session dropped or our own read ceiling clipped the frame. Shrink the window so the
                # next read can actually land, then ask for the same offset again.
                lost_reads += 1
                if truncated and limit > 1024:
                    limit = max(1024, limit // 2)
                if lost_reads > max(1, lost_read_attempts):
                    raise ExecdError(
                        "response-lost",
                        f"execd rpc {method} produced no readable response frame at offset {offset} after "
                        f"{lost_reads} reads; the daemon may well have answered and we simply did not hear it",
                        data={
                            "method": method,
                            "offset": offset,
                            "rpc_id": rpc_id,
                            "retryable": True,
                        },
                    )
                sleep(0.5)
                continue
            if code == 0:
                raise ExecdError(
                    "invalid-response",
                    f"execd rpc {method} exited 0 without a response frame: {_redact(_clip(out))}",
                    data={"observed": _redact(_clip(out))},
                )
            raise ExecdError(
                "agent-command-failed",
                f"execd rpc {method} failed (exit {code}): {_redact(_clip(out))}",
                data={"exit_code": code, "observed": _redact(_clip(out))},
            )
        lost_reads = 0
        if total is None:
            total, expected_sha = frame.total, frame.sha256
        elif frame.total != total or frame.sha256 != expected_sha:
            raise ExecdError(
                "protocol-mismatch",
                "the execd response changed between frames, so the spool behind this response id is not the "
                "one this read started from",
            )
        buffer += frame.payload
        offset = frame.next_offset
        if total is not None and offset > total:
            raise ExecdError(
                "integrity-check-failed",
                f"execd returned {offset} bytes for a {total}-byte response",
            )

    body = bytes(buffer)
    if expected_sha and hashlib.sha256(body).hexdigest() != expected_sha:
        raise ExecdError(
            "integrity-check-failed",
            f"the reassembled execd response does not match its declared sha256 {expected_sha}",
            data={"method": method, "rpc_id": rpc_id},
        )
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExecdError(
            "invalid-response",
            f"execd response is not JSON: {type(exc).__name__}",
        ) from exc
    if not isinstance(parsed, dict):
        raise ExecdError("invalid-response", "execd returned a response that is not an object")
    try:
        return unwrap_response(parsed)
    except ExecdError as exc:
        raise _redact_error(exc) from None


__all__ = [
    "AGENT_FILENAME",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_COMMAND_TIMEOUT",
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "DEFAULT_RESPONSE_LIMIT",
    "EXECD_ROOT",
    "LOG_DIR",
    "MAX_INLINE_REQUEST_BYTES",
    "MIN_FREE_BYTES",
    "MIN_FREE_INODES",
    "NOT_RUNNING_CODES",
    "RPC_DIR",
    "STATE_DIR",
    "TRANSPORT_CODES",
    "VERSIONS_DIR",
    "XFER_DIR",
    "AgentHandle",
    "agent_path",
    "decode_output",
    "ensure_agent",
    "log_path",
    "read_agent_payload",
    "run_rpc",
]
