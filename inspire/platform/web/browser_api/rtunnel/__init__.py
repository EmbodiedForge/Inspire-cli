"""Notebook rtunnel setup: commands, state, probe, verify, and flow.

Split into submodules for maintainability.  The public entry point is
``setup_notebook_rtunnel`` (async-safe wrapper around the sync flow).
"""

from __future__ import annotations

# -- Permanent public re-exports ---------------------------------------------

from .commands import BOOTSTRAP_SENTINEL as BOOTSTRAP_SENTINEL  # noqa: F401
from .commands import SETUP_DONE_MARKER as SETUP_DONE_MARKER  # noqa: F401
from .commands import SSHD_MISSING_MARKER as SSHD_MISSING_MARKER  # noqa: F401
from .commands import SSH_SERVER_MISSING_MARKER as SSH_SERVER_MISSING_MARKER  # noqa: F401
from .commands import RtunnelSetupPlan as RtunnelSetupPlan  # noqa: F401
from .commands import build_rtunnel_setup_commands as build_rtunnel_setup_commands  # noqa: F401
from .commands import describe_rtunnel_setup_plan as describe_rtunnel_setup_plan  # noqa: F401
from .commands import resolve_rtunnel_setup_plan as resolve_rtunnel_setup_plan  # noqa: F401
from .flow import setup_notebook_rtunnel as setup_notebook_rtunnel  # noqa: F401
from .verify import redact_proxy_url as redact_proxy_url  # noqa: F401

# -- Re-exports for production code ------------------------------------------
# playwright_notebooks.py lazy-imports these from the rtunnel package:
from .terminal import (  # noqa: F401
    _build_terminal_websocket_url,
    _create_terminal_via_api,
    _delete_terminal_via_api,
    _focus_terminal_input,
    _open_or_create_terminal,
    _send_terminal_command_via_websocket,
)

# -- Re-exports for direct test imports --------------------------------------
# Tests do ``from inspire.platform.web.browser_api.rtunnel import <sym>``
# and call these directly (not via monkeypatch).

from .state import (
    get_cached_rtunnel_proxy_candidates as get_cached_rtunnel_proxy_candidates,
)  # noqa: F401, E501
from .state import save_rtunnel_proxy_state as save_rtunnel_proxy_state  # noqa: F401
from .verify import _is_rtunnel_proxy_ready as _is_rtunnel_proxy_ready  # noqa: F401
from .verify import wait_for_rtunnel_reachable as wait_for_rtunnel_reachable  # noqa: F401
from ._jupyter import _build_jupyter_xsrf_headers as _build_jupyter_xsrf_headers  # noqa: F401
from ._jupyter import _extract_jupyter_token as _extract_jupyter_token  # noqa: F401
from ._jupyter import _jupyter_server_base as _jupyter_server_base  # noqa: F401
from .probe import (
    probe_existing_rtunnel_proxy_url as probe_existing_rtunnel_proxy_url,
)  # noqa: F401, E501
from .terminal import _build_batch_setup_script as _build_batch_setup_script  # noqa: F401
from .terminal import (
    _send_setup_command_via_terminal_ws as _send_setup_command_via_terminal_ws,
)  # noqa: F401, E501
from .terminal import _verify_terminal_focus as _verify_terminal_focus  # noqa: F401
from .terminal import _wait_for_terminal_surface as _wait_for_terminal_surface  # noqa: F401
from .terminal import (
    _wait_for_terminal_surface_progressive as _wait_for_terminal_surface_progressive,
)  # noqa: F401, E501
from .terminal import _attach_ws_output_listener as _attach_ws_output_listener  # noqa: F401
from .terminal import _detach_ws_output_listener as _detach_ws_output_listener  # noqa: F401
from .terminal import _log_ws_diagnostics as _log_ws_diagnostics  # noqa: F401
from .terminal import _poll_ws_capture as _poll_ws_capture  # noqa: F401
from .terminal import _wait_for_ws_capture as _wait_for_ws_capture  # noqa: F401
from .upload import _CONTENTS_API_RTUNNEL_FILENAME as _CONTENTS_API_RTUNNEL_FILENAME  # noqa: F401
from .upload import _compute_rtunnel_hash as _compute_rtunnel_hash  # noqa: F401
from .upload import _download_rtunnel_locally as _download_rtunnel_locally  # noqa: F401
from .upload import _resolve_rtunnel_binary as _resolve_rtunnel_binary  # noqa: F401
from .upload import _rtunnel_matches_on_notebook as _rtunnel_matches_on_notebook  # noqa: F401
from .upload import _upload_rtunnel_hash_sidecar as _upload_rtunnel_hash_sidecar  # noqa: F401
from .upload import (
    _upload_rtunnel_via_contents_api as _upload_rtunnel_via_contents_api,
)  # noqa: F401, E501
from .flow import _StepTimer as _StepTimer  # noqa: F401
from .flow import _wait_for_setup_completion as _wait_for_setup_completion  # noqa: F401

# PlaywrightError re-export: used by test stubs that raise it.
try:
    from playwright.sync_api import Error as PlaywrightError  # noqa: F401
except ImportError:  # pragma: no cover

    class PlaywrightError(Exception):  # type: ignore[no-redef]
        pass


__all__ = ["setup_notebook_rtunnel"]
