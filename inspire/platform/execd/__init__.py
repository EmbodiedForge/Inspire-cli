"""Resumable remote execution through a pod-side execd."""

from inspire.platform.execd.protocol import AGENT_VERSION, PROTOCOL_VERSION, ExecdError, new_exec_id

__all__ = ["AGENT_VERSION", "PROTOCOL_VERSION", "ExecdError", "new_exec_id"]
