#!/usr/bin/env python3
"""Pod-side execd agent: one self-contained file, standard library only.

This file is copied into a pod and run there, so it must not import anything from ``inspire``.
That is why :class:`ExecdError`, ``response_ok``/``response_error`` and the exit-code frame below are
*copies* of ``inspire/platform/execd/protocol.py`` and ``inspire/platform/exec_frame.py`` rather than
imports: the contract test in ``tests/test_execd_agent.py`` compares the copies against the originals so
they cannot drift silently.

Three entry points, one file
----------------------------
``serve``     the daemon. Listens on a ``AF_UNIX`` stream socket under ``--state-dir`` and owns every
              record: it decides whether a ``run`` is new, whether it is a duplicate, and what the
              authoritative state of an execution is.
``rpc``       the short-lived shim a pod-side caller (or a test) actually invokes: read the daemon token
              from the state directory, send one JSON line, print one JSON line, exit. It holds no state.
``recorder``  the process that holds one execution. The daemon spawns it with ``start_new_session`` so it
              outlives the daemon; it starts the payload in its own process group, keeps stdout and stderr
              in separate files, and writes ``rc`` only after the payload is gone.

Durability rule
---------------
A record's metadata is written atomically (temp file, ``fsync``, ``os.replace``, directory ``fsync``)
*before* any process is forked. If the daemon dies between "decided" and "forked", the on-disk record says
"accepted with no pid", which is exactly the evidence a later scan needs to answer *uncertain* instead of
re-running a side effect. The daemon never relaunches anything: recovery only classifies what it finds.
An execution with no ``rc`` whose process is gone is ``outcome_uncertain``, full stop.

"Process is gone" is judged on identity, not on one pid. A record names its recorder as well as its payload,
and the recorder's ``starttime`` is stored with it, so a daemon that died in the window between "recorder is
forked" and "recorder wrote the payload's pid" adopts the run as the live execution it still is instead of
declaring uncertainty about a process that is running right now. ``cleanup`` reads the same identity: it
refuses a record that some still-live holder could write to, even when an earlier daemon pinned that record
as terminal, because unlinking a directory under a live process loses its output with nobody the wiser.

Who a cancel may signal
-----------------------
A direct run owns its process group, so the whole group goes. A run inside a named session shares the
session shell's group, so there the daemon walks the shell's process tree and keeps only the processes whose
environment still carries this run's ``INSPIRE_EXECD_EXEC_ID`` - the one reliable way to leave an *earlier*
run's background jobs alone. Both proofs are re-read at the moment of each signal rather than trusted from
the moment they were decided: the direct target carries the ``starttime`` the recorder wrote down and is
compared with the live process before TERM and again before SIGKILL (a pid freed during the grace window can
be somebody else's job by the second signal), and a session target re-reads its ``exec_id`` the same way. A
target with no anchor, or one whose identity moved, is never signalled. Neither is a payload that had already
exited when the cancel arrived: that is reported as nothing delivered, with the reason, and its own exit code
stands. What cannot be attributed is never signalled, and a cancel that delivered nothing writes no cancel
marker: an exit code the payload produced by itself must not be relabelled ``cancelled``.

Named sessions
--------------
A session is one persistent ``bash`` reading a FIFO, so cwd, exported variables and background jobs survive
from one run to the next. Each run's batch is staged as its own ``payload.sh`` and the FIFO is given a single
``. '<path>'`` line - never the script itself, because a half-delivered script would leave half a command in
the shell's input stream for the *next* caller to trip over. If even that one line is truncated, the session
is marked ``POISONED`` and refuses further work while that shell lives - a replacement shell starts with an
empty input stream, and is the only thing that clears the marker. Submitting to a session can
block on that FIFO, so it happens under the session's own lock and not the daemon's global one: a wedged
session must not stop ``health`` from answering about everything else.

Why the recorder is a separate process rather than a thread
-----------------------------------------------------------
Threads die with the interpreter. The whole point of this daemon is that an execution outlives the channel
that started it - including the daemon itself, which an operator may restart to upgrade it - so the holder
of a run must be a process in its own session. That is also what makes ``attach`` resumable across a daemon
restart: the payload's stdout was never a pipe we owned, it was a file the payload owned from exec time.

Security model
--------------
The state directory is 0700 and every file we create is 0600, with ``umask(077)`` set for the whole daemon
process. The socket is 0600, so only our own uid can open it at all; on top of that, every accepted
connection is checked with ``SO_PEERCRED`` and its uid must equal ours - a socket file in a directory we
own can still be reached through a bind mount or a passed fd, and the peer uid is the one thing the kernel
guarantees about the other end. Each request must also carry the daemon token, which ``serve`` generates
with :mod:`secrets` and stores in the state directory. The token deliberately never travels through the
browser PTY that carries our command text on this platform: it is read from the file system by ``rpc``,
locally, so a PTY tap cannot replay an RPC. Records are never deleted automatically; ``cleanup`` removes
one terminal record by exact path.

Two more things follow from sharing a pod. ``status`` and an idempotent ``run`` answer with a record's
*digest*, command count and per-command statuses rather than its command text - a credential on a command
line is routine, and those fields already answer "is this the payload I sent" and "how did each part end".
Output bytes are not treated that way: ``attach`` serves them raw and offset-exact, because byte fidelity on
the streams is what the protocol promises. And ``health`` reports the sha256 of the agent file that is
running, so a bootstrap deciding whether to adopt a daemon it found already listening can prove it is the
code it just uploaded instead of trusting a version string.

Failure policy
--------------
Anything we cannot determine is reported as an error with a stable code, or as ``outcome_uncertain`` with a
``reason``. The daemon never guesses success: no ``rc`` plus a vanished process is uncertainty, a payload
that outlived its named session is uncertainty, and a partially written session payload is uncertainty.
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
import re
import secrets
import select
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------------------------
# Protocol (mirror of inspire/platform/execd/protocol.py - keep in lockstep, test-enforced)
# ---------------------------------------------------------------------------------------------

PROTOCOL_VERSION = 2
AGENT_VERSION = "2.1.0"
EXEC_ID_RE = re.compile(r"^ex-[0-9a-f]{32}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "outcome_uncertain"})
STATES = frozenset({"accepted", "running", *TERMINAL_STATES})

#: Maximum accepted request line. A pod account is shared, and an unbounded read on a Unix socket
#: is an invitation to be swapped to death.
MAX_REQUEST_BYTES = 8 * 1024 * 1024
#: Largest answer a client will buffer: two base64 pages at the ceiling plus headers.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
#: Default and ceiling for one attach page, per stream.
DEFAULT_MAX_PAGE_BYTES = 256 * 1024
MAX_PAGE_BYTES_CEILING = 8 * 1024 * 1024
#: Refuse a new run when the state filesystem has less than this much room or so few inodes that
#: creating a record directory would be the thing that fills it up.
DEFAULT_MIN_FREE_BYTES = 64 * 1024 * 1024
DEFAULT_MIN_FREE_INODES = 512
#: Longest ``attach`` long-poll a client may ask for.
MAX_WAIT_SECONDS = 60.0
#: Largest payload we will stage. Batches are shell text, not file transfer.
MAX_PAYLOAD_BYTES = 1024 * 1024

METHODS = frozenset({"health", "run", "attach", "status", "list", "cancel", "cleanup"})


class ExecdError(RuntimeError):
    """An execd operation failed with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, data: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


def response_ok(data: dict) -> dict:
    return {"protocol": PROTOCOL_VERSION, "ok": True, "data": data}


def response_error(exc: ExecdError) -> dict:
    return {
        "protocol": PROTOCOL_VERSION,
        "ok": False,
        "error": {"code": exc.code, "message": str(exc), "data": exc.data},
    }


def new_exec_id() -> str:
    """A fresh execution id. Ids are the caller's, so a retry can reuse one and stay idempotent."""
    return f"ex-{secrets.token_hex(16)}"


def _preview(value: object, limit: int = 80) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------------------------
# Exit-code framing (mirror of inspire/platform/exec_frame.py - keep in lockstep, test-enforced)
# ---------------------------------------------------------------------------------------------

#: A stage killed by SIGPIPE means "the reader had enough", not "the command failed".
SIGPIPE_STATUS = 141
STATUS_VAR = "__inspire_status"
STAGES_VAR = "__inspire_stages"
STAGE_VAR = "__inspire_stage"
STAGE_N_VAR = "__inspire_stage_n"

#: Batch bookkeeping. ``BATCH_RC_VAR`` keeps the last non-zero per-command status, ``LAST_RC_VAR`` the
#: status of the final command, so an early failure cannot be masked by a trailing ``echo``.
BATCH_RC_VAR = "__inspire_batch_rc"
LAST_RC_VAR = "__inspire_last_rc"
FINAL_RC_VAR = "__inspire_final_rc"


def frame_exit_code(command: str, *, result_var: str = STATUS_VAR) -> str:
    """Return shell text that runs `command` and leaves its exit code in `$<result_var>`.

    POSIX-safe: it runs unchanged under bash, where a pipeline's per-stage list is read from
    ``PIPESTATUS``, and under a shell without ``PIPESTATUS``, where it degrades to the plain ``$?``.
    The list is consulted only when it holds two or more stages, because ``PIPESTATUS`` outlives the
    pipeline it describes - after a compound command it holds the last pipeline run inside it, which for
    a ``while``/``until``/``if`` is the test that ended the construct. Reading that one stale element made
    every looping script fail with the status of its own terminating test.
    No shell option is touched - in particular no ``pipefail``, which would turn ``cmd | head -1``
    into a 141 failure and is a hard error in ``sh``.
    """
    body = command.rstrip("\r\n")
    return "\n".join(
        [
            f"{result_var}=",
            body,
            (
                f'case "${{BASH_VERSION+set}}" in set) {STAGES_VAR}="${{PIPESTATUS[*]}}|$?";; '
                f'*) {STAGES_VAR}="|$?";; esac'
            ),
            f"{result_var}=${{{STAGES_VAR}##*|}}",
            f"{STAGE_N_VAR}=0; for {STAGE_VAR} in ${{{STAGES_VAR}%|*}}; do {STAGE_N_VAR}=$(({STAGE_N_VAR}+1)); done",
            (
                f'if [ "${result_var}" = 0 ] && [ "${{{STAGE_N_VAR}}}" -gt 1 ]; then '
                f"for {STAGE_VAR} in ${{{STAGES_VAR}%|*}}; do "
                f'case "${STAGE_VAR}" in 0|{SIGPIPE_STATUS}) ;; *) {result_var}=${STAGE_VAR}; break;; '
                "esac; done; fi"
            ),
            f"case ${result_var} in ''|*[!0-9]*) {result_var}=${{{STAGES_VAR}##*|}} ;; esac",
        ]
    )


# ---------------------------------------------------------------------------------------------
# Time, JSON and the private-by-default file helpers
# ---------------------------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_from_mtime(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return _now_iso()
    return (
        datetime.fromtimestamp(stamp, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return f"nodename-{socket.gethostname()}"


def _fsync_dir(path: Path) -> None:
    """Persist a directory entry. Without this an ``os.replace`` can survive a crash as *nothing*."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{secrets.token_hex(6)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _atomic_write_json(path: Path, payload: dict) -> None:
    _atomic_write_bytes(path, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))


def _read_json(path: Path) -> dict | None:
    """Parse a record file, tolerating a file that is absent or was never finished.

    A half-written JSON object is *not* treated as an empty record: it raises, and the caller decides
    what to say about a corrupt record. Silently reading it as `{}` would turn "we lost the payload"
    into "the payload was empty", which is a different - and wrong - answer.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ExecdError("record-unreadable", f"cannot read {path.name}: {exc}") from exc
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExecdError("record-corrupt", f"{path.name} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ExecdError("record-corrupt", f"{path.name} is not a JSON object")
    return parsed


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ExecdError("record-unreadable", f"cannot read {path.name}: {exc}") from exc


def _read_rc(path: Path) -> int | None:
    text = _read_text(path)
    if text is None:
        return None
    text = text.strip()
    if not re.fullmatch(r"-?\d{1,5}", text):
        raise ExecdError("record-corrupt", f"{path.name} is not an exit code: {_preview(text)!r}")
    return int(text)


def _touch_stream(path: Path) -> None:
    """Create an empty 0600 stream file so an attach before the first byte is an empty page, not an error."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)


def _ensure_private_dir(path: Path) -> None:
    existed = path.is_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        # mkdir's mode is masked by the umask and applies only to the leaf, so set it explicitly - and set it
        # even when the directory already existed, because an earlier version or a hand-made state directory
        # may well be 0755, which on a shared pod means another team can read our command text.
        os.chmod(path, 0o700)
    except OSError:
        if not existed:
            raise


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _sha256_payload(
    commands: list[str], session: str | None, session_bootstrap: str | None = None
) -> str:
    canonical = json.dumps(
        {
            "protocol": PROTOCOL_VERSION,
            "commands": commands,
            "session": session,
            "session_bootstrap": session_bootstrap,
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str | None:
    """sha256 of a file's bytes, or ``None`` if it cannot be read. ``None`` is never a guess."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


_AGENT_SHA256: str | None = None
_AGENT_SHA256_READ = False


def agent_sha256() -> str | None:
    """The hash of *this* agent file, as it sits on disk right now.

    A bootstrap that is deciding whether an already-running daemon is safe to adopt needs proof that the
    code behind the socket is the code it just uploaded, and ``agent_version`` cannot say that - two
    different files carry the same version string the moment someone forgets to bump it. ``health`` reports
    this so the bootstrap can refuse a mismatch instead of adopting an unknown daemon. It is read once and
    cached: the file does not change underneath a running interpreter, and re-hashing on every ``health``
    call would turn a status poll into a file read.
    """
    global _AGENT_SHA256, _AGENT_SHA256_READ
    if not _AGENT_SHA256_READ:
        _AGENT_SHA256 = file_sha256(Path(__file__).resolve())
        _AGENT_SHA256_READ = True
    return _AGENT_SHA256


# ---------------------------------------------------------------------------------------------
# State directory layout
# ---------------------------------------------------------------------------------------------

SOCKET_NAME = "daemon.sock"
TOKEN_NAME = "daemon.token"
LOCK_NAME = "daemon.lock"
PID_NAME = "daemon.pid"
RUNS_DIR_NAME = "runs"
SESSIONS_DIR_NAME = "sessions"

META_NAME = "meta.json"
PAYLOAD_NAME = "payload.sh"
STDOUT_NAME = "stdout.log"
STDERR_NAME = "stderr.log"
RC_NAME = "rc"
PIDINFO_NAME = "pid.json"
CANCEL_NAME = "cancel"
COMMANDS_RC_NAME = "commands.rc"
RECORDER_LOG_NAME = "recorder.log"
SPAWN_ERROR_NAME = "spawn_error"
FIFO_NAME = "in.fifo"
#: Written into a session directory when a payload reached its shell *partially*. The shell's input
#: stream now holds half a command, which no later submission can undo, so the session refuses new work
#: until its shell is replaced.
POISON_NAME = "POISONED"

#: The payload's environment carries its own exec_id, and that is how a cancel in a shared session knows
#: which processes belong to the run being cancelled. See :meth:`Daemon._cancel_targets`.
EXEC_ID_ENV = "INSPIRE_EXECD_EXEC_ID"
SESSION_ENV = "INSPIRE_EXECD_SESSION"

#: Exact file names a record may own. ``cleanup`` walks this list and nothing else - no glob, no walk,
#: no ``rm -rf`` - because a stray recursive delete on a shared pod is indistinguishable from vandalism.
RUN_FILE_NAMES = (
    META_NAME,
    PAYLOAD_NAME,
    STDOUT_NAME,
    STDERR_NAME,
    RC_NAME,
    PIDINFO_NAME,
    CANCEL_NAME,
    COMMANDS_RC_NAME,
    RECORDER_LOG_NAME,
    SPAWN_ERROR_NAME,
)


def socket_path_for(state_dir: Path) -> Path:
    return state_dir / SOCKET_NAME


def token_path_for(state_dir: Path) -> Path:
    return state_dir / TOKEN_NAME


def read_token(state_dir: Path) -> str:
    path = token_path_for(state_dir)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ExecdError("no-token", f"no daemon token at {path}; is the daemon started?") from exc
    except OSError as exc:
        raise ExecdError("no-token", f"cannot read daemon token: {exc}") from exc
    if not token:
        raise ExecdError("no-token", f"daemon token at {path} is empty")
    return token


def _validate_exec_id(value: object) -> str:
    text = str(value if value is not None else "")
    if not EXEC_ID_RE.fullmatch(text):
        raise ExecdError(
            "invalid-exec-id",
            "exec_id must have the form ex-<32 lowercase hex characters>",
            data={"exec_id": _preview(text)},
        )
    return text


def _validate_session(value: object) -> str:
    text = str(value if value is not None else "")
    if not SESSION_RE.fullmatch(text):
        raise ExecdError(
            "invalid-session",
            "session must be 1-64 letters, digits, dots, underscores or dashes, starting with a letter "
            "or digit",
            data={"session": _preview(text)},
        )
    return text


def _run_dir(state_dir: Path, exec_id: str) -> Path:
    """The record directory for a validated id. Validation here is the path-traversal guard."""
    return state_dir / RUNS_DIR_NAME / _validate_exec_id(exec_id)


def _session_dir(state_dir: Path, session: str) -> Path:
    return state_dir / SESSIONS_DIR_NAME / _validate_session(session)


def _contained(path: Path, root: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
    except (ValueError, OSError):
        return False
    return True


# ---------------------------------------------------------------------------------------------
# Process identity: the guards that keep a cancel from killing somebody else's job
# ---------------------------------------------------------------------------------------------


def _proc_stat(pid: int) -> tuple[str, dict] | tuple[None, None]:
    """Return ``(state, fields)`` from ``/proc/<pid>/stat``.

    ``comm`` is user-controlled and may contain spaces and parentheses, so the field list is split
    *after* the last ``)`` and the process state is the first byte there.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return None, None
    close = raw.rfind(")")
    if close < 0:
        return None, None
    tail = raw[close + 1 :].split()
    if not tail:
        return None, None
    return tail[0], {
        "pid": pid,
        "comm": raw[raw.find("(") + 1 : close],
        "state": tail[0],
        "ppid": int(tail[1]) if len(tail) > 1 and tail[1].isdigit() else None,
        "pgid": int(tail[2]) if len(tail) > 2 and tail[2].isdigit() else None,
        "starttime": tail[19] if len(tail) > 19 else None,
    }


def _proc_uid(pid: int) -> int | None:
    try:
        for line in (
            Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace").splitlines()
        ):
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None
    return None


def _pid_alive_unchecked(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def inspect_pid(pid: int, *, expect_starttime: str | None = None) -> dict:
    """Classify one pid as ours-or-not.

    Three confusions are refused here, all of them real: a recycled pid that now belongs to a different
    process (``starttime`` differs), a process owned by somebody else on a shared pod (uid differs), and a
    zombie, which is a corpse in the table rather than a running process.
    """
    if pid <= 0:
        return {"alive": False, "reason": "invalid-pid", "pid": pid}
    state, fields = _proc_stat(pid)
    if not _pid_alive_unchecked(pid):
        return {"alive": False, "reason": "not-found", "pid": pid}
    if fields is None:  # /proc missing (non-Linux, or a hidepid mount): identity cannot be checked
        return {"alive": False, "unknown": True, "reason": "proc-unavailable", "pid": pid}
    uid = _proc_uid(pid)
    if uid is not None and uid != os.getuid():
        return {"alive": False, "reason": "not-owned", "pid": pid, "uid": uid}
    if expect_starttime is not None and fields.get("starttime") != expect_starttime:
        return {
            "alive": False,
            "reason": "pid-recycled",
            "pid": pid,
            "starttime": fields.get("starttime"),
        }
    if state == "Z":
        return {"alive": False, "reason": "zombie", "pid": pid, "pgid": fields.get("pgid")}
    return {
        "alive": True,
        "reason": "ok",
        "pid": pid,
        "pgid": fields.get("pgid"),
        "ppid": fields.get("ppid"),
        "starttime": fields.get("starttime"),
        "comm": fields.get("comm"),
        "uid": uid,
    }


def _proc_available() -> bool:
    return Path("/proc/self/stat").exists()


def _read_pidinfo(path: Path) -> dict | None:
    info = _read_json(path)
    if info is None:
        return None
    try:
        info["pid"] = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return info


def _proc_environment(pid: int, name: str) -> tuple[bool, str | None]:
    """Look one variable up in ``/proc/<pid>/environ``.

    The answer is two-valued on purpose: ``(False, None)`` means we could not read it - the process died
    under us, or it belongs to another uid - which is *not* the same fact as ``(True, None)``, "read it and
    the variable is absent". Attribution decisions must be able to tell "this process is not mine" from "I
    cannot tell", because only the first one justifies declining to signal.
    """
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError, IsADirectoryError, OSError):
        return False, None
    wanted = f"{name}=".encode("utf-8")
    for entry in raw.split(b"\0"):
        if entry.startswith(wanted):
            return True, entry[len(wanted) :].decode("utf-8", "replace")
    return True, None


def _child_map() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    if not _proc_available():
        return children
    try:
        pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return children
    for pid in pids:
        _state, fields = _proc_stat(pid)
        if fields and fields.get("ppid"):
            children.setdefault(int(fields["ppid"]), []).append(pid)
    return children


def _descendants(root_pid: int, *, limit: int = 256) -> list[int]:
    """Every descendant of ``root_pid`` in parent-first order.

    A run inside a named session shares the session shell's process group by design (that is what keeps
    ``cd`` and ``export`` alive), so a cancel there cannot use ``killpg`` - it walks the tree instead.
    """
    children = _child_map()
    found: list[int] = []
    queue = list(children.get(root_pid, []))
    seen = {root_pid}
    while queue and len(found) < limit:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        found.append(pid)
        queue.extend(children.get(pid, []))
    return found


def _signal_target(pid: int, *, group: bool) -> tuple[str, int]:
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return ("missing", pid)
    if group and pgid == pid:
        return ("killpg", pgid)
    return ("kill", pid)


def _send_signal(kind: str, target: int, sig: int) -> bool:
    try:
        if kind == "killpg":
            os.killpg(target, sig)
        else:
            os.kill(target, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------------------------
# Authoritative record resolution
# ---------------------------------------------------------------------------------------------


def _stream_paths(state_dir: Path, meta: dict) -> tuple[Path, Path]:
    """Where a record's two streams live: its own directory, or its named session's."""
    session = meta.get("session")
    if session:
        base = _session_dir(state_dir, session)
        return base / STDOUT_NAME, base / STDERR_NAME
    return _run_dir(state_dir, str(meta.get("exec_id"))) / STDOUT_NAME, (
        _run_dir(state_dir, str(meta.get("exec_id"))) / STDERR_NAME
    )


def resolve_record(state_dir: Path, exec_id: str) -> dict:
    """The single authoritative answer for one execution.

    Precedence is deliberate. An ``rc`` file is ground truth - the wrapper wrote it after the payload was
    gone - so it wins even over a state the daemon pinned earlier (a recorder can outlive the daemon and
    legitimately produce an ``rc`` after a restart). Failing that we honour a pinned terminal state, which
    is how a recovery decision survives the next scan. Only then do we derive from process liveness.
    """
    run_dir = _run_dir(state_dir, exec_id)
    meta = _read_json(run_dir / META_NAME)
    if meta is None:
        raise ExecdError(
            "unknown-exec", f"no execd record for {exec_id}", data={"exec_id": exec_id}
        )

    session = meta.get("session")
    rc_path = run_dir / RC_NAME
    cancel_path = run_dir / CANCEL_NAME
    rc_value = _read_rc(rc_path)
    cancel_requested = cancel_path.exists() or bool(meta.get("cancel_requested_at"))
    spawn_error = _read_text(run_dir / SPAWN_ERROR_NAME)

    record: dict = {
        "exec_id": str(meta.get("exec_id") or exec_id),
        "session": session,
        "commands": list(meta.get("commands") or []),
        "created_at": meta.get("created_at"),
        "session_created_at": meta.get("session_created_at"),
        "payload_sha256": meta.get("payload_sha256"),
        "exit_code": None,
        "reason": meta.get("reason"),
        "cancel_requested": cancel_requested,
        "cancel_requested_at": meta.get("cancel_requested_at"),
        "recovered": bool(meta.get("recovered")),
        "session_restarted": bool(meta.get("session_restarted")),
        "recorder_pid": meta.get("recorder_pid"),
        "recorder_alive": False,
        "payload_pid": None,
        "session_pid": meta.get("session_pid"),
        "session_generation": meta.get("session_generation"),
        "command_statuses": _command_statuses(run_dir / COMMANDS_RC_NAME),
        "spawn_error": spawn_error,
    }
    # Reported even once an rc exists: "which pid ran this" is part of the record, and dropping it as soon as
    # the run finished would take away the one thing an operator asks for after a suspicious exit code.
    payload_info = _read_pidinfo(run_dir / PIDINFO_NAME)
    if payload_info is not None:
        record["payload_pid"] = payload_info.get("pid")
        record["payload_pgid"] = payload_info.get("pgid")

    rc_mtime = _mtime(rc_path)
    if rc_value is not None:
        # ``cancel`` is only ever written *after* a signal was actually delivered (see `_rpc_cancel`), so its
        # existence already means "we signalled this run". Ordering by mtime is then what separates a real
        # cancellation from a cancel that arrived too late: the wrapper writes `rc` only after the payload is
        # gone, so an rc older than the marker means the run had already finished on its own. A cancel that
        # reached nothing leaves no marker, and its record keeps the exit code the payload really produced.
        cancel_mtime = _mtime(cancel_path)
        cancelled = rc_mtime >= cancel_mtime > 0
        record["state"] = "cancelled" if cancelled else ("succeeded" if rc_value == 0 else "failed")
        record["exit_code"] = rc_value
        record["reason"] = "terminated" if cancelled else None
        record["finished_at"] = _iso_from_mtime(rc_path)
        pinned = meta.get("terminal_state")
        if pinned in TERMINAL_STATES and pinned != record["state"]:
            record["rc_overrode_pinned_state"] = pinned
    else:
        pinned = meta.get("terminal_state")
        if pinned in TERMINAL_STATES:
            record["state"] = pinned
            record["exit_code"] = meta.get("exit_code")
            record["finished_at"] = meta.get("finished_at")
            if record["reason"] is None:
                record["reason"] = "pinned"
        elif session:
            _resolve_session_run(state_dir, meta, record)
        else:
            _resolve_direct_run(state_dir, meta, record)

    stdout_path, stderr_path = _stream_paths(state_dir, meta)
    window = _stream_window(state_dir, meta, record)
    record["stdout_path"] = str(stdout_path)
    record["stderr_path"] = str(stderr_path)
    record["stdout_start"] = int(meta.get("stdout_start") or 0)
    record["stderr_start"] = int(meta.get("stderr_start") or 0)
    record["stdout_end"] = window[0]
    record["stderr_end"] = window[1]
    record["stdout_bytes"] = max(0, (window[0] or _file_size(stdout_path)) - record["stdout_start"])
    record["stderr_bytes"] = max(0, (window[1] or _file_size(stderr_path)) - record["stderr_start"])
    record["terminal"] = record["state"] in TERMINAL_STATES
    if record["state"] == "outcome_uncertain":
        record["uncertain"] = True
    return record


def _command_statuses(path: Path) -> list[int]:
    text = _read_text(path)
    if not text:
        return []
    statuses: list[int] = []
    for line in text.splitlines():
        line = line.strip()
        if re.fullmatch(r"-?\d{1,5}", line):
            statuses.append(int(line))
    return statuses


def _recorder_identity(meta: dict) -> dict | None:
    """What we may claim about the recorder process this daemon forked for the record.

    ``None`` means no recorder was ever recorded - the daemon died before it got that far. A recorded pid is
    only believed when its ``starttime`` still matches, because a recycled pid would otherwise let a record
    look live forever. A record written by an older agent has no stored ``starttime``; that record gets the
    weaker (liveness-only) check, which is what it was judged by in the first place.
    """
    pid = meta.get("recorder_pid")
    if not pid:
        return None
    try:
        return inspect_pid(int(pid), expect_starttime=meta.get("recorder_starttime"))
    except (TypeError, ValueError):
        return {"alive": False, "reason": "recorder-pid-unreadable"}


def _resolve_direct_run(state_dir: Path, meta: dict, record: dict) -> None:
    run_dir = _run_dir(state_dir, str(meta.get("exec_id") or record["exec_id"]))
    recorder = _recorder_identity(meta)
    # "Unknown" counts as alive here: if /proc cannot answer, we may declare a live process dead, so the
    # conservative reading is that the holder is still there and still able to write an `rc`.
    holder_alive = bool(recorder and (recorder.get("alive") or recorder.get("unknown")))
    record["recorder_alive"] = holder_alive
    info = _read_pidinfo(run_dir / PIDINFO_NAME)
    if info is None:
        if holder_alive:
            # The recorder is alive and had simply not recorded its payload's pid yet: the daemon died in the
            # window between forking the recorder and the recorder writing `pid.json`. The execution is
            # *live*, and a scan that pinned it here would report uncertainty about a process that is running
            # right now - and then have to explain away the `rc` that turns up moments later.
            record["state"] = "accepted"
            record["reason"] = "waiting-for-recorder"
            return
        if recorder is not None:
            # A recorder existed and is now gone without ever naming a payload process. It may have forked the
            # payload and died in between, so the side effect may or may not have happened: uncertainty.
            record["state"] = "outcome_uncertain"
            record["reason"] = "recorder-gone-before-pid"
            record["finished_at"] = _iso_from_mtime(run_dir / META_NAME)
            return
        # No pid and no recorder: the daemon died between the metadata write and the fork. Both stay
        # `accepted` until a scan - which knows the daemon restarted - pins it.
        record["state"] = "accepted"
        record["reason"] = "waiting-for-recorder"
        return
    record["payload_pid"] = info.get("pid")
    check = inspect_pid(int(info.get("pid") or 0), expect_starttime=info.get("starttime"))
    if check.get("unknown"):
        record["state"] = "running"
        record["reason"] = "proc-unavailable"
        record["process_check"] = check.get("reason")
        return
    if check.get("alive"):
        record["state"] = "running"
        record["reason"] = None
        return
    if holder_alive:
        # The payload is not running but its holder is, and that holder is the only writer of `rc`: a killed
        # payload is a zombie for as long as it takes the recorder to reap it. Answering "running, awaiting
        # the holder's rc" keeps the record honest without waiting for the next status call to change its
        # mind, and it survives a daemon restart because the identity check is on disk.
        record["state"] = "running"
        record["reason"] = "payload-gone-awaiting-rc"
        record["process_check"] = check.get("reason")
        return
    record["state"] = "outcome_uncertain"
    record["reason"] = check.get("reason") or "no-rc-process-gone"
    record["finished_at"] = _iso_from_mtime(run_dir / PIDINFO_NAME)


def _resolve_session_run(state_dir: Path, meta: dict, record: dict) -> None:
    session = str(meta.get("session"))
    sdir = _session_dir(state_dir, session)
    info = _read_pidinfo(sdir / PIDINFO_NAME)
    if info is None:
        record["state"] = "outcome_uncertain"
        record["reason"] = "session-missing"
        return
    record["session_pid"] = info.get("pid")
    check = inspect_pid(int(info.get("pid") or 0), expect_starttime=info.get("starttime"))
    record["process_check"] = check.get("reason")
    if check.get("alive"):
        record["state"] = "running"
        record["reason"] = None
        return
    # The shell that was running this payload is gone and never wrote an rc. Whatever it had buffered died
    # with it, so we cannot claim the payload ran and must not claim it did not: uncertainty, explicitly.
    record["state"] = "outcome_uncertain"
    if check.get("reason") in ("not-found", "zombie") or int(info.get("generation") or 0) != int(
        meta.get("session_generation") or 0
    ):
        # A zombie is the same fact with a different face: the shell is not executing anything any more.
        record["reason"] = "session-lost"
    else:
        record["reason"] = check.get("reason") or "no-rc-process-gone"
    record["finished_at"] = _iso_from_mtime(sdir / PIDINFO_NAME)


def _writer_is_live(check: dict) -> bool:
    """A process we cannot rule out. "Unknown" counts as live: declaring a live holder dead is the worse error."""
    return bool(check.get("alive") or check.get("unknown"))


def _live_writers(state_dir: Path, exec_id: str) -> list[dict]:
    """The processes that could still write into this record's directory.

    ``rc`` is the last thing a holder writes, so a record that already has one is done with by definition.
    Without an ``rc``, a holder may still be alive - the window this exists for is a record some earlier
    daemon pinned as terminal while its recorder, or the session shell that was told to run it, is in fact
    still running. Deleting the directory underneath such a process loses its work silently: the payload
    keeps appending to an unlinked inode and the record that would have explained the run is gone. Refusing
    costs the caller one retry; the alternative is a hole in the durability promise.
    """
    run_dir = _run_dir(state_dir, exec_id)
    if (run_dir / RC_NAME).exists():
        return []
    meta = _read_json(run_dir / META_NAME) or {}
    writers: list[dict] = []
    recorder = _recorder_identity(meta)
    if recorder is not None and _writer_is_live(recorder):
        writers.append(
            {"role": "recorder", "pid": meta.get("recorder_pid"), "reason": recorder.get("reason")}
        )
    info = _read_pidinfo(run_dir / PIDINFO_NAME)
    if info is not None:
        check = inspect_pid(int(info.get("pid") or 0), expect_starttime=info.get("starttime"))
        if _writer_is_live(check):
            writers.append(
                {"role": "payload", "pid": info.get("pid"), "reason": check.get("reason")}
            )
    session = meta.get("session")
    if session:
        session_info = _read_pidinfo(_session_dir(state_dir, str(session)) / PIDINFO_NAME)
        # Only the shell that was *told* to run this record can still write it. A replacement shell (a later
        # generation) never saw the payload, so it must not hold the record's cleanup hostage forever.
        if session_info is not None and int(session_info.get("generation") or 0) == int(
            meta.get("session_generation") or 0
        ):
            check = inspect_pid(
                int(session_info.get("pid") or 0), expect_starttime=session_info.get("starttime")
            )
            if _writer_is_live(check):
                writers.append(
                    {
                        "role": "session-shell",
                        "pid": session_info.get("pid"),
                        "reason": check.get("reason"),
                    }
                )
    return writers


def _stream_window(state_dir: Path, meta: dict, record: dict) -> tuple[int | None, int | None]:
    """The readable byte window's end for a session-backed run, or ``None`` for "grows with the file".

    A named session's log is shared by every run in it, so a run's slice has to be closed when the run
    ends; otherwise a later run's output would be handed out as this run's tail. Closing needs the file
    size at the moment the rc appeared, which is what ``pin_streams`` records.
    """
    if not meta.get("session"):
        return None, None
    if record["state"] in TERMINAL_STATES:
        end_out = meta.get("stdout_end")
        end_err = meta.get("stderr_end")
        stdout_path, stderr_path = _stream_paths(state_dir, meta)
        if end_out is None:
            end_out = _file_size(stdout_path)
        if end_err is None:
            end_err = _file_size(stderr_path)
        return int(end_out), int(end_err)
    return None, None


def pin_terminal_record(state_dir: Path, exec_id: str) -> dict:
    """Freeze a derived terminal state into ``meta.json`` so the next answer cannot drift.

    Pinning is not just a cache. Once a process is gone, a recycled pid could later make a naive liveness
    check say "running" again; writing the decision down closes that door. ``outcome_uncertain`` is pinned
    for exactly this reason, and an ``rc`` that turns up afterwards still outranks it.
    """
    run_dir = _run_dir(state_dir, exec_id)
    record = resolve_record(state_dir, exec_id)
    meta = _read_json(run_dir / META_NAME) or {}
    if record["state"] not in TERMINAL_STATES:
        return record
    stdout_path, stderr_path = _stream_paths(state_dir, meta) if meta else (run_dir, run_dir)
    updates: dict = {}
    for key, value in (
        ("terminal_state", record["state"]),
        ("exit_code", record["exit_code"]),
        ("reason", record.get("reason")),
        ("finished_at", record.get("finished_at") or _now_iso()),
    ):
        if meta.get(key) != value:
            updates[key] = value
    if meta.get("session"):
        # Freeze the slice only the first time. A background process the run left behind keeps appending to
        # the shared session log, and re-measuring on every status call would hand those later bytes out as
        # this run's tail - and turn a read-only status call into a write storm.
        if meta.get("stdout_end") is None:
            updates["stdout_end"] = _file_size(stdout_path)
        if meta.get("stderr_end") is None:
            updates["stderr_end"] = _file_size(stderr_path)
    changed = bool(updates)
    if changed:
        updates["pinned_at"] = _now_iso()
        meta.update(updates)
        _atomic_write_json(run_dir / META_NAME, meta)
    return resolve_record(state_dir, exec_id)


def scan_state_dir(state_dir: Path) -> dict:
    """Classify every record after a daemon start. Recovery here means *reading*, never re-running.

    A daemon can die at any point, including between "metadata is durable" and "recorder is forked", and
    between "recorder is forked" and "the recorder wrote its payload's pid". The scan's job is to turn each
    non-terminal record into an answer the new daemon can defend: adopted when a process is demonstrably
    still ours - which includes a *recorder* that is alive and has not named its payload yet, since that is
    a live execution rather than a lost one - and otherwise pinned as ``outcome_uncertain``. Nothing is ever
    relaunched, and a pin is a decision on paper, not a kill: if the holder really is alive, its ``rc``
    outranks the pin the moment it lands.
    """
    report: dict[str, Any] = {
        "records": 0,
        "adopted": 0,
        "pinned": 0,
        "terminal": 0,
        "problems": [],
    }
    runs = state_dir / RUNS_DIR_NAME
    try:
        entries = sorted(runs.iterdir()) if runs.is_dir() else []
    except OSError as exc:
        report["problems"].append({"path": str(runs), "problem": str(exc)})
        entries = []
    for entry in entries:
        if not entry.is_dir():
            continue
        exec_id = entry.name
        report["records"] += 1
        try:
            if not EXEC_ID_RE.fullmatch(exec_id):
                report["problems"].append(
                    {"exec_id": exec_id, "problem": "unexpected-directory-name"}
                )
                continue
            record = resolve_record(state_dir, exec_id)
        except ExecdError as exc:
            report["problems"].append({"exec_id": exec_id, "problem": exc.code, "detail": str(exc)})
            continue
        if record["terminal"]:
            before = record["state"]
            record = pin_terminal_record(state_dir, exec_id)
            report["terminal"] += 1
            if record.get("recovered") or record["state"] != before:
                report["pinned"] += 1
        elif record["state"] == "running":
            report["adopted"] += 1
        elif record.get("recorder_alive"):
            # The recorder is alive and has not named its payload yet: the daemon died in the window between
            # forking the recorder and the recorder writing `pid.json`. This run is *live*, so it is adopted
            # like any other and its record is left alone - the holder will write `rc` when it finishes, and
            # a pin here would have to be un-explained later.
            report["adopted"] += 1
            report.setdefault("adopted_waiting_for_pid", []).append(exec_id)
        else:  # `accepted` with no recorder to adopt: never relaunched, always uncertain
            meta = _read_json(entry / META_NAME) or {}
            meta.update(
                {
                    "terminal_state": "outcome_uncertain",
                    "exit_code": None,
                    "reason": (
                        "recorder-gone-before-pid"
                        if meta.get("recorder_pid")
                        else "no-rc-process-gone"
                    ),
                    "finished_at": _now_iso(),
                    "recovered": True,
                    "recovered_at": _now_iso(),
                }
            )
            _atomic_write_json(entry / META_NAME, meta)
            report["pinned"] += 1
    for session_dir in (
        sorted((state_dir / SESSIONS_DIR_NAME).iterdir())
        if (state_dir / SESSIONS_DIR_NAME).is_dir()
        else []
    ):
        if not session_dir.is_dir():
            continue
        info = _read_pidinfo(session_dir / PIDINFO_NAME)
        if info is None:
            continue
        check = inspect_pid(int(info.get("pid") or 0), expect_starttime=info.get("starttime"))
        if not check.get("alive"):
            (session_dir / "DEAD").write_text(_now_iso() + "\n", encoding="utf-8")
            report.setdefault("dead_sessions", []).append(session_dir.name)
    return report


# ---------------------------------------------------------------------------------------------
# Payload scripts
# ---------------------------------------------------------------------------------------------


def build_command_frames(commands: list[str], *, status_log: Path | None) -> list[str]:
    """One exit-code frame per command, plus the batch accumulation.

    Every command runs in the *same* shell, in order, with no subshell and no ``set -e``: ``cd`` and
    ``export`` in one command are still in force in the next, and a failing command does not abort the
    batch. Each command's own status is appended to ``status_log`` so the per-command truth survives the
    batch's single exit code.
    """
    lines: list[str] = []
    for command in commands:
        lines.append(frame_exit_code(command))
        lines.append(f'{LAST_RC_VAR}="${{{STATUS_VAR}}}"')
        lines.append(
            f'if [ "${{{STATUS_VAR}}}" != 0 ]; then {BATCH_RC_VAR}="${{{STATUS_VAR}}}"; fi'
        )
        if status_log is not None:
            lines.append(f"printf '%s\\n' \"${{{STATUS_VAR}}}\" >> {shlex.quote(str(status_log))}")
    return lines


def build_final_rc_block() -> list[str]:
    return [
        f'if [ "${{{BATCH_RC_VAR}}}" != 0 ]; then {FINAL_RC_VAR}="${{{BATCH_RC_VAR}}}"; '
        f'else {FINAL_RC_VAR}="${{{LAST_RC_VAR}}}"; fi'
    ]


def build_run_script(
    commands: list[str], *, status_log: Path | None, payload_umask: str | None = None
) -> str:
    """Script for the recorder's payload: run the batch, then exit with its status.

    The ``umask`` line hands the payload the umask the daemon was *launched* with. Our bookkeeping needs
    077/0600 and the recorder inherits that from the daemon, but a training run must not start writing its
    checkpoints 0600 because our record files are private - which is the same split the pre-daemon recorder
    made by handing the caller's umask back to the payload.

    A payload that calls ``exit`` leaves through the middle of this script, which is exactly what the
    recorder's ``rc`` then reports - the alternative (a subshell) would eat the caller's ``cd``/``export``.
    """
    lines: list[str] = []
    if payload_umask and re.fullmatch(r"0?[0-7]{3,4}", payload_umask):
        lines.append(f"umask {payload_umask}")
    lines += [f"{BATCH_RC_VAR}=0", f"{LAST_RC_VAR}=0"]
    lines += build_command_frames(commands, status_log=status_log)
    lines += build_final_rc_block()
    lines.append(f'exit "${{{FINAL_RC_VAR}}}"')
    return "\n".join(lines) + "\n"


def build_session_script(
    commands: list[str],
    *,
    exec_id: str,
    rc_path: Path,
    status_log: Path | None,
    bootstrap: str | None = None,
) -> str:
    """The batch, staged as a session run's ``payload.sh``, to be *sourced* by the session shell.

    Two things happen here that the direct-run script does not need.

    The first line exports the run's own ``exec_id``. Every process the batch forks inherits it, and inside
    a shared session that is the only reliable way to tell this run's processes apart from the background
    jobs an *earlier* run left behind - see :meth:`Daemon._cancel_targets`. It is unset again once ``rc`` is
    written, so a later run can never inherit an attribution pointing at a finished record.

    The ``rc`` line is written by the session shell itself, because there is no recorder process to wait on
    inside a shared shell. A payload that calls ``exit`` takes the session down with it and the record comes
    back ``outcome_uncertain`` - the honest answer, since we cannot tell whether its side effects happened.
    """
    lines = [
        f"export {EXEC_ID_ENV}={shlex.quote(str(exec_id))}",
        f"{BATCH_RC_VAR}=0",
        f"{LAST_RC_VAR}=0",
    ]
    body = build_command_frames(commands, status_log=status_log) + build_final_rc_block()
    if bootstrap is not None:
        lines.append(f"if {{\n{bootstrap}\n}}; then")
        lines += body
        lines.append(f"else {FINAL_RC_VAR}=$?; fi")
    else:
        lines += body
    lines.append(f"printf '%s\\n' \"${{{FINAL_RC_VAR}}}\" > {shlex.quote(str(rc_path))}")
    lines.append(f"unset {EXEC_ID_ENV}")
    return "\n".join(lines) + "\n"


def build_session_source_line(payload_path: Path, *, rc_path: Path) -> str:
    """The *one* line a session's FIFO is given: source the staged batch, nothing else.

    The whole script used to go down the pipe itself, which meant a write that stopped half way left the
    shell's input stream holding half a command - and the *next* run's line would then be parsed as the
    tail of that half command, in the wrong record's name, poisoning a session the caller had no way to
    repair. A few dozen bytes either land or do not, so the corruption window collapses to "one unreadable
    line", and the session is poisoned in that case rather than quietly reused.

    A top-level ``return`` leaves the sourced script before its own ``rc`` and cleanup lines. Capture the
    source status immediately, record it only when the payload wrote no ``rc``, and always clear the exec
    attribution. An unreadable staged file still answers 127, matching a payload the recorder cannot spawn.
    """
    source_rc = "__inspire_source_rc"
    quoted_payload = shlex.quote(str(payload_path))
    quoted_rc = shlex.quote(str(rc_path))
    line = (
        f". {quoted_payload}; {source_rc}=$?; "
        f"if [ ! -f {quoted_rc} ]; then "
        f"if [ ! -f {quoted_payload} ] || [ ! -r {quoted_payload} ]; then {source_rc}=127; fi; "
        f"printf '%s\\n' \"${source_rc}\" > {quoted_rc}; fi; "
        f"unset {EXEC_ID_ENV} {source_rc}\n"
    )
    if "\n" in line[:-1] or not line.endswith("\n"):
        raise ExecdError("invalid-params", "a session source line must be exactly one line")
    return line


def choose_shell() -> str:
    for candidate in ("/bin/bash", "/usr/bin/bash"):
        if os.access(candidate, os.X_OK):
            return candidate
    found = _which("bash")
    if found:
        return found
    for shell in ("/bin/sh", "/usr/bin/sh"):
        if os.access(shell, os.X_OK):
            return shell
    return _which("sh") or "sh"


def _which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if os.access(candidate, os.X_OK):
            return candidate
    return None


# ---------------------------------------------------------------------------------------------
# Disk headroom
# ---------------------------------------------------------------------------------------------


def disk_headroom(state_dir: Path) -> dict:
    try:
        info = os.statvfs(str(state_dir))
    except OSError as exc:
        return {"ok": False, "error": str(exc), "free_bytes": None, "free_inodes": None}
    return {
        "free_bytes": info.f_bavail * info.f_frsize,
        "free_inodes": info.f_ffree,
        "ok": True,
    }


def check_disk_space(state_dir: Path, *, min_free_bytes: int, min_free_inodes: int) -> dict:
    headroom = disk_headroom(state_dir)
    headroom["minimum_bytes"] = min_free_bytes
    headroom["minimum_inodes"] = min_free_inodes
    if not headroom.get("ok"):
        raise ExecdError(
            "disk-check-failed", f"cannot statvfs {state_dir}: {headroom.get('error')}"
        )
    if int(headroom["free_bytes"]) < min_free_bytes:
        raise ExecdError(
            "disk-space-low",
            "refusing a new execution: the state filesystem is below the free-space floor",
            data={"free_bytes": headroom["free_bytes"], "minimum_bytes": min_free_bytes},
        )
    if int(headroom["free_inodes"]) < min_free_inodes:
        raise ExecdError(
            "disk-inodes-low",
            "refusing a new execution: the state filesystem is below the free-inode floor",
            data={"free_inodes": headroom["free_inodes"], "minimum_inodes": min_free_inodes},
        )
    return headroom


# ---------------------------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------------------------


class Daemon:
    """Owner of one state directory: socket, token, records, and the authority over both."""

    def __init__(
        self,
        state_dir: Path,
        *,
        socket_path: Path | None = None,
        cancel_grace_seconds: float = 5.0,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        min_free_inodes: int = DEFAULT_MIN_FREE_INODES,
        max_page_bytes: int = DEFAULT_MAX_PAGE_BYTES,
        session_write_timeout: float = 15.0,
    ) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.socket_path = Path(socket_path) if socket_path else socket_path_for(self.state_dir)
        self.cancel_grace_seconds = float(cancel_grace_seconds)
        self.min_free_bytes = int(min_free_bytes)
        self.min_free_inodes = int(min_free_inodes)
        self.max_page_bytes = max(1, min(int(max_page_bytes), MAX_PAGE_BYTES_CEILING))
        self.session_write_timeout = float(session_write_timeout)
        # Serialises every decision that reads-then-writes a record, so two concurrent `run` calls with the
        # same exec_id cannot both conclude "new". Reads that only classify a record use the same lock:
        # pinning is a write, and the answers must not interleave with it.
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._lock_fd: int | None = None
        self._pid_fd: int | None = None
        self._threads: list[threading.Thread] = []
        self._children: list[subprocess.Popen] = []
        # Submitting to a named session can block for seconds on a shell that is not draining its FIFO, and
        # that wait must not sit on the global lock - otherwise one wedged session stops the daemon from
        # answering `health` about anything else, which is exactly when an operator needs `health`. One lock
        # per session keeps that session's own submissions ordered (and a session still runs one payload at a
        # time, because the fast phase under the global lock refuses a second live run in it) while every
        # other record stays readable.
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()
        self.nonce = secrets.token_hex(16)
        self.boot_id = _boot_id()
        self.started_at = time.time()
        self.started_at_iso = _now_iso()
        self.recovery: dict = {"scanned": False}
        self.payload_umask = "0077"
        self.token = ""

    # -- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        # Remember the umask the daemon was *launched* with and hand it to payloads, then keep the daemon at
        # 077 for its whole life. Our bookkeeping must be private, but a training run must not start writing
        # its checkpoints 0600 just because our record files are.
        self.payload_umask = f"{os.umask(0o077):04o}"
        _ensure_private_dir(self.state_dir)
        _ensure_private_dir(self.state_dir / RUNS_DIR_NAME)
        _ensure_private_dir(self.state_dir / SESSIONS_DIR_NAME)
        self._acquire_lock()
        self._write_token()
        self.recovery = dict(scan_state_dir(self.state_dir))
        self.recovery["scanned"] = True
        self._bind_socket()
        self._write_pidfile()

    def _acquire_lock(self) -> None:
        path = self.state_dir / LOCK_NAME
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # pragma: no cover - POSIX only, kept honest for odd builds
            os.close(fd)
            if (self.state_dir / SOCKET_NAME).exists() and _socket_is_live(self.socket_path):
                raise ExecdError(
                    "already-running", f"another execd daemon holds {self.state_dir}"
                ) from None
            return
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise ExecdError(
                    "already-running",
                    f"another execd daemon already holds {self.state_dir}",
                ) from exc
            raise ExecdError("lock-failed", f"cannot lock {path}: {exc}") from exc
        os.set_inheritable(fd, False)
        self._lock_fd = fd

    def _write_token(self) -> None:
        """Reuse a token across daemon restarts, generate one on first start.

        Reusing is deliberate: an in-flight `rpc` shim must keep working across an upgrade restart, and a
        rotating token would turn a routine daemon restart into an auth failure for every caller in the pod.
        """
        path = token_path_for(self.state_dir)
        if path.exists():
            self.token = read_token(self.state_dir)
            os.chmod(path, 0o600)
            return
        self.token = secrets.token_urlsafe(32)
        _atomic_write_bytes(path, (self.token + "\n").encode("utf-8"))
        os.chmod(path, 0o600)

    def _bind_socket(self) -> None:
        text = str(self.socket_path)
        if (
            len(text) > 100
        ):  # sockaddr_un is 108 bytes; leave room for the terminator and be explicit
            raise ExecdError(
                "socket-path-too-long",
                f"socket path must be 100 characters or fewer, got {len(text)}: {text}",
            )
        if _socket_is_live(self.socket_path):
            raise ExecdError("already-running", f"a daemon is already listening on {text}")
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ExecdError(
                "socket-unlink-failed", f"cannot remove stale socket {text}: {exc}"
            ) from exc
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        listener.listen(64)
        listener.settimeout(0.25)
        self._listener = listener

    def _write_pidfile(self) -> None:
        _atomic_write_bytes(
            self.state_dir / PID_NAME, (f"{os.getpid()}\n{self.started_at_iso}\n").encode("utf-8")
        )

    def serve_forever(self) -> None:
        if self._listener is None:
            self.start()
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                conn, _addr = self._listener.accept()
            except SocketTimeout:
                continue
            except OSError as exc:
                if exc.errno in (errno.EINTR, errno.EBADF):
                    continue
                if self._stop.is_set():
                    break
                raise
            thread = threading.Thread(
                target=self._connection_guard, args=(conn,), name="execd-conn", daemon=True
            )
            self._threads.append(thread)
            thread.start()
        self._reap_threads()

    def _reap_threads(self) -> None:
        for thread in list(self._threads):
            if not thread.is_alive():
                self._threads.remove(thread)

    def stop(self) -> None:
        """Close the listener and stop accepting. Executions are untouched on purpose.

        Recorders and named sessions live in their own sessions, so a daemon restart neither signals nor
        orphans them: that is what lets us answer "still running" after a restart instead of re-running.
        """
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
        if self._pid_fd is not None:
            try:
                os.close(self._pid_fd)
            except OSError:
                pass
            self._pid_fd = None

    def request_stop(self) -> None:
        """Ask the accept loop to finish. Safe from a signal handler: it only sets a flag."""
        self._stop.set()

    def wait_stopped(self, timeout: float = 5.0) -> None:
        self._stop.wait(timeout)

    # -- transport --------------------------------------------------------------------------

    def _connection_guard(self, conn: socket.socket) -> None:
        try:
            self._handle_connection(conn)
        except Exception as exc:  # noqa: BLE001 - a thread that dies silently is a hung client
            sys.stderr.write(f"execd: connection error: {type(exc).__name__}: {exc}\n")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle_connection(self, conn: socket.socket) -> None:
        peer = _peer_credentials(conn)
        if peer is None:
            _write_line(
                conn, response_error(ExecdError("peer-credentials", "no SO_PEERCRED on this peer"))
            )
            return
        rejection = _reject_peer(peer)
        if rejection is not None:
            # Report it rather than dropping the connection: the answer costs nothing (the peer is already
            # refused) and a silent close in a shared pod reads as a network fault.
            _write_line(conn, response_error(rejection))
            return
        conn.settimeout(30.0)
        request = _read_line(conn, limit=MAX_REQUEST_BYTES)
        if request is None:
            _write_line(
                conn,
                response_error(
                    ExecdError(
                        "invalid-request", "expected one JSON request line (or it was too large)"
                    )
                ),
            )
            return
        try:
            payload = json.loads(request)
        except ValueError as exc:
            _write_line(conn, response_error(ExecdError("invalid-request", f"not JSON: {exc}")))
            return
        _write_line(conn, self.handle_request(payload, peer=peer))

    def _reap_children(self) -> None:
        """Collect recorders and session shells that have finished, so they do not pile up as zombies.

        We are their parent, so nobody else can do this. Nothing in a record depends on their exit status -
        that is what ``rc`` and the pinned metadata are for - so this is only about not leaving corpses in
        the process table of a pod somebody else is sharing with us.
        """
        still_running = []
        for proc in self._children:
            try:
                if proc.poll() is None:
                    still_running.append(proc)
            except Exception:  # noqa: BLE001 - a child we cannot poll is a child we are done with
                continue
        self._children = still_running

    def handle_request(self, payload: object, *, peer: dict | None = None) -> dict:
        """Answer one decoded request. Every path returns a well-formed response, never an exception."""
        self._reap_children()
        if not isinstance(payload, dict):
            return response_error(ExecdError("invalid-request", "request must be a JSON object"))
        if payload.get("protocol") != PROTOCOL_VERSION:
            return response_error(
                ExecdError(
                    "protocol-mismatch",
                    f"this agent speaks protocol {PROTOCOL_VERSION}, request said {payload.get('protocol')!r}",
                )
            )
        method = str(payload.get("method") or "")
        params = payload.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return response_error(ExecdError("invalid-params", "params must be a JSON object"))
        # Authenticate before describing the API: answering "unknown method" to a peer that has not proved
        # it belongs here would hand the method list to whatever managed to open the socket.
        if not _token_matches(self.token, payload.get("token")):
            return response_error(
                ExecdError(
                    "unauthorized",
                    "missing or incorrect execd token (read it from the state directory)",
                    data={"peer_uid": (peer or {}).get("uid")},
                )
            )
        if not method:
            # A request with no method is malformed, which is a different complaint from "I do not know
            # that method" - and only the second one tells the caller the daemon is alive and listening.
            return response_error(ExecdError("invalid-request", "request has no method"))
        if method not in METHODS:
            return response_error(
                ExecdError(
                    "unknown-method",
                    f"unknown method {method!r}",
                    data={"methods": sorted(METHODS)},
                )
            )
        try:
            handler = getattr(self, f"_rpc_{method}")
            return response_ok(handler(params))
        except ExecdError as exc:
            return response_error(exc)
        except Exception as exc:  # noqa: BLE001 - the wire contract is a response, not a traceback
            return response_error(
                ExecdError("agent-error", f"{type(exc).__name__}: {exc}", data={"method": method})
            )

    # -- methods ----------------------------------------------------------------------------

    def _rpc_health(self, params: dict) -> dict:
        counts = {"records": 0, "running": 0, "accepted": 0, "terminal": 0, "unreadable": 0}
        sessions = []
        for exec_id in _iter_record_ids(self.state_dir):
            try:
                state = resolve_record(self.state_dir, exec_id)["state"]
            except ExecdError:
                # An unreadable record is reported, never counted as live: "we lost it" must not quietly
                # upgrade into "it is probably still running".
                counts["unreadable"] += 1
                continue
            counts["records"] += 1
            if state in TERMINAL_STATES:
                counts["terminal"] += 1
            elif state == "accepted":
                counts["accepted"] += 1
            else:
                counts["running"] += 1
        for session_dir in _iter_session_dirs(self.state_dir):
            info = _read_pidinfo(session_dir / PIDINFO_NAME)
            alive = False
            if info:
                alive = bool(
                    inspect_pid(
                        int(info.get("pid") or 0), expect_starttime=info.get("starttime")
                    ).get("alive")
                )
            meta = _read_json(session_dir / META_NAME) or {}
            sessions.append(
                {
                    "session": session_dir.name,
                    "pid": (info or {}).get("pid"),
                    "alive": alive,
                    "generation": (info or {}).get("generation"),
                    "poisoned": (session_dir / POISON_NAME).exists(),
                    "created_at": meta.get("created_at"),
                }
            )
        headroom = disk_headroom(self.state_dir)
        headroom["minimum_bytes"] = self.min_free_bytes
        headroom["minimum_inodes"] = self.min_free_inodes
        headroom["within_limits"] = (
            bool(headroom.get("ok"))
            and int(headroom.get("free_bytes") or 0) >= (self.min_free_bytes)
            and int(headroom.get("free_inodes") or 0) >= self.min_free_inodes
        )
        return {
            "protocol": PROTOCOL_VERSION,
            "agent_version": AGENT_VERSION,
            # What code is behind this socket, for a bootstrap that will only adopt a daemon it can prove is
            # the one it just uploaded. `None` means we could not hash our own file, which a fail-closed
            # caller must treat as a mismatch rather than as "no requirement".
            "agent_sha256": agent_sha256(),
            "pid": os.getpid(),
            "boot_id": self.boot_id,
            "nonce": self.nonce,
            "state_dir": str(self.state_dir),
            "socket": str(self.socket_path),
            "started_at": self.started_at_iso,
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "payload_umask": self.payload_umask,
            "max_page_bytes": self.max_page_bytes,
            "counts": counts,
            "sessions": sessions,
            "disk": headroom,
            "recovery": self.recovery,
        }

    def _rpc_run(self, params: dict) -> dict:
        exec_id = _validate_exec_id(params.get("exec_id"))
        raw_session = params.get("session")
        session = None if raw_session in (None, "") else _validate_session(raw_session)
        commands = _validate_commands(params.get("commands"))
        raw_bootstrap = params.get("session_bootstrap")
        if raw_bootstrap is not None and (
            session is None or not isinstance(raw_bootstrap, str) or not raw_bootstrap.strip()
        ):
            raise ExecdError(
                "invalid-params", "session_bootstrap requires a session and non-empty shell text"
            )
        session_bootstrap = raw_bootstrap if isinstance(raw_bootstrap, str) else None
        if session_bootstrap is not None:
            _validate_commands([session_bootstrap])
        digest = _sha256_payload(commands, session, session_bootstrap)
        run_dir = _run_dir(self.state_dir, exec_id)
        with self._lock:
            existing = _read_json(run_dir / META_NAME)
            if existing is not None:
                if existing.get("payload_sha256") != digest:
                    raise ExecdError(
                        "payload-mismatch",
                        "exec_id is already used by a different payload; use a new exec_id to re-run",
                        data={
                            "exec_id": exec_id,
                            "stored_sha256": existing.get("payload_sha256"),
                            "requested_sha256": digest,
                            "stored_session": existing.get("session"),
                            "requested_session": session,
                            "state": resolve_record(self.state_dir, exec_id)["state"],
                        },
                    )
                return {
                    **_safe_record_view(self._record_view(exec_id)),
                    "created": False,
                    "idempotent": True,
                }

            if session is not None:
                # One run at a time per session. Two payloads in one shell share its cwd, its environment and
                # its two streams, so their output would be interleaved beyond separating and their `cd`
                # effects inseparable. Refusing is the only answer that keeps both records true.
                active = self._session_active_run(session)
                if active is not None:
                    raise ExecdError(
                        "session-busy",
                        f"session {session} is still running {active}; wait for it or cancel it first",
                        data={"active_exec_id": active, "session": session},
                    )
                poison = _read_text(_session_dir(self.state_dir, session) / POISON_NAME)
                if poison is not None and self._session_shell_is_alive(session):
                    # A previous submission reached the shell *partially*, so its input stream now holds half
                    # a command. Anything pushed after it would be parsed as the tail of that half line and
                    # charged to the wrong record, so the session is closed for business while that shell
                    # lives. The marker describes one shell's input stream, not the session name forever: once
                    # the shell is gone the corruption went with it, and the replacement starts clean.
                    raise ExecdError(
                        "session-poisoned",
                        f"session {session} holds a truncated command from an earlier submission; kill its "
                        "shell (or wait for it to exit) and the next run starts a clean one",
                        data={"session": session, "poison": _preview(poison, 400)},
                    )
                # The session's plumbing is prepared here, while nothing of this run exists yet. Its command
                # stream may be missing, or - refused below rather than adopted - be something that is not a
                # fifo at all; either way the honest answer is "nothing was accepted", not a record the
                # caller has to explain to a daemon that never took the work.
                snapshot = self._session_snapshot(session)

            check_disk_space(
                self.state_dir,
                min_free_bytes=self.min_free_bytes,
                min_free_inodes=self.min_free_inodes,
            )
            _ensure_private_dir(run_dir)
            status_log = run_dir / COMMANDS_RC_NAME
            if session is None:
                _touch_stream(run_dir / STDOUT_NAME)
                _touch_stream(run_dir / STDERR_NAME)
            _touch_stream(status_log)

            now = _now_iso()
            meta: dict = {
                "protocol": PROTOCOL_VERSION,
                "exec_id": exec_id,
                "commands": commands,
                "session": session,
                "payload_sha256": digest,
                "created_at": now,
                "agent_version": AGENT_VERSION,
                "daemon_nonce": self.nonce,
                "boot_id": self.boot_id,
                "cwd": os.getcwd(),
                "payload_umask": self.payload_umask,
            }
            if session is not None:
                # The snapshot the fast phase took (and the plumbing it prepared), not a second look: the
                # stream offsets below are the baseline this run's output is measured from, and taking them
                # twice would mean the record described two different instants.
                meta["stdout_start"] = snapshot["stdout_bytes"]
                meta["stderr_start"] = snapshot["stderr_bytes"]
                meta["session_pid_at_submit"] = snapshot["pid"]
                meta["session_restarted"] = not snapshot["alive"]
                meta["session_generation"] = snapshot["generation"] + (
                    0 if snapshot["alive"] else 1
                )
            # Durable before any fork. If we die right here, the record says "accepted, no pid", which is
            # the evidence a later scan needs to answer uncertain instead of running a side effect twice.
            _atomic_write_json(run_dir / META_NAME, meta)

            if session is None:
                self._launch_recorder(exec_id, meta)
                return self._created_view(exec_id, meta)
            # Everything a named session needs that may take seconds - starting its shell, waiting for the
            # FIFO, pushing the source line - happens outside this lock, under the session's own lock. The
            # record is already durable as `accepted` by now, and the fast phase above is what guarantees one
            # live run per session, so waiting here costs this session's throughput and nobody else's.
            session_name = session
        with self._session_lock(session_name):
            self._submit_to_session(
                exec_id, session_name, commands, meta, session_bootstrap=session_bootstrap
            )
        return self._created_view(exec_id, meta)

    def _created_view(self, exec_id: str, meta: dict) -> dict:
        """The `run` answer for a record this call created. Offsets are run-relative, so both start at 0."""
        record = resolve_record(self.state_dir, exec_id)
        return {
            **_safe_record_view(record),
            "created": True,
            "idempotent": False,
            "session_restarted": bool(meta.get("session_restarted")),
            "stdout_offset": 0,
            "stderr_offset": 0,
        }

    def _rpc_attach(self, params: dict) -> dict:
        exec_id = _validate_exec_id(params.get("exec_id"))
        stdout_offset = _nonneg_int(params.get("stdout_offset"), "stdout_offset")
        stderr_offset = _nonneg_int(params.get("stderr_offset"), "stderr_offset")
        max_bytes = _nonneg_int(params.get("max_bytes"), "max_bytes") or self.max_page_bytes
        max_bytes = min(max_bytes, self.max_page_bytes)
        wait_seconds = _wait_seconds(params.get("wait_seconds"))
        deadline = time.monotonic() + wait_seconds
        while True:
            page = self._attach_once(exec_id, stdout_offset, stderr_offset, max_bytes)
            got_bytes = page["stdout_b64"] or page["stderr_b64"]
            if got_bytes or page["terminal"] or time.monotonic() >= deadline:
                return page
            time.sleep(min(0.1, max(0.005, deadline - time.monotonic())))

    def _attach_once(
        self, exec_id: str, stdout_offset: int, stderr_offset: int, max_bytes: int
    ) -> dict:
        with self._lock:
            record = self._record_view(exec_id)
            meta = _read_json(_run_dir(self.state_dir, exec_id) / META_NAME) or {}
            stdout_path, stderr_path = _stream_paths(self.state_dir, meta)
            out_b64, out_next, out_eof = _read_stream(
                stdout_path,
                int(meta.get("stdout_start") or 0),
                stdout_offset,
                max_bytes,
                record["stdout_end"],
            )
            err_b64, err_next, err_eof = _read_stream(
                stderr_path,
                int(meta.get("stderr_start") or 0),
                stderr_offset,
                max_bytes,
                record["stderr_end"],
            )
        return {
            "exec_id": exec_id,
            "state": record["state"],
            "exit_code": record["exit_code"],
            "terminal": record["terminal"],
            "stdout_b64": out_b64,
            "stdout_offset": out_next,
            "stdout_eof": out_eof,
            "stdout_start": record["stdout_start"],
            "stdout_end": record["stdout_end"],
            "stderr_b64": err_b64,
            "stderr_offset": err_next,
            "stderr_eof": err_eof,
            "stderr_start": record["stderr_start"],
            "stderr_end": record["stderr_end"],
            "reason": record.get("reason"),
        }

    def _rpc_status(self, params: dict) -> dict:
        exec_id = _validate_exec_id(params.get("exec_id"))
        with self._lock:
            # A summary, not the raw record: the command text stays on disk (see `_safe_record_view`). Output
            # bytes are untouched by this - `attach` still serves them offset-exact and unredacted.
            return _safe_record_view(self._record_view(exec_id))

    def _rpc_list(self, params: dict) -> dict:
        state_filter = params.get("state")
        session_filter = params.get("session")
        limit = _nonneg_int(params.get("limit"), "limit") or 200
        limit = max(1, min(limit, 5000))
        summaries: list[dict] = []
        problems: list[dict] = []
        for exec_id in _iter_record_ids(self.state_dir):
            try:
                record = resolve_record(self.state_dir, exec_id)
            except ExecdError as exc:
                problems.append({"exec_id": exec_id, "problem": exc.code, "detail": str(exc)})
                continue
            if state_filter and record["state"] != state_filter:
                continue
            if session_filter and record["session"] != session_filter:
                continue
            summaries.append(_record_summary(record))
        summaries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        truncated = len(summaries) > limit
        return {
            "protocol": PROTOCOL_VERSION,
            "agent_version": AGENT_VERSION,
            "count": len(summaries),
            "records": summaries[:limit],
            "truncated": truncated,
            "problems": problems,
        }

    def _rpc_cancel(self, params: dict) -> dict:
        exec_id = _validate_exec_id(params.get("exec_id"))
        grace = params.get("grace_seconds")
        grace = self.cancel_grace_seconds if grace is None else float(grace)
        with self._lock:
            record = self._record_view(exec_id)
            if record["terminal"]:
                return {
                    "exec_id": exec_id,
                    "state": record["state"],
                    "already_finished": True,
                    "delivered": False,
                    "signalled": [],
                    "killed": [],
                    "skipped": [],
                }
            run_dir = _run_dir(self.state_dir, exec_id)
            # Decide what we may signal *before* recording anything: an identity refusal must not leave this
            # record looking as though it had been cancelled by a signal we never sent.
            targets, note = self._cancel_targets(record)
            # The *request* is a fact worth keeping even when nothing can be signalled, so an operator can see
            # it was asked for. It is deliberately not the `cancel` marker: that file is what turns a later
            # `rc` into a `cancelled` state, and it appears only once a signal has actually been delivered.
            meta = _read_json(run_dir / META_NAME) or {}
            meta["cancel_requested_at"] = _now_iso()
            _atomic_write_json(run_dir / META_NAME, meta)

        signalled: list[dict] = []
        killed: list[dict] = []
        skipped: list[dict] = []
        for target in targets:
            # Identity is re-established at the instant of the signal, not inherited from the decision above:
            # what made this pid ours a moment ago is not a promise about now.
            verdict = _target_signalable(target)
            if not verdict["ok"]:
                skipped.append({"pid": target.get("pid"), "reason": verdict["reason"]})
                continue
            if not _send_signal(target["kind"], target["target"], signal.SIGTERM):
                skipped.append({"pid": target.get("pid"), "reason": "term-not-delivered"})
                continue
            signalled.append(target)
            if not (run_dir / CANCEL_NAME).exists():
                # Written right after the first delivery, so its mtime brackets the payload's death as
                # tightly as the file system allows: `rc` only appears once the payload is gone, so an `rc`
                # older than this marker can only mean the run had already finished on its own.
                _atomic_write_bytes(run_dir / CANCEL_NAME, (_now_iso() + "\n").encode("utf-8"))
        deadline = time.monotonic() + max(0.0, grace)
        # Only what we actually asked to stop is escalated. A target whose TERM never landed was not asked,
        # and jumping straight to SIGKILL would be an escalation nobody consented to.
        while signalled and time.monotonic() < deadline:
            if all(_target_gone(item) for item in signalled):
                break
            time.sleep(0.05)
        for target in signalled:
            if _target_gone(target):
                continue
            # Re-checked here above all, because the grace window is exactly where a pid gets recycled: it can
            # be handed to another tenant's job at any moment, and SIGKILL is not a signal to send on a guess.
            verdict = _target_signalable(target)
            if not verdict["ok"]:
                skipped.append({"pid": target.get("pid"), "reason": verdict["reason"]})
                continue
            if _send_signal(target["kind"], target["target"], signal.SIGKILL):
                killed.append(target)
            else:
                skipped.append({"pid": target.get("pid"), "reason": "kill-not-delivered"})
        with self._lock:
            state = self._record_view(exec_id)["state"]
        return {
            "exec_id": exec_id,
            "state": state,
            "grace_seconds": grace,
            # False whenever nothing was actually signalled - the caller's answer to "did my cancel do
            # anything?", and the reason a cancel of a run that had already slipped through says `succeeded`
            # rather than claiming a cancellation it did not cause.
            "delivered": bool(signalled or killed),
            "signalled": signalled,
            "killed": killed,
            "skipped": skipped,
            "note": note,
        }

    def _rpc_cleanup(self, params: dict) -> dict:
        exec_id = _validate_exec_id(params.get("exec_id"))
        with self._lock:
            record = self._record_view(exec_id)
            if not record["terminal"]:
                raise ExecdError(
                    "not-terminal",
                    "only a terminal execution may be cleaned up; cancel it and wait for it to settle",
                    data={"exec_id": exec_id, "state": record["state"]},
                )
            writers = _live_writers(self.state_dir, exec_id)
            if writers:
                # Terminal *on paper* is not terminal. A record can be pinned as uncertain while the recorder
                # it was pinned about is still running (the daemon died and guessed), and unlinking the
                # directory underneath a live process throws its future output away with nobody the wiser.
                raise ExecdError(
                    "record-in-use",
                    "a process belonging to this execution is still alive; cleanup refuses until it is gone",
                    data={"exec_id": exec_id, "state": record["state"], "holders": writers},
                )
            run_dir = _run_dir(self.state_dir, exec_id)
            if not run_dir.is_dir():
                raise ExecdError("unknown-exec", f"no record directory for {exec_id}")
            removed: list[str] = []
            failed: list[dict] = []
            for name in RUN_FILE_NAMES:
                path = run_dir / name
                if not path.exists():
                    continue
                if not _contained(path, self.state_dir) or path.is_dir():
                    failed.append(
                        {"file": name, "error": "refused: not a plain file inside the state dir"}
                    )
                    continue
                try:
                    os.unlink(path)
                    removed.append(name)
                except OSError as exc:
                    failed.append({"file": name, "error": str(exc)})
            directory_removed = False
            directory_error: str | None = None
            try:
                os.rmdir(
                    run_dir
                )  # rmdir, never rmtree: a name we did not list keeps the directory alive
                directory_removed = True
            except OSError as exc:
                directory_error = exc.strerror or str(exc)
            _fsync_dir(self.state_dir / RUNS_DIR_NAME)
            return {
                "exec_id": exec_id,
                "state": record["state"],
                "removed": removed,
                "failed": failed,
                "directory_removed": directory_removed,
                "directory_error": directory_error,
                "complete": directory_removed and not failed,
                "session_streams_kept": bool(record.get("session")),
            }

    # -- record helpers ---------------------------------------------------------------------

    def _record_view(self, exec_id: str) -> dict:
        """Resolve, and freeze the answer if this call is what made the record terminal."""
        record = resolve_record(self.state_dir, exec_id)
        if record["terminal"]:
            record = pin_terminal_record(self.state_dir, exec_id)
        return record

    def _pin_record(
        self, exec_id: str, *, state: str, reason: str, exit_code: int | None = None
    ) -> None:
        # Locked end to end: a concurrent `status` call may be pinning the same record from what it can see
        # on disk, and read-then-write without the lock would let one of those two decisions vanish.
        with self._lock:
            run_dir = _run_dir(self.state_dir, exec_id)
            meta = _read_json(run_dir / META_NAME) or {}
            if meta.get("terminal_state") in TERMINAL_STATES:
                return
            meta.update(
                {
                    "terminal_state": state,
                    "exit_code": exit_code,
                    "reason": reason,
                    "finished_at": _now_iso(),
                }
            )
            _atomic_write_json(run_dir / META_NAME, meta)

    def _update_meta(self, exec_id: str, updates: dict) -> dict:
        """Read-merge-write one record's metadata, under the daemon lock.

        For callers that are *not* already holding ``self._lock`` - which is everything that happens in a
        session submission, since that deliberately runs outside it. Never call this from inside a locked
        block: the lock is not reentrant, and that would be a self-inflicted deadlock.
        """
        with self._lock:
            run_dir = _run_dir(self.state_dir, exec_id)
            meta = _read_json(run_dir / META_NAME) or {}
            meta.update(updates)
            _atomic_write_json(run_dir / META_NAME, meta)
            return meta

    def _launch_recorder(self, exec_id: str, meta: dict) -> None:
        """Fork the holder for a direct run. The caller holds ``self._lock``, so this writes meta directly."""
        run_dir = _run_dir(self.state_dir, exec_id)
        argv = [
            sys.executable or "python3",
            str(Path(__file__).resolve()),
            "recorder",
            "--state-dir",
            str(self.state_dir),
            "--exec-id",
            exec_id,
            "--payload-umask",
            self.payload_umask,
        ]
        log_fd = os.open(run_dir / RECORDER_LOG_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            proc = subprocess.Popen(  # noqa: S603 - our own argv, no shell
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                cwd=meta.get("cwd") or str(self.state_dir),
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            os.close(log_fd)
            _atomic_write_bytes(
                run_dir / SPAWN_ERROR_NAME,
                f"recorder spawn failed: {type(exc).__name__}: {exc}\n".encode(),
            )
            _atomic_write_bytes(run_dir / RC_NAME, b"127\n")
            return
        os.close(log_fd)
        self._children.append(proc)
        meta["recorder_pid"] = proc.pid
        # The starttime is what makes the pid usable as an identity after a restart: without it, a recycled
        # pid could make a long-dead run look alive again. If the recorder has already exited by the time we
        # look, there is nothing to record and the weaker liveness-only check applies.
        meta["recorder_starttime"] = _read_starttime(proc.pid)
        meta["recorder_started_at"] = _now_iso()
        _atomic_write_json(run_dir / META_NAME, meta)

    def _cancel_targets(self, record: dict) -> tuple[list[dict], str | None]:
        """What a cancel may signal, and what it may not.

        A direct run owns its process group (its recorder started it in a new session), so the whole group
        goes - background children included, which is the point. A run inside a named session does *not* own
        its group: it shares the session shell's, so ``killpg`` there would take the session and everybody
        else's work in it down. There we walk the session shell's process tree instead and keep only the
        processes this run can be *proved* to own - the ones whose environment still carries this run's
        ``exec_id``, which every process the batch forked inherited from its first line.

        That attribution is what protects a process an *earlier* run left behind: it inherited its own
        exec_id, not this one's, so it is not ours to kill. The trade-off runs the safe way on purpose. A
        payload that scrubs its own environment becomes unkillable by us rather than killed on a guess, and
        the caller is told nothing was signalled instead of being promised a cancellation that did not
        happen.
        """
        if record.get("session"):
            return self._session_cancel_targets(record)
        info = _read_pidinfo(_run_dir(self.state_dir, str(record["exec_id"])) / PIDINFO_NAME)
        if info is None:
            return (
                [],
                "no-payload-pid: the recorder has not named a process yet, so there is nothing to signal",
            )
        pid = int(info.get("pid") or 0)
        anchor = info.get("starttime")
        check = inspect_pid(pid, expect_starttime=anchor)
        if check.get("unknown"):
            # No /proc, so identity cannot be established at all. "Cannot tell" never becomes "signal it".
            return (
                [],
                "proc-unavailable: the payload's identity cannot be checked, so nothing is signalled",
            )
        if not check.get("alive"):
            reason = str(check.get("reason"))
            if reason in ("not-found", "zombie"):
                # It really did go away: the payload exited (or became a corpse awaiting its recorder)
                # between the pid being written down and this cancel arriving. Nothing is left to signal -
                # the same late fact a session cancel reports, not an API error, and not a cancellation we
                # caused: the run's own exit code is what stands.
                return (
                    [],
                    f"payload-gone: the payload pid {pid} had already exited before this cancel, so "
                    "nothing was signalled and its own exit code stands",
                )
            # The pid is occupied by a process that is not the one we recorded - recycled, or somebody
            # else's on a shared pod. A real anomaly, so it stays a refusal the caller sees rather than a
            # quiet no-op, and above all it is never signalled.
            raise ExecdError(
                "process-identity-mismatch",
                "the recorded payload pid is not our process any more; refusing to signal it",
                data={"exec_id": record["exec_id"], "pid": pid, "reason": reason},
            )
        # The anchor travels with the target: every signal and every recheck from here compares the live
        # process with what the recorder wrote, never with whatever occupies the pid next.
        target = _target_for_pid(pid, group=True, expect_starttime=anchor)
        if target is None:
            return [], "identity-check-failed: the payload process is not signallable by us"
        return [target], "process-group"

    def _session_cancel_targets(self, record: dict) -> tuple[list[dict], str]:
        """Per-exec attribution inside a shared session shell. See :meth:`_cancel_targets`."""
        session = str(record.get("session"))
        exec_id = str(record["exec_id"])
        meta = _read_json(_run_dir(self.state_dir, exec_id) / META_NAME) or {}
        info = _read_pidinfo(_session_dir(self.state_dir, session) / PIDINFO_NAME)
        if info is None:
            return [], "session-missing: no shell to attribute this run's processes to"
        if int(info.get("generation") or 0) != int(meta.get("session_generation") or 0):
            # A replacement shell. Its children belong to whoever is using it now, and none of them ran this
            # payload, so the shell's identity alone would not justify signalling anything in it.
            return [], "session-generation-changed: the shell that was given this payload is gone"
        shell = inspect_pid(int(info.get("pid") or 0), expect_starttime=info.get("starttime"))
        if shell.get("unknown"):
            return (
                [],
                "proc-unavailable: cannot identify the session shell, so nothing is signalled",
            )
        if not shell.get("alive"):
            return [], "session-lost: the shell that was given this payload is gone"
        targets: list[dict] = []
        unreadable = 0
        # `_descendants` never yields the shell itself, which is what keeps a run's cancel from taking the
        # shared session down - the shell is the session, and killing it is not this record's business.
        for pid in _descendants(int(info["pid"])):
            readable, value = _proc_environment(pid, EXEC_ID_ENV)
            if not readable:
                unreadable += 1
                continue
            if value != exec_id:
                continue
            # `attributed_to` is what makes the environment proof outlast this line: the exec_id is re-read
            # before every signal, so a pid freed after selection cannot be claimed by its replacement on the
            # strength of a check that was true of the previous occupant.
            target = _target_for_pid(pid, group=False, attributed_to=exec_id)
            if target is not None:
                targets.append(target)
        if targets:
            return targets, "process-tree-attributed"
        if unreadable:
            return [], (
                f"no-attributed-process: {unreadable} descendant(s) had unreadable environments, and an "
                "unattributable process is never signalled"
            )
        return [], (
            "no-attributed-process: no descendant carries this exec_id, so the session shell is between "
            "commands or the payload replaced its own environment"
        )

    # -- named sessions ---------------------------------------------------------------------

    def _session_lock(self, session: str) -> threading.Lock:
        """The one lock that may be held across a session's slow FIFO work."""
        with self._session_locks_guard:
            lock = self._session_locks.get(session)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session] = lock
            return lock

    def _session_shell_is_alive(self, session: str) -> bool:
        """Whether the session's recorded shell is still the process it named - asking, not preparing.

        Used where a refusal must not have side effects: `_session_snapshot` is the one that creates the
        session's directories and verifies its command fifo, and a request we are about to turn down should
        not have built anything on the way out.
        """
        info = _read_pidinfo(_session_dir(self.state_dir, session) / PIDINFO_NAME)
        if not info:
            return False
        return bool(
            inspect_pid(int(info.get("pid") or 0), expect_starttime=info.get("starttime")).get(
                "alive"
            )
        )

    def _session_snapshot(self, session: str) -> dict:
        sdir = _session_dir(self.state_dir, session)
        _ensure_private_dir(sdir)
        _touch_stream(sdir / STDOUT_NAME)
        _touch_stream(sdir / STDERR_NAME)
        _ensure_fifo(sdir / FIFO_NAME)
        meta = _read_json(sdir / META_NAME)
        if meta is None:
            meta = {"session": session, "created_at": _now_iso(), "generation": 0, "history": []}
            _atomic_write_json(sdir / META_NAME, meta)
        info = _read_pidinfo(sdir / PIDINFO_NAME)
        alive = False
        if info:
            alive = bool(
                inspect_pid(int(info.get("pid") or 0), expect_starttime=info.get("starttime")).get(
                    "alive"
                )
            )
        return {
            "alive": alive,
            "pid": (info or {}).get("pid"),
            "generation": int((info or {}).get("generation") or meta.get("generation") or 0),
            "stdout_bytes": _file_size(sdir / STDOUT_NAME),
            "stderr_bytes": _file_size(sdir / STDERR_NAME),
            "poisoned": (sdir / POISON_NAME).exists(),
            "meta": meta,
        }

    def _ensure_session_shell(self, session: str, *, started_by: str) -> dict:
        """Start the session's bash if it is not alive, and wait until its command FIFO can be opened."""
        sdir = _session_dir(self.state_dir, session)
        snapshot = self._session_snapshot(session)
        if snapshot["alive"]:
            return {**snapshot, "restarted": False}
        # A dead session is replaced, never resurrected: its cwd, exported variables and background jobs died
        # with it. Callers see `session_restarted` and decide whether that is acceptable.
        argv = [
            sys.executable or "python3",
            str(Path(__file__).resolve()),
            "recorder",
            "--state-dir",
            str(self.state_dir),
            "--session",
            session,
            "--start-session",
            "--payload-umask",
            self.payload_umask,
        ]
        log_fd = os.open(sdir / RECORDER_LOG_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            proc = subprocess.Popen(  # noqa: S603 - our own argv, no shell
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                cwd=os.getcwd(),
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            os.close(log_fd)
            raise ExecdError(
                "session-spawn-failed", f"cannot start the shell for session {session}: {exc}"
            ) from exc
        os.close(log_fd)
        self._children.append(proc)
        starttime = _read_starttime(proc.pid)
        generation = int(snapshot["generation"]) + 1
        pidinfo = {
            "pid": proc.pid,
            "pgid": proc.pid,
            "starttime": starttime,
            "generation": generation,
            "started_at": _now_iso(),
            "started_by": started_by,
            "shell": choose_shell(),
        }
        _atomic_write_json(sdir / PIDINFO_NAME, pidinfo)
        meta = _read_json(sdir / META_NAME) or {}
        history = list(meta.get("history") or [])
        history.append(
            {"generation": generation, "pid": proc.pid, "started_at": pidinfo["started_at"]}
        )
        meta.update(
            {
                "generation": generation,
                "updated_at": _now_iso(),
                "pid": proc.pid,
                "history": history[-20:],
            }
        )
        _atomic_write_json(sdir / META_NAME, meta)
        for stale in ("DEAD", POISON_NAME):
            # A brand-new shell has an empty input stream, which is the one thing a poison marker was
            # warning about, and `DEAD` is a note about the shell that has just gone. Both die with it.
            try:
                os.unlink(sdir / stale)
            except OSError:
                pass
        if not _wait_for_fifo_reader(
            sdir / FIFO_NAME, timeout=max(1.0, self.session_write_timeout)
        ):
            raise ExecdError(
                "session-not-ready",
                f"session {session} shell started but never opened its command FIFO",
                data={"pid": proc.pid, "generation": generation},
            )
        return {**self._session_snapshot(session), "restarted": True, "generation": generation}

    def _submit_to_session(
        self,
        exec_id: str,
        session: str,
        commands: list[str],
        meta: dict,
        *,
        session_bootstrap: str | None,
    ) -> None:
        """Hand a batch to a named session: stage it, then tell the shell to source it.

        Called with the session's lock held and the daemon's global lock *not* held, which is what lets a
        wedged shell block its own session rather than the whole daemon. The record is already durable
        (``accepted``) by the time we get here, so every outcome below is an update to a record a caller can
        already ask about.

        The script never goes down the pipe. Staging it as the record's ``payload.sh`` and pushing one
        ``. '<path>'`` line keeps a partially-delivered submission from turning into a half command in the
        shell's input stream - and if even that line is truncated, the session is poisoned instead of being
        left to charge the next caller's command to this record.
        """
        sdir = _session_dir(self.state_dir, session)
        run_dir = _run_dir(self.state_dir, exec_id)
        rc_path = run_dir / RC_NAME
        status_log = run_dir / COMMANDS_RC_NAME
        poison = _read_text(sdir / POISON_NAME)
        if poison is not None and self._session_snapshot(session)["alive"]:
            # Re-checked here as well as in `run`: the marker is the session's state, not the caller's word
            # for it. It is only a refusal while the poisoned shell still lives - a dead shell took the
            # truncated line with it, and `_ensure_session_shell` clears the marker when it starts the
            # replacement, which is the recovery path this check must not block.
            #
            # It is also a *terminal* answer here: the record has been durable since the fast phase, and this
            # branch means no byte of it reached a shell. Leaving it `accepted` would be the one outcome
            # worse than refusing - a record that looks like work in flight while nothing is running it.
            self._pin_record(exec_id, state="failed", reason="session-poisoned", exit_code=None)
            raise ExecdError(
                "session-poisoned",
                f"session {session} holds a truncated command from an earlier submission",
                data={"session": session, "poison": _preview(poison, 400)},
            )
        try:
            ready = self._ensure_session_shell(session, started_by=exec_id)
        except ExecdError as exc:
            # Nothing reached a shell, so nothing ran: `failed` is a real answer, not a hedge.
            self._pin_record(exec_id, state="failed", reason=exc.code, exit_code=None)
            raise
        self._update_meta(
            exec_id, {"session_pid": ready["pid"], "session_generation": ready["generation"]}
        )
        payload_path = run_dir / PAYLOAD_NAME
        _atomic_write_bytes(
            payload_path,
            build_session_script(
                commands,
                exec_id=exec_id,
                rc_path=rc_path,
                status_log=status_log,
                bootstrap=session_bootstrap if ready["restarted"] else None,
            ).encode("utf-8"),
        )
        line = build_session_source_line(payload_path, rc_path=rc_path)
        try:
            written = _write_fifo_text(
                sdir / FIFO_NAME, line, deadline=time.monotonic() + self.session_write_timeout
            )
        except OSError as exc:
            if written_count(exc) == 0:
                # No byte reached the shell: the payload provably did not start, and the shell's input stream
                # is untouched, so the session stays usable for the next caller.
                self._pin_record(
                    exec_id, state="failed", reason="session-not-writable", exit_code=None
                )
                raise ExecdError(
                    "session-not-writable",
                    f"cannot write to session {session}: {exc}",
                ) from exc
            # Some bytes are in the stream and they are not a whole line. The shell will read the next
            # submission as their continuation, which would run someone else's command under this record's
            # name, so the session is closed for business and this record stays uncertain: the payload may
            # or may not run, and nothing we can do from here can find out.
            self._poison_session(session, exec_id=exec_id, reason=f"partial write: {exc}")
            self._pin_record(exec_id, state="outcome_uncertain", reason="session-write-incomplete")
            raise ExecdError(
                "session-write-incomplete",
                f"only part of the payload reached session {session}; the execution is uncertain and the "
                "session will refuse new work until its shell is replaced",
            ) from exc
        self._update_meta(exec_id, {"session_bytes_written": written})

    def _poison_session(self, session: str, *, exec_id: str, reason: str) -> None:
        """Close a session whose input stream now holds part of a command.

        The marker is a file rather than an attribute because the next daemon to start over this state
        directory inherits the same broken shell, and a poisoned session has to stay refused across a
        restart. Nothing but replacing the shell clears it - that is the only thing that flushes its input.
        """
        sdir = _session_dir(self.state_dir, session)
        marker = json.dumps(
            {"session": session, "exec_id": exec_id, "reason": reason, "at": _now_iso()},
            sort_keys=True,
        )
        try:
            _atomic_write_bytes(sdir / POISON_NAME, (marker + "\n").encode("utf-8"))
        except OSError as exc:  # pragma: no cover - only on a filesystem that has already failed us
            sys.stderr.write(f"execd: cannot mark session {session} poisoned: {exc}\n")

    def _session_active_run(self, session: str) -> str | None:
        for exec_id in _iter_record_ids(self.state_dir):
            meta = _read_json(_run_dir(self.state_dir, exec_id) / META_NAME)
            if not meta or meta.get("session") != session:
                continue
            state = resolve_record(self.state_dir, exec_id)["state"]
            if state in ("accepted", "running"):
                return exec_id
        return None


def _record_summary(record: dict) -> dict:
    return {
        "exec_id": record["exec_id"],
        "session": record.get("session"),
        "state": record["state"],
        "terminal": record.get("terminal"),
        "exit_code": record.get("exit_code"),
        "reason": record.get("reason"),
        "created_at": record.get("created_at"),
        "finished_at": record.get("finished_at"),
        "payload_sha256": record.get("payload_sha256"),
        "command_count": len(record.get("commands") or []),
        "command_statuses": record.get("command_statuses") or [],
        "stdout_bytes": record.get("stdout_bytes"),
        "stderr_bytes": record.get("stderr_bytes"),
        "payload_pid": record.get("payload_pid"),
        "session_pid": record.get("session_pid"),
        "cancel_requested": record.get("cancel_requested"),
        "recovered": record.get("recovered"),
    }


def _safe_record_view(record: dict) -> dict:
    """Everything ``status`` and an idempotent ``run`` may say about a record - and no command text.

    The commands are the one part of a record that is somebody's secret by default: a credential on a
    command line is completely routine, and on this platform the control plane polls ``status`` far more
    often than any human reads a command back. The digest says whether it is the payload you sent, the count
    and the per-command statuses say how much of it ran and how each part ended, and the byte windows say
    where its output starts and stops - so refusing to echo the text costs a caller nothing it could not get
    another way. Output itself is never summarised away here: ``attach`` still serves the raw bytes,
    offset-exact, because byte fidelity on the streams is the promise the protocol makes.
    """
    view = _record_summary(record)
    view.update(
        {
            "payload_pgid": record.get("payload_pgid"),
            "recorder_pid": record.get("recorder_pid"),
            "recorder_alive": record.get("recorder_alive"),
            "session_generation": record.get("session_generation"),
            "session_restarted": record.get("session_restarted"),
            "stdout_path": record.get("stdout_path"),
            "stderr_path": record.get("stderr_path"),
            "stdout_start": record.get("stdout_start"),
            "stdout_end": record.get("stdout_end"),
            "stderr_start": record.get("stderr_start"),
            "stderr_end": record.get("stderr_end"),
            "cancel_requested_at": record.get("cancel_requested_at"),
        }
    )
    for key in ("process_check", "rc_overrode_pinned_state", "spawn_error"):
        if record.get(key) is not None:
            view[key] = record[key]
    if record.get("uncertain"):
        view["uncertain"] = True
    return view


def _iter_record_ids(state_dir: Path) -> list[str]:
    runs = state_dir / RUNS_DIR_NAME
    try:
        return sorted(entry.name for entry in runs.iterdir() if entry.is_dir())
    except OSError:
        return []


def _iter_session_dirs(state_dir: Path) -> list[Path]:
    sessions = state_dir / SESSIONS_DIR_NAME
    try:
        return sorted(entry for entry in sessions.iterdir() if entry.is_dir())
    except OSError:
        return []


def _validate_commands(value: object) -> list[str]:
    if isinstance(value, str):
        raise ExecdError(
            "invalid-params", "commands must be a list of command strings, not one string"
        )
    if not isinstance(value, list) or not value:
        raise ExecdError("invalid-params", "commands must be a non-empty list of command strings")
    commands: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ExecdError("invalid-params", "every command must be a string")
        commands.append(item)
    total = sum(len(item.encode("utf-8")) for item in commands)
    if total > MAX_PAYLOAD_BYTES:
        raise ExecdError(
            "payload-too-large",
            f"commands total {total} bytes, over the {MAX_PAYLOAD_BYTES} byte limit",
        )
    return commands


def _nonneg_int(value: object, field: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            parsed = int(str(value))
        except (TypeError, ValueError) as exc:
            raise ExecdError("invalid-params", f"{field} must be a non-negative integer") from exc
    else:
        parsed = int(value)
    if parsed < 0:
        raise ExecdError("invalid-params", f"{field} must be a non-negative integer")
    return parsed


def _wait_seconds(value: object) -> float:
    if value in (None, ""):
        return 0.0
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ExecdError("invalid-params", "wait_seconds must be a number") from exc
    if seconds < 0:
        raise ExecdError("invalid-params", "wait_seconds must not be negative")
    return min(seconds, MAX_WAIT_SECONDS)


def _read_stream(
    path: Path, start: int, offset: int, limit: int, end: int | None
) -> tuple[str, int, bool]:
    """One page of one stream, in run-relative bytes.

    The client's offset is the only cursor, and a page is exactly `[offset, offset+len)` of the run's own
    slice, so re-asking for an offset it already consumed returns nothing new rather than repeating bytes.
    ``offset > size`` is an error and never a silent EOF: the only ways to reach it are a truncated or
    deleted log, and pretending we simply have no more data would lose the difference between "finished"
    and "your bytes are gone".
    """
    absolute = start + offset
    if end is not None and absolute > end:
        raise ExecdError(
            "offset-out-of-range",
            f"offset {offset} is past the end of this run's {path.name} slice",
            data={"stream": path.name, "offset": offset, "max_offset": max(0, end - start)},
        )
    size = _file_size(path)
    if absolute > size:
        raise ExecdError(
            "offset-out-of-range",
            f"offset {offset} is past the end of {path.name} ({size - start} bytes exist)",
            data={"stream": path.name, "offset": offset, "max_offset": max(0, size - start)},
        )
    if end is not None:
        size = min(size, end)
    if absolute >= size:
        return "", offset, True
    try:
        with open(path, "rb") as handle:
            handle.seek(absolute)
            data = handle.read(min(limit, size - absolute))
    except FileNotFoundError:
        raise ExecdError("stream-missing", f"{path.name} disappeared") from None
    except OSError as exc:
        raise ExecdError("stream-unreadable", f"cannot read {path.name}: {exc}") from exc
    next_offset = offset + len(data)
    return base64.b64encode(data).decode("ascii"), next_offset, next_offset >= size


def _target_for_pid(
    pid: int, *, group: bool, expect_starttime: str | None = None, attributed_to: str | None = None
) -> dict | None:
    """Build a signal target for `pid`, anchored on the identity we were *told* to trust.

    ``expect_starttime`` is not a hint, it is the anchor: passing it here means the check below and every
    later escalation compare against the starttime the recorder wrote down, never against a starttime read
    after the decision was made. Anchoring on a fresh read is what made a recycle invisible - a pid freed
    between the identity check and this line would supply its own new starttime, and the pre-SIGKILL recheck
    would then faithfully confirm that the stranger is still the stranger.
    """
    check = inspect_pid(pid, expect_starttime=expect_starttime)
    if not check.get("alive") or check.get("uid") not in (None, os.getuid()):
        return None
    kind, target = _signal_target(pid, group=group)
    if kind == "missing":
        return None
    return {
        "kind": kind,
        "target": target,
        "pid": pid,
        "pgid": check.get("pgid"),
        # Carried with the target so an escalation later can prove it is still the same process (see
        # `_target_still_ours`). A pid on its own is a claim about a slot in a table, not about a process.
        # When we were given an expectation it *is* the anchor; the freshly read value is only used when we
        # had nothing recorded to go by.
        "starttime": expect_starttime or check.get("starttime"),
        # Set when the process was picked out by its environment inside a shared session: the exec_id is the
        # proof of ownership, so it is re-read before every signal rather than trusted from selection time.
        "attributed_to": attributed_to,
    }


def _target_signalable(target: dict) -> dict:
    """Compare one target with the live process right now, immediately before a signal is sent.

    Both proofs a target can rest on are re-checked at the moment of signalling, not at the moment they were
    decided: the pid still holds the process we identified (``starttime``, and for a group target the pgid
    that made the group ours), and - when the process was chosen by its environment - that environment still
    carries the exec_id that made it ours. Pids get recycled and a session shell hands its environment to
    whoever it runs next, so a decision is only valid for the instant it was made.
    """
    if target.get("attributed_to") is not None:
        readable, value = _proc_environment(int(target.get("pid") or 0), EXEC_ID_ENV)
        if not readable:
            return {"ok": False, "reason": "attribution-unreadable"}
        if value != target["attributed_to"]:
            return {"ok": False, "reason": "attribution-changed"}
    return _target_still_ours(target)


def _target_still_ours(target: dict) -> dict:
    """Is this target still the process we meant to signal?

    This is asked before *every* signal, not only before the escalation: TERM has the same problem as KILL,
    just a gentler one, and the gap between choosing a target and signalling it is a gap all the same.
    ``starttime`` is what closes it - it is carried on the target as the anchor it was chosen under, and if it
    moved, the pid is a stranger now and SIGKILL to a stranger is not a thing a shared pod survives politely.
    A target with no anchor at all cannot answer the question, so it is not signalled either: "I think this
    pid is still mine" is exactly the claim a recycled pid makes. For a group target the leader's identity is
    what authorised the group, so a leader whose pgid no longer matches is refused too.
    """
    pid = int(target.get("pid") or 0)
    if not target.get("starttime"):
        return {"ok": False, "reason": "identity-unanchored"}
    check = inspect_pid(pid, expect_starttime=target.get("starttime"))
    if check.get("unknown"):
        return {"ok": False, "reason": check.get("reason") or "identity-unknown"}
    if check.get("reason") == "pid-recycled":
        return {"ok": False, "reason": "pid-recycled"}
    if not check.get("alive"):
        return {"ok": False, "reason": "already-gone"}
    if target.get("kind") == "killpg" and check.get("pgid") != target.get("target"):
        return {"ok": False, "reason": "pgid-changed"}
    return {"ok": True, "reason": None}


def _target_gone(target: dict) -> bool:
    pid = int(target.get("pid") or 0)
    if target.get("kind") == "killpg":
        try:
            os.killpg(int(target["target"]), 0)
            return False
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            return True
    return not inspect_pid(pid).get("alive")


def _read_starttime(pid: int) -> str | None:
    _state, fields = _proc_stat(pid)
    return fields.get("starttime") if fields else None


def _ensure_fifo(path: Path) -> None:
    """Make ``path`` a private FIFO, and refuse to treat anything else as one.

    Creation is attempted *first* and ``FileExistsError`` is the concurrent case, because the natural
    ``exists()``-then-``mkfifo()`` order is a race in both directions. Two submissions that both saw no FIFO
    left one of them dying on ``FileExistsError``, which reached the caller as an opaque ``agent-error``. And
    an existing path is not necessarily a FIFO: that order adopted whatever was already there - a plain file
    planted under a world-writable ``/tmp``, or debris from an earlier bug - as a session's command stream and
    just chmod'd it, so the shell's input would have been an ordinary file nobody was reading.

    So the answer is decided by what is at the path *after* the attempt: a FIFO we made, or a FIFO somebody
    else made, is ours to use; anything else is an explicit refusal. ``lstat`` because a symlink is not a
    FIFO no matter what it points at, and because the chmod that follows must not be steered by one.
    """
    try:
        os.mkfifo(path, 0o600)
    except FileExistsError:
        pass  # concurrent creation or a pre-existing path; what it actually is gets verified below
    except OSError as exc:
        raise ExecdError(
            "session-fifo-unusable",
            f"cannot create the session command fifo at {path}: {exc}",
            data={"path": str(path), "errno": exc.errno},
        ) from None
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise ExecdError(
            "session-fifo-unusable",
            f"the session command fifo at {path} vanished while it was being prepared: {exc}",
            data={"path": str(path), "errno": exc.errno},
        ) from None
    if not stat.S_ISFIFO(mode):
        raise ExecdError(
            "session-fifo-unusable",
            f"{path} is not a fifo (it is {stat.filemode(mode)!r}); refusing to use it as a session's "
            "command stream",
            data={"path": str(path), "filemode": stat.filemode(mode)},
        )
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        raise ExecdError(
            "session-fifo-unusable",
            f"the session command fifo at {path} cannot be made private: {exc}",
            data={"path": str(path), "errno": exc.errno},
        ) from None


def _wait_for_fifo_reader(path: Path, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno != errno.ENXIO:
                return False
            time.sleep(0.02)
            continue
        os.close(fd)
        return True
    return False


def _write_fifo_text(path: Path, text: str, *, deadline: float) -> int:
    """Push one payload into a session shell's FIFO without ever blocking the daemon indefinitely.

    ``O_NONBLOCK`` is what makes a dead session an error instead of a hang: with no reader the open fails
    with ``ENXIO`` at once, whereas a blocking open would sit there until the shell came back - which is
    precisely the failure mode this daemon must report rather than exhibit.

    The write is attempted *before* waiting on writability, because the wait cannot be trusted here: the
    session shell holds the FIFO ``O_RDWR`` (that is what keeps it from seeing EOF when our write end
    closes), and on such a FIFO ``select`` was measured to report no writability while ``write`` returned
    immediately. Gating on readiness therefore stalls forever; gating on the write's own ``EAGAIN`` is the
    only condition that actually means "the pipe is full", and it still honours the deadline.

    ``written`` is decision-grade, not diagnostic: a caller distinguishes "nothing started" from "half a
    line is in the shell's input stream" by it. Measured on a Linux pipe, a non-blocking write larger than
    ``PIPE_BUF`` is all-or-nothing below one atomic block of room (it returns ``EAGAIN`` having taken 0 bytes,
    even when 20 bytes were free) and delivers exactly the free block when at least ``PIPE_BUF`` is room, so
    both 0 and a genuine partial count are real, reproducible states rather than edge-case guesses.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise _FifoError(exc, written=0) from None
    data = text.encode("utf-8")
    written = 0
    try:
        while written < len(data):
            try:
                written += os.write(fd, data[written:])
                continue
            except BlockingIOError:
                pass  # the shell is not draining the pipe right now; wait, but not past the deadline
            except OSError as exc:
                raise _FifoError(exc, written=written) from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _FifoError(TimeoutError("deadline"), written=written)
            select.select([], [fd], [], min(remaining, 0.2))
    finally:
        os.close(fd)
    return written


class _FifoError(OSError):
    """An FIFO write that says how much of the payload actually left, which decides failed vs uncertain."""

    def __init__(self, cause: BaseException, *, written: int) -> None:
        super().__init__(getattr(cause, "errno", None), str(cause))
        self.written = written
        self.cause = cause


def written_count(exc: OSError) -> int:
    return int(getattr(exc, "written", 0) or 0)


def _token_matches(expected: str, got: object) -> bool:
    if not isinstance(got, str) or not expected:
        return False
    import hmac

    return hmac.compare_digest(expected, got)


def _peer_credentials(conn: socket.socket) -> dict | None:
    """``SO_PEERCRED`` for the other end, as the kernel saw it when the connection was accepted."""
    peercred = getattr(socket, "SO_PEERCRED", None)
    if peercred is None:  # pragma: no cover - non-Linux
        return None
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("iii"))
    except OSError:
        return None
    if len(raw) < 12:
        return None
    pid, uid, gid = struct.unpack("iii", raw[:12])
    return {"pid": pid, "uid": uid, "gid": gid}


def _reject_peer(peer: dict) -> ExecdError | None:
    """Only our own uid may talk to the daemon.

    The socket file is already 0600 in a 0700 directory, so this normally duplicates the file system's
    answer - and that is the point. A bind mount, a broadened ACL, or an fd passed across the pod all
    reach a socket without touching its mode bits, and the peer uid is the one claim about the other end
    that the kernel, not the file system, is testifying to.
    """
    uid = peer.get("uid")
    if uid != os.getuid():
        return ExecdError(
            "peer-uid-mismatch",
            "execd only accepts connections from its own uid",
            data={"peer_uid": uid, "daemon_uid": os.getuid(), "peer_pid": peer.get("pid")},
        )
    return None


SocketTimeout = socket.timeout


def _write_line(conn: socket.socket, payload: dict) -> None:
    data = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    try:
        conn.sendall(data)
    except OSError:
        return


def _read_line(conn: socket.socket, *, limit: int) -> str | None:
    buffer = bytearray()
    while True:
        try:
            chunk = conn.recv(65536)
        except (TimeoutError, SocketTimeout):
            return None
        except OSError:
            return None
        if not chunk:
            break
        buffer.extend(chunk)
        if b"\n" in buffer:
            line, _rest = buffer.split(b"\n", 1)
            return line.decode("utf-8", errors="replace")
        if len(buffer) > limit:
            return None
    if not buffer:
        return None
    return buffer.decode("utf-8", errors="replace")


def _socket_is_live(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        probe.connect(str(path))
    except OSError:
        return False
    try:
        probe.close()
    except OSError:
        pass
    return True


# ---------------------------------------------------------------------------------------------
# recorder: the process that holds one execution
# ---------------------------------------------------------------------------------------------


def run_recorder(state_dir: Path, exec_id: str, *, payload_umask: str | None = None) -> int:
    """Hold one execution: start it in its own process group, then write ``rc``.

    The order is the contract. The payload's stdout and stderr are handed straight to the record's own
    files at exec time, so nobody has to be reading them - including a daemon that restarts mid-run.
    ``rc`` is written only after ``waitpid`` returns, which is what makes a missing ``rc`` mean "we cannot
    know", and a present ``rc`` mean the payload is gone.
    """
    run_dir = _run_dir(state_dir, exec_id)
    meta = _read_json(run_dir / META_NAME)
    if meta is None:
        sys.stderr.write(f"recorder: no record for {exec_id}; nothing to hold\n")
        return 3
    commands = [str(item) for item in (meta.get("commands") or [])]
    if not commands:
        _atomic_write_bytes(run_dir / SPAWN_ERROR_NAME, b"record has no commands\n")
        _atomic_write_bytes(run_dir / RC_NAME, b"127\n")
        return 127
    payload_path = run_dir / PAYLOAD_NAME
    script = build_run_script(
        commands,
        status_log=run_dir / COMMANDS_RC_NAME,
        payload_umask=str(meta.get("payload_umask") or payload_umask or "0077"),
    )
    _atomic_write_bytes(payload_path, script.encode("utf-8"))
    shell = choose_shell()
    out_fd = os.open(run_dir / STDOUT_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    err_fd = os.open(run_dir / STDERR_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    _touch_stream(run_dir / COMMANDS_RC_NAME)
    try:
        proc = subprocess.Popen(  # noqa: S603 - our own argv, no shell
            [shell, str(payload_path)],
            stdin=subprocess.DEVNULL,
            stdout=out_fd,
            stderr=err_fd,
            cwd=str(meta.get("cwd") or os.getcwd()),
            env=_payload_env(meta),
            start_new_session=True,  # new session: our signals cannot reach it, and neither can a hangup
            close_fds=True,
        )
    except OSError as exc:
        os.close(out_fd)
        os.close(err_fd)
        _atomic_write_bytes(
            run_dir / SPAWN_ERROR_NAME,
            f"payload spawn failed: {type(exc).__name__}: {exc}\n".encode(),
        )
        _atomic_write_bytes(run_dir / RC_NAME, b"127\n")
        return 127
    os.close(out_fd)
    os.close(err_fd)
    _atomic_write_json(
        run_dir / PIDINFO_NAME,
        {
            "pid": proc.pid,
            "pgid": _pgid_or(proc.pid, proc.pid),
            "starttime": _read_starttime(proc.pid),
            "shell": shell,
            "started_at": _now_iso(),
            "recorder_pid": os.getpid(),
        },
    )
    try:
        _pid, status = os.waitpid(proc.pid, 0)
    except InterruptedError:
        _pid, status = os.waitpid(proc.pid, 0)
    except ChildProcessError:  # pragma: no cover - we are the parent
        _atomic_write_bytes(run_dir / SPAWN_ERROR_NAME, b"payload was not our child\n")
        _atomic_write_bytes(run_dir / RC_NAME, b"127\n")
        return 127
    _atomic_write_bytes(run_dir / RC_NAME, f"{_status_to_rc(status)}\n".encode("utf-8"))
    return _status_to_rc(status)


def _pgid_or(pid: int, fallback: int) -> int:
    try:
        return os.getpgid(pid)
    except OSError:
        return fallback


def _status_to_rc(status: int) -> int:
    """Map a ``waitpid`` status to the shell's convention: 128+signal for a signal death."""
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 128


def _payload_env(meta: dict) -> dict:
    """The environment a payload is started with: ours, plus the identity it will be recognised by.

    ``INSPIRE_EXECD_EXEC_ID`` is not decoration. It is inherited by every process the payload forks, which is
    what lets a cancel inside a shared named session attribute a process to the run that started it instead
    of signalling the whole session - see :meth:`Daemon._session_cancel_targets`. A direct run gets it too,
    where it is mainly useful to a payload that wants to log which record it belongs to.
    """
    env = dict(os.environ)
    env[EXEC_ID_ENV] = str(meta.get("exec_id") or "")
    env.pop(SESSION_ENV, None)
    if meta.get("session"):
        env[SESSION_ENV] = str(meta["session"])
    return env


def run_session_shell(state_dir: Path, session: str, *, payload_umask: str = "0077") -> int:
    """Become a named session's bash: FIFO in on fd 0, the session's two log files on 1 and 2.

    Two details make the session survive a daemon restart. fd 0 is opened ``O_RDWR``, so the shell holds a
    *writer* on its own input and never sees EOF when the daemon's write end closes; and the process is in
    its own session, so a hangup on whatever started the daemon cannot reach it. Everything the shell keeps
    - cwd, exported variables, background jobs - is therefore still there for the next run.

    The shell runs with ``--norc --noprofile`` on purpose: a user rc file that redefines the prompt or
    clobbers our bookkeeping variables would make the session's exit codes lie.
    """
    sdir = _session_dir(state_dir, session)
    _ensure_private_dir(sdir)
    _ensure_fifo(sdir / FIFO_NAME)
    shell = choose_shell()
    bash = os.path.basename(shell) == "bash"
    try:
        fifo_fd = os.open(sdir / FIFO_NAME, os.O_RDWR | os.O_NONBLOCK)
    except OSError as exc:
        sys.stderr.write(f"session shell: cannot open command FIFO: {exc}\n")
        return 3
    out_fd = os.open(sdir / STDOUT_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    err_fd = os.open(sdir / STDERR_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    for stream in (STDOUT_NAME, STDERR_NAME):
        try:
            os.chmod(sdir / stream, 0o600)
        except OSError:
            pass
    try:
        os.dup2(fifo_fd, 0)
        os.dup2(out_fd, 1)
        os.dup2(err_fd, 2)
        os.closerange(3, 1024)
        if re.fullmatch(r"0?[0-7]{3,4}", payload_umask or ""):
            os.umask(
                int(payload_umask, 8)
            )  # the session shell is the caller's shell, not our recorder
        argv = [shell] + (["--norc", "--noprofile"] if bash else [])
        os.execv(shell, argv + ["-s"])
    except OSError as exc:  # pragma: no cover - only on a pod with no shell at all
        sys.stderr.write(f"session shell: exec failed: {exc}\n")
        return 127
    return 0


# ---------------------------------------------------------------------------------------------
# rpc: the short-lived shim a pod caller actually runs
# ---------------------------------------------------------------------------------------------


def call_daemon(
    *,
    state_dir: Path,
    method: str,
    params: dict,
    socket_path: Path | None = None,
    token: str | None = None,
    timeout: float = 30.0,
    wait: float = 0.0,
) -> dict:
    """One request, one response, no state kept. Everything here is a client-side convenience."""
    path = Path(socket_path) if socket_path else socket_path_for(state_dir)
    if token is None:
        token = read_token(state_dir)
    request = {
        "protocol": PROTOCOL_VERSION,
        "method": method,
        "token": token,
        "params": params,
    }
    deadline = time.monotonic() + max(0.0, wait)
    last_error = ""
    while True:
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(timeout)
            conn.connect(str(path))
        except OSError as exc:
            last_error = str(exc)
            try:
                conn.close()
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise ExecdError(
                    "daemon-unreachable", f"cannot connect to execd at {path}: {last_error}"
                ) from exc
            time.sleep(0.05)
            continue
        try:
            conn.sendall((json.dumps(request) + "\n").encode("utf-8"))
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            raw = _read_line(conn, limit=MAX_RESPONSE_BYTES)
        finally:
            try:
                conn.close()
            except OSError:
                pass
        if raw is None:
            raise ExecdError("daemon-no-answer", f"execd at {path} returned no response line")
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ExecdError("daemon-bad-answer", f"execd answer is not JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ExecdError("daemon-bad-answer", "execd answer is not a JSON object")
        return parsed


def rpc_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.py rpc",
        description="Send one execd request over the state directory's Unix socket and print the reply.",
    )
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--socket", type=Path, default=None)
    parser.add_argument(
        "--token", default=None, help="override the token read from the state directory"
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--wait", type=float, default=0.0, help="seconds to keep trying to connect")
    parser.add_argument(
        "--json", dest="json_text", default=None, help="params object, or - for stdin"
    )
    parser.add_argument("method", help="health|run|attach|status|list|cancel|cleanup")
    args = parser.parse_args(argv)

    method = str(args.method)
    text = args.json_text
    if text == "-":
        params_text = sys.stdin.read()
    elif text is None:
        params_text = "{}"
    else:
        params_text = text
    try:
        params = json.loads(params_text) if params_text.strip() else {}
    except ValueError as exc:
        _print_response(
            response_error(ExecdError("invalid-params", f"params are not JSON: {exc}")),
            exit_for_error=True,
        )
        return 1
    if not isinstance(params, dict):
        _print_response(
            response_error(ExecdError("invalid-params", "params must be a JSON object")),
            exit_for_error=True,
        )
        return 1
    try:
        response = call_daemon(
            state_dir=args.state_dir,
            method=method,
            params=params,
            socket_path=args.socket,
            token=args.token,
            timeout=args.timeout,
            wait=args.wait,
        )
    except ExecdError as exc:  # transport-level failure: still answer with a response line
        _print_response(response_error(exc), exit_for_error=True)
        return 2
    _print_response(response, exit_for_error=not bool(response.get("ok")))
    return 0 if response.get("ok") else 1


def _print_response(response: dict, *, exit_for_error: bool) -> None:
    sys.stdout.write(json.dumps(response, sort_keys=True) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def serve_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.py serve",
        description="Run the execd daemon: one Unix socket, one state directory, all authority.",
    )
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--socket", type=Path, default=None)
    parser.add_argument("--cancel-grace-seconds", type=float, default=5.0)
    parser.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    parser.add_argument("--min-free-inodes", type=int, default=DEFAULT_MIN_FREE_INODES)
    parser.add_argument("--max-page-bytes", type=int, default=DEFAULT_MAX_PAGE_BYTES)
    parser.add_argument("--session-write-timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    daemon = Daemon(
        args.state_dir,
        socket_path=args.socket,
        cancel_grace_seconds=args.cancel_grace_seconds,
        min_free_bytes=args.min_free_bytes,
        min_free_inodes=args.min_free_inodes,
        max_page_bytes=args.max_page_bytes,
        session_write_timeout=args.session_write_timeout,
    )
    try:
        daemon.start()
    except ExecdError as exc:
        sys.stderr.write(json.dumps(response_error(exc), sort_keys=True) + "\n")
        return 3
    for received in (signal.SIGTERM, signal.SIGHUP):
        # Stop accepting new work and clean up the socket, but leave every execution and named session
        # alone: they are separate sessions precisely so that restarting the daemon is not an outage.
        try:
            signal.signal(received, lambda *_args: daemon.request_stop())
        except (ValueError, OSError):  # pragma: no cover - signal() only works in the main thread
            pass
    sys.stdout.write(
        json.dumps(
            {
                "event": "serving",
                "protocol": PROTOCOL_VERSION,
                "agent_version": AGENT_VERSION,
                # Also on the boot line, so a bootstrap that adopted a daemon it found already running can
                # check what it adopted without spending an RPC it may not be authorised to make.
                "agent_sha256": agent_sha256(),
                "pid": os.getpid(),
                "boot_id": daemon.boot_id,
                "nonce": daemon.nonce,
                "state_dir": str(daemon.state_dir),
                "socket": str(daemon.socket_path),
                "recovery": daemon.recovery,
            },
            sort_keys=True,
        )
        + "\n"
    )
    sys.stdout.flush()
    try:
        daemon.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        daemon.stop()
    return 0


def recorder_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.py recorder",
        description="Hold one execution (or one named session shell) until it is finished.",
    )
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--exec-id", default=None)
    parser.add_argument("--session", default=None)
    parser.add_argument(
        "--start-session", action="store_true", help="become the session's persistent shell"
    )
    parser.add_argument(
        "--payload-umask",
        default=None,
        help="umask to hand the payload (the daemon's own launch umask), as an octal string",
    )
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir).resolve()
    if args.start_session:
        if not args.session:
            sys.stderr.write("recorder: --start-session needs --session\n")
            return 2
        return run_session_shell(
            state_dir, _validate_session(args.session), payload_umask=args.payload_umask or "0077"
        )
    if not args.exec_id:
        sys.stderr.write("recorder: need --exec-id (or --start-session with --session)\n")
        return 2
    return run_recorder(
        state_dir, _validate_exec_id(args.exec_id), payload_umask=args.payload_umask
    )


USAGE = """usage: agent.py <command> [options]

commands:
  serve     run the execd daemon (listens on <state-dir>/daemon.sock)
  rpc       send one JSON-line request to the daemon and print one JSON-line response
  recorder  hold one execution, or be a named session's persistent shell (daemon-spawned)

run `agent.py <command> --help` for the options of each."""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE + "\n")
        return 0 if argv else 2
    if argv[0] in ("--version", "version"):
        sys.stdout.write(
            json.dumps({"protocol": PROTOCOL_VERSION, "agent_version": AGENT_VERSION}) + "\n"
        )
        return 0
    command, rest = argv[0], argv[1:]
    try:
        if command == "serve":
            return serve_main(rest)
        if command == "rpc":
            return rpc_main(rest)
        if command == "recorder":
            return recorder_main(rest)
    except ExecdError as exc:
        sys.stderr.write(json.dumps(response_error(exc), sort_keys=True) + "\n")
        return 3
    sys.stderr.write(USAGE + "\n")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
