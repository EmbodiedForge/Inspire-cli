"""SSH ProxyCommand construction helpers."""

from __future__ import annotations

from pathlib import Path

from .tunnel_models import BridgeProfile


def _get_proxy_command(bridge: BridgeProfile, rtunnel_bin: Path, quiet: bool = False) -> str:
    """Build the ProxyCommand string for SSH.

    Args:
        bridge: Bridge profile with proxy_url
        rtunnel_bin: Path to rtunnel binary
        quiet: If True, suppress rtunnel stderr output (startup/shutdown messages)

    Returns:
        ProxyCommand string for SSH -o option
    """
    import shlex

    # Convert https:// URL to wss:// for websocket
    proxy_url = bridge.proxy_url
    if proxy_url.startswith("https://"):
        ws_url = "wss://" + proxy_url[8:]
    elif proxy_url.startswith("http://"):
        ws_url = "ws://" + proxy_url[7:]
    else:
        ws_url = proxy_url

    # ProxyCommand is executed by a shell on the client; quote the URL because it
    # can contain characters like '?' (e.g. token query params) that some shells
    # treat as glob patterns.
    if quiet:
        # Wrap in sh -c to redirect stderr, suppressing rtunnel's verbose output
        cmd = f"{rtunnel_bin} {shlex.quote(ws_url)} stdio://%h:%p 2>/dev/null"
        return f"sh -c {shlex.quote(cmd)}"
    return f"{shlex.quote(str(rtunnel_bin))} {shlex.quote(ws_url)} {shlex.quote('stdio://%h:%p')}"
