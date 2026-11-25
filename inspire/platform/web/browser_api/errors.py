"""Shared browser API error handling."""

from __future__ import annotations

from typing import Any


def ensure_browser_api_success(payload: dict[str, Any], *, action: str) -> None:
    """Raise with the backend code/message when a browser API envelope fails."""
    if payload.get("code") == 0:
        return

    code = payload.get("code")
    message = payload.get("message") or payload.get("msg") or "unknown error"
    if code is None:
        raise ValueError(f"{action} failed: {message}")
    raise ValueError(f"{action} failed: API error {code}: {message}")


def get_browser_api_action_result(payload: dict[str, Any], *, action: str) -> dict[str, Any]:
    """Return the Result object from a v2 Action response."""
    metadata = payload.get("ResponseMetadata")
    if isinstance(metadata, dict):
        error = metadata.get("Error")
        if isinstance(error, dict):
            code = error.get("Code") or "Error"
            message = error.get("Message") or "unknown error"
            raise ValueError(f"{action} failed: {code}: {message}")

    result = payload.get("Result", payload.get("result"))
    if result is None:
        if "code" in payload:
            ensure_browser_api_success(payload, action=action)
        return {}
    if not isinstance(result, dict):
        raise ValueError(f"{action} failed: platform Result is not an object")
    return result


__all__ = ["ensure_browser_api_success", "get_browser_api_action_result"]
