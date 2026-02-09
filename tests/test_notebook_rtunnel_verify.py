"""Tests for notebook rtunnel verification helpers."""

from __future__ import annotations

import pytest

from inspire.platform.web.browser_api.rtunnel import (
    _is_rtunnel_proxy_ready,
)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, "SSH-2.0-OpenSSH_9.0", True),
        (200, "", True),
        (500, "upstream error", False),
        (200, "ECONNREFUSED", False),
        (200, "404 page not found", False),
        (200, "<html><title>Jupyter Server</title></html>", False),
    ],
)
def test_is_rtunnel_proxy_ready(status: int, body: str, expected: bool) -> None:
    assert _is_rtunnel_proxy_ready(status=status, body=body) is expected
