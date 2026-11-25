"""Paramiko-based SSH connection pool for persistent bridge connections.

This module provides a connection pool to eliminate SSH handshake overhead
when running multiple commands via `inspire bridge exec`.

Key design decisions:
- Thread-safe connection pool with per-bridge connection management
- Automatic connection reuse for subsequent commands
- Idle timeout to close unused connections
- Transparent fallback to subprocess-based SSH if paramiko fails
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import paramiko

    from .models import BridgeProfile, TunnelConfig

logger = logging.getLogger(__name__)

# Connection pool settings
DEFAULT_IDLE_TIMEOUT = 300  # Close idle connections after 5 minutes
DEFAULT_MAX_CONNECTIONS = 10  # Max connections per bridge
DEFAULT_CONNECT_TIMEOUT = 30  # SSH connect timeout


@dataclass
class PooledConnection:
    """A pooled SSH connection with metadata."""

    client: "paramiko.SSHClient"
    bridge_name: str
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    use_count: int = 0
    is_active: bool = True
    in_use: int = 0

    def mark_used(self) -> None:
        """Mark connection as recently used."""
        self.last_used = time.time()
        self.use_count += 1

    def acquire(self) -> None:
        """Record that a caller has taken this connection out of the pool."""
        self.in_use += 1

    def release(self) -> None:
        """Return a checked-out connection and stamp it as recently used."""
        self.in_use = max(self.in_use - 1, 0)
        self.last_used = time.time()

    def is_idle(self, timeout: float) -> bool:
        """Check if connection has been idle for longer than timeout.

        A connection that is still checked out by a caller is never idle, even
        if ``last_used`` is old — the caller's command may still be running.
        """
        if self.in_use:
            return False
        return time.time() - self.last_used > timeout


class ConnectionPool:
    """Thread-safe SSH connection pool for bridge connections.

    Usage:
        pool = ConnectionPool()
        with pool.get_connection(bridge, config) as conn:
            stdin, stdout, stderr = conn.exec_command("ls -la")
    """

    def __init__(
        self,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
    ):
        self.idle_timeout = idle_timeout
        self.max_connections = max_connections
        self._pools: dict[str, list[PooledConnection]] = {}
        self._lock = threading.RLock()
        self._cleanup_thread: Optional[threading.Thread] = None
        self._shutdown = False

    def _ensure_cleanup_thread(self) -> None:
        """Start background cleanup thread if not running."""
        if self._cleanup_thread is None or not self._cleanup_thread.is_alive():
            self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
            self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        """Background thread to close idle connections."""
        while not self._shutdown:
            time.sleep(30)  # Check every 30 seconds
            self._close_idle_connections()

    def _close_idle_connections(self) -> None:
        """Close connections that have been idle too long.

        Connections currently checked out by a caller are never closed.
        """
        with self._lock:
            for bridge_name, connections in list(self._pools.items()):
                to_keep: list[PooledConnection] = []
                for conn in connections:
                    if conn.in_use:
                        to_keep.append(conn)
                        continue
                    if conn.is_idle(self.idle_timeout) or not conn.is_active:
                        try:
                            conn.client.close()
                            logger.debug("Closed idle connection for bridge %s", bridge_name)
                        except Exception:
                            pass
                    else:
                        to_keep.append(conn)
                if to_keep:
                    self._pools[bridge_name] = to_keep
                else:
                    del self._pools[bridge_name]

    def _get_proxy_command(
        self, bridge: "BridgeProfile", config: "TunnelConfig"
    ) -> "paramiko.ProxyCommand":
        """Create a paramiko ProxyCommand for tunneling through rtunnel.

        This uses paramiko's built-in ProxyCommand support to tunnel SSH
        through the rtunnel websocket proxy.
        """
        import paramiko
        from .rtunnel import _ensure_rtunnel_binary
        from .ssh import _get_proxy_command

        _ensure_rtunnel_binary(config)

        # Get the proxy command string (e.g., "sh -c 'rtunnel ... stdio://%h:%p 2>/dev/null'")
        proxy_cmd_str = _get_proxy_command(bridge, config.rtunnel_bin, quiet=True)

        # Replace %h:%p with actual host:port for paramiko
        proxy_cmd_str = proxy_cmd_str.replace("%h", "localhost").replace("%p", str(bridge.ssh_port))

        logger.debug("Using ProxyCommand: %s", proxy_cmd_str)

        return paramiko.ProxyCommand(proxy_cmd_str)

    def _create_connection(
        self, bridge: "BridgeProfile", config: "TunnelConfig"
    ) -> PooledConnection:
        """Create a new SSH connection via paramiko."""
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Get the proxy command (rtunnel wrapper)
        proxy_cmd = self._get_proxy_command(bridge, config)

        try:
            # Connect using the proxy command
            connect_kwargs: dict = {
                "hostname": bridge.name,  # Used for logging only with ProxyCommand
                "port": bridge.ssh_port,
                "username": bridge.ssh_user,
                "timeout": DEFAULT_CONNECT_TIMEOUT,
                "sock": proxy_cmd,
            }

            if bridge.identity_file:
                connect_kwargs["key_filename"] = bridge.identity_file

            client.connect(**connect_kwargs)

            logger.debug("Created new SSH connection for bridge %s", bridge.name)

            return PooledConnection(client=client, bridge_name=bridge.name)

        except Exception:
            proxy_cmd.close()
            raise

    def _get_or_create_connection(
        self, bridge: "BridgeProfile", config: "TunnelConfig"
    ) -> PooledConnection:
        """Get an existing connection or create a new one."""
        bridge_name = bridge.name

        with self._lock:
            self._ensure_cleanup_thread()

            # Try to reuse an existing connection
            if bridge_name in self._pools:
                for conn in self._pools[bridge_name]:
                    if conn.is_active:
                        transport = conn.client.get_transport()
                        if transport and transport.is_active():
                            conn.mark_used()
                            logger.debug("Reusing connection for bridge %s", bridge_name)
                            return conn
                        else:
                            conn.is_active = False

            # Create new connection
            if bridge_name in self._pools and len(self._pools[bridge_name]) >= self.max_connections:
                raise RuntimeError(
                    f"Max connections ({self.max_connections}) reached for bridge {bridge_name}"
                )

            conn = self._create_connection(bridge, config)

            if bridge_name not in self._pools:
                self._pools[bridge_name] = []
            self._pools[bridge_name].append(conn)

            return conn

    @contextmanager
    def get_connection(self, bridge: "BridgeProfile", config: "TunnelConfig"):
        """Get a connection from the pool (context manager).

        The connection is marked in-use for the duration of the block, so the
        idle reaper cannot close it while a command is running, and its
        ``last_used`` timestamp is refreshed when the block exits.

        Yields:
            paramiko.SSHClient: An active SSH client connection
        """
        conn = None
        try:
            conn = self._get_or_create_connection(bridge, config)
            with self._lock:
                conn.acquire()
            yield conn.client
        except Exception:
            # Mark connection as failed
            if conn:
                conn.is_active = False
                try:
                    conn.client.close()
                except Exception:
                    pass
            raise
        finally:
            if conn:
                with self._lock:
                    conn.release()

    def close_all(self) -> None:
        """Close all connections in the pool."""
        self._shutdown = True
        with self._lock:
            for bridge_name, connections in self._pools.items():
                for conn in connections:
                    try:
                        conn.client.close()
                        logger.debug("Closed connection for bridge %s", bridge_name)
                    except Exception:
                        pass
            self._pools.clear()

    def get_stats(self) -> dict:
        """Get pool statistics."""
        with self._lock:
            return {
                bridge_name: {
                    "total": len(connections),
                    "active": sum(1 for c in connections if c.is_active),
                    "total_uses": sum(c.use_count for c in connections),
                }
                for bridge_name, connections in self._pools.items()
            }


# Global connection pool instance
_global_pool: Optional[ConnectionPool] = None
_pool_lock = threading.Lock()


def get_connection_pool() -> ConnectionPool:
    """Get the global connection pool instance."""
    global _global_pool
    if _global_pool is None:
        with _pool_lock:
            if _global_pool is None:
                _global_pool = ConnectionPool()
    return _global_pool


def close_connection_pool() -> None:
    """Close the global connection pool."""
    global _global_pool
    if _global_pool is not None:
        _global_pool.close_all()
        _global_pool = None


def is_paramiko_available() -> bool:
    """Check if paramiko is available."""
    try:
        import paramiko  # noqa: F401

        return True
    except ImportError:
        return False


__all__ = [
    "ConnectionPool",
    "PooledConnection",
    "get_connection_pool",
    "close_connection_pool",
    "is_paramiko_available",
    "DEFAULT_IDLE_TIMEOUT",
    "DEFAULT_MAX_CONNECTIONS",
]
