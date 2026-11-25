"""Short-lived local broker that lets separate CLI calls share one platform PTY."""

from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

from websocket import WebSocketConnectionClosedException

_BROKER_PROTOCOL = 1
_IDLE_SECONDS = 60.0
_START_TIMEOUT_SECONDS = 15.0
_MAX_MESSAGE_BYTES = 4 * 1024 * 1024


def _root() -> Path:
    path = Path(tempfile.gettempdir()) / f"inspire-cli-{os.getuid()}" / "exec-sessions"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _encoded_target(target: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(target, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def _paths(target: dict[str, Any]) -> tuple[Path, Path]:
    digest = hashlib.sha256(_encoded_target(target).encode("ascii")).hexdigest()[:32]
    return _root() / f"v{_BROKER_PROTOCOL}-{digest}.sock", _root() / f"{digest}.lock"


def _receive(stream: Any) -> dict[str, Any]:
    line = stream.readline(_MAX_MESSAGE_BYTES + 1)
    if not line or len(line) > _MAX_MESSAGE_BYTES:
        raise OSError("local exec session broker returned an invalid response")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise OSError("local exec session broker returned a non-object response")
    return value


def _request(
    socket_path: Path, message: dict[str, Any], *, connect_timeout: float | None = None
) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        if connect_timeout is not None:
            connection.settimeout(connect_timeout)
        connection.connect(str(socket_path))
        if connect_timeout is not None:
            connection.settimeout(None)
        stream = connection.makefile("rwb")
        stream.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
        stream.flush()
        response = _receive(stream)
    if response.get("protocol") != _BROKER_PROTOCOL:
        raise OSError("local exec session broker protocol mismatch")
    return response


def _ping(socket_path: Path, *, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        return _request(
            socket_path, {"protocol": _BROKER_PROTOCOL, "op": "ping"}, connect_timeout=timeout
        )
    except (OSError, ValueError):
        return None


class SharedJobSession:
    """A PlatformWsSession-compatible proxy owned by a per-pod local broker."""

    def __init__(self, target: dict[str, Any]) -> None:
        self._target = target
        self._socket_path, self._lock_path = _paths(target)
        self.instance_record: dict[str, Any] | None = None
        self._start()

    def _start(self) -> None:
        _root()
        with self._lock_path.open("a+") as lock:
            os.chmod(self._lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(0.2)
                    connection.connect(str(self._socket_path))
                # A broker in a long RPC cannot answer a ping, but its listening socket proves it is alive.
                return
            except (ConnectionRefusedError, FileNotFoundError):
                try:
                    self._socket_path.unlink()
                except FileNotFoundError:
                    pass
            except socket.timeout:
                return

            subprocess.Popen(  # noqa: S603 - fixed interpreter/module, never a shell
                [
                    sys.executable,
                    "-m",
                    "inspire.platform.execd.session_broker",
                    "--socket",
                    str(self._socket_path),
                    "--target",
                    _encoded_target(self._target),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            deadline = time.monotonic() + _START_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                response = _ping(self._socket_path, timeout=0.2)
                if response is not None:
                    return
                time.sleep(0.05)
            raise OSError("local exec session broker did not start")

    def run(
        self, command: str, timeout: float | None = None, *, max_bytes: int | None = None
    ) -> dict[str, Any]:
        self._start()
        message: dict[str, Any] = {
            "protocol": _BROKER_PROTOCOL,
            "op": "run",
            "command": command,
            "timeout": timeout,
        }
        if max_bytes is not None:
            message["max_bytes"] = max_bytes
        response = _request(self._socket_path, message)
        record = response.get("instance_record")
        if isinstance(record, dict):
            self.instance_record = record
        if response.get("ok") is not True:
            raise WebSocketConnectionClosedException(
                str(response.get("error") or "local exec session broker transport failure")
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise OSError("local exec session broker returned an invalid command result")
        return result

    def close(self) -> None:
        # Releasing one CLI's proxy must not close the PTY other CLI calls can reuse.
        return


def _serve(socket_path: Path, target: dict[str, Any]) -> None:
    from inspire.platform.web.browser_api import open_job_command_session
    from inspire.platform.web.session import get_web_session

    target = dict(target)
    account_alias = target.pop("account_alias", None)
    web_session = get_web_session(account=account_alias) if account_alias else None
    if web_session is not None:
        target["session"] = web_session
    session: Any | None = None
    socket_identity: int | None = None
    last_active = time.monotonic()
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        socket_identity = socket_path.stat().st_ino
        server.listen(16)
        while time.monotonic() - last_active < _IDLE_SECONDS:
            readable, _writable, _error = select.select([server], [], [], 1.0)
            if not readable:
                continue
            connection, _address = server.accept()
            with connection:
                stream = connection.makefile("rwb")
                try:
                    request = _receive(stream)
                    if request.get("protocol") != _BROKER_PROTOCOL:
                        raise OSError("broker protocol mismatch")
                    operation = request.get("op")
                    if operation == "ping":
                        response: dict[str, Any] = {
                            "protocol": _BROKER_PROTOCOL,
                            "pid": os.getpid(),
                        }
                    elif operation == "run":
                        with_session = session
                        if with_session is None:
                            with_session = session = open_job_command_session(**target)
                        try:
                            result = with_session.run(
                                str(request["command"]),
                                request.get("timeout"),
                                max_bytes=request.get("max_bytes"),
                            )
                        except Exception:
                            try:
                                with_session.close()
                            finally:
                                session = None
                            raise
                        response = {
                            "protocol": _BROKER_PROTOCOL,
                            "ok": True,
                            "result": result,
                            "instance_record": getattr(with_session, "instance_record", None),
                        }
                    else:
                        raise OSError("unknown broker operation")
                except Exception as exc:  # returned to the client, never written to a log
                    response = {
                        "protocol": _BROKER_PROTOCOL,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                try:
                    stream.write(
                        json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
                    )
                    stream.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # A lifetime probe or a killed CLI is not a reason to kill the shared PTY.
                    pass
                last_active = time.monotonic()
    finally:
        server.close()
        if session is not None:
            session.close()
        try:
            if socket_identity is not None and socket_path.stat().st_ino == socket_identity:
                socket_path.unlink()
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    target = json.loads(base64.urlsafe_b64decode(args.target).decode("utf-8"))
    _serve(Path(args.socket), target)


if __name__ == "__main__":
    main()
