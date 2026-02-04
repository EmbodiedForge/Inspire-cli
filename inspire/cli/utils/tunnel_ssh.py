"""SSH tunnel utilities for Bridge access via ProxyCommand.

This module keeps the historical import path stable while splitting the implementation into
smaller modules under `inspire/cli/utils/_impl/tunnel/`:
- `ssh/proxy.py`: ProxyCommand construction
- `ssh/connection.py`: connectivity checks / availability
- `ssh_exec/*`: command execution helpers
- `ssh/status.py`: status reporting
"""

from __future__ import annotations

from inspire.cli.utils.tunnel_config import load_tunnel_config  # noqa: F401
from inspire.cli.utils.tunnel_models import (  # noqa: F401
    BridgeNotFoundError,
    BridgeProfile,
    TunnelConfig,
    TunnelError,
    TunnelNotAvailableError,
)
from inspire.cli.utils.tunnel_rtunnel import _ensure_rtunnel_binary  # noqa: F401
from inspire.cli.utils._impl.tunnel.ssh.connection import (  # noqa: F401
    _test_ssh_connection,
    is_tunnel_available,
)
from inspire.cli.utils.tunnel_ssh_exec import (  # noqa: F401
    get_ssh_command_args,
    run_ssh_command,
    run_ssh_command_streaming,
)
from inspire.cli.utils._impl.tunnel.ssh.proxy import _get_proxy_command  # noqa: F401
from inspire.cli.utils._impl.tunnel.ssh.status import get_tunnel_status  # noqa: F401
