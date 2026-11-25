"""Versioned contract shared by the local execd client and its CLI consumers."""

from __future__ import annotations

import re
import uuid
from typing import Any

PROTOCOL_VERSION = 2
AGENT_VERSION = "2.1.0"
EXEC_ID_RE = re.compile(r"^ex-[0-9a-f]{32}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "outcome_uncertain"})
STATES = frozenset({"accepted", "running", *TERMINAL_STATES})


class ExecdError(RuntimeError):
    """An execd operation failed with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


def new_exec_id() -> str:
    return f"ex-{uuid.uuid4().hex}"


def validate_exec_id(value: Any) -> str:
    text = str(value or "")
    if not EXEC_ID_RE.fullmatch(text):
        raise ValueError("exec_id must have the form ex-<32 lowercase hex characters>")
    return text


def validate_session(value: Any) -> str:
    text = str(value or "")
    if not SESSION_RE.fullmatch(text):
        raise ValueError("session must be 1-64 letters, digits, dots, underscores or dashes")
    return text


def response_ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"protocol": PROTOCOL_VERSION, "ok": True, "data": data}


def response_error(exc: ExecdError) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "ok": False,
        "error": {"code": exc.code, "message": str(exc), "data": exc.data},
    }


def unwrap_response(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("protocol") != PROTOCOL_VERSION:
        raise ExecdError("protocol-mismatch", "execd response uses an unsupported protocol")
    if response.get("ok") is True and isinstance(response.get("data"), dict):
        return dict(response["data"])
    error = response.get("error")
    if isinstance(error, dict):
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        raise ExecdError(
            str(error.get("code") or "agent-error"),
            str(error.get("message") or "execd operation failed"),
            data=data,
        )
    raise ExecdError("invalid-response", "execd returned an invalid response")


__all__ = [
    "AGENT_VERSION",
    "EXEC_ID_RE",
    "PROTOCOL_VERSION",
    "SESSION_RE",
    "STATES",
    "TERMINAL_STATES",
    "ExecdError",
    "new_exec_id",
    "response_error",
    "response_ok",
    "unwrap_response",
    "validate_exec_id",
    "validate_session",
]
