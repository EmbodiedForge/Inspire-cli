"""Notebook SSH and rtunnel setup flow."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Optional
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import click

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_CONFIG_ERROR
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import get_base_url, load_config, require_web_session
from inspire.cli.utils.tunnel_reconnect import (
    NotebookBridgeReconnectState,
    NotebookBridgeReconnectStatus,
    attempt_notebook_bridge_rebuild,
    load_ssh_public_key_material,
    rebuild_notebook_bridge_profile,
    retry_pause_seconds,
    should_attempt_ssh_reconnect,
)
from inspire.config import ConfigError
from inspire.config.ssh_runtime import resolve_ssh_runtime_config
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import NotebookFailedError
from inspire.platform.web.browser_api.rtunnel import redact_proxy_url

from .notebook_lookup import (
    _get_current_user_detail,
    _resolve_notebook_id,
    _validate_notebook_account_access,
)


def _format_proxy_http_body(raw: bytes) -> str:
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace")
    compact = " ".join(text.split())
    return compact[:180]


def _describe_proxy_http_status(proxy_url: str, timeout_s: float = 4.0) -> str:
    parsed = urllib_parse.urlsplit(proxy_url)
    if parsed.scheme not in {"http", "https"}:
        return "n/a (non-http proxy URL)"

    request = urllib_request.Request(proxy_url, method="GET")
    try:
        with urllib_request.urlopen(request, timeout=timeout_s) as response:
            body = _format_proxy_http_body(response.read(220))
            return f"{response.status} {body}".strip()
    except urllib_error.HTTPError as error:
        try:
            body = _format_proxy_http_body(error.read(220))
        except Exception:
            body = ""
        return f"{error.code} {body}".strip()
    except Exception as error:
        return str(error)


def load_ssh_public_key(pubkey_path: Optional[str] = None) -> str:
    return load_ssh_public_key_material(pubkey_path)


def _run_interactive_notebook_ssh_with_reconnect(
    ctx: Context,
    *,
    profile_name: str,
    tunnel_account: Optional[str],
    session,
    pubkey: Optional[str],
    rtunnel_bin: Optional[str],
    debug_playwright: bool,
    setup_timeout: int,
    tunnel_retries: int,
    tunnel_retry_pause: float,
) -> None:
    from inspire.bridge.tunnel import (
        get_ssh_command_args,
        is_tunnel_available,
        load_tunnel_config,
    )

    reconnect_limit = max(0, int(tunnel_retries))
    reconnect_state = NotebookBridgeReconnectState(
        reconnect_limit=reconnect_limit,
        reconnect_pause=tunnel_retry_pause,
    )

    def _runtime_loader() -> object:
        return resolve_ssh_runtime_config(
            cli_overrides={"rtunnel_bin": rtunnel_bin},
        )

    def _runtime_validator(runtime: object) -> None:
        del runtime
        pass

    while True:
        tunnel_config = load_tunnel_config(account=tunnel_account)
        bridge = tunnel_config.get_bridge(profile_name)
        if bridge is None:
            _handle_error(
                ctx,
                "ConfigError",
                f"Bridge profile '{profile_name}' not found.",
                EXIT_CONFIG_ERROR,
                hint="Run 'inspire tunnel list' to check saved bridge profiles.",
            )
            return

        args = get_ssh_command_args(bridge_name=profile_name, config=tunnel_config)
        try:
            returncode = subprocess.call(args)
        except KeyboardInterrupt:
            raise SystemExit(130) from None

        if returncode == 0:
            return
        if not should_attempt_ssh_reconnect(returncode, interactive=True):
            raise SystemExit(returncode if returncode is not None else 1)
        if reconnect_state.reconnect_attempt >= reconnect_limit:
            _handle_error(
                ctx,
                "APIError",
                "SSH connection dropped and auto-reconnect retries were exhausted.",
                EXIT_API_ERROR,
                hint="Re-run 'inspire notebook ssh <notebook-id>' to refresh the tunnel.",
            )
            return

        attempt = reconnect_state.reconnect_attempt + 1
        click.echo(
            (
                "SSH connection dropped; rebuilding tunnel automatically "
                f"(attempt {attempt}/{reconnect_limit})..."
            ),
            err=True,
        )

        reconnect_result = attempt_notebook_bridge_rebuild(
            state=reconnect_state,
            bridge_name=profile_name,
            bridge=bridge,
            tunnel_config=tunnel_config,
            session_loader=lambda: session,
            runtime_loader=_runtime_loader,
            rebuild_fn=rebuild_notebook_bridge_profile,
            key_loader=lambda path: load_ssh_public_key(path),
            runtime_validator=_runtime_validator,
            pubkey_path=pubkey,
            timeout=setup_timeout,
            headless=not debug_playwright,
        )

        if isinstance(reconnect_result.error, (ValueError, ConfigError)):
            hint = None
            if "setup_script" in str(reconnect_result.error):
                hint = (
                    "Set [ssh].setup_script in config.toml or export INSPIRE_SETUP_SCRIPT "
                    "to the setup script path on the cluster."
                )
            _handle_error(
                ctx,
                "ConfigError",
                str(reconnect_result.error),
                EXIT_CONFIG_ERROR,
                hint=hint,
            )
            return

        if reconnect_result.status is NotebookBridgeReconnectStatus.RETRY_LATER:
            if reconnect_result.pause_seconds > 0:
                time.sleep(reconnect_result.pause_seconds)
            continue

        if reconnect_result.status is NotebookBridgeReconnectStatus.NOT_REBUILDABLE:
            _handle_error(
                ctx,
                "ConfigError",
                f"Bridge profile '{profile_name}' is missing notebook metadata.",
                EXIT_CONFIG_ERROR,
                hint="Re-run 'inspire notebook ssh <notebook-id> --save-as <name>'.",
            )
            return

        if reconnect_result.status is NotebookBridgeReconnectStatus.EXHAUSTED:
            if reconnect_result.error is not None:
                _handle_error(
                    ctx,
                    "APIError",
                    f"Failed to rebuild notebook tunnel after disconnect: {reconnect_result.error}",
                    EXIT_API_ERROR,
                )
                return
            _handle_error(
                ctx,
                "APIError",
                "SSH connection dropped and auto-reconnect retries were exhausted.",
                EXIT_API_ERROR,
                hint="Re-run 'inspire notebook ssh <notebook-id>' to refresh the tunnel.",
            )
            return

        refreshed_config = load_tunnel_config(account=tunnel_account)
        if is_tunnel_available(
            bridge_name=profile_name,
            config=refreshed_config,
            retries=3,
            retry_pause=1.0,
        ):
            continue
        if reconnect_state.reconnect_attempt >= reconnect_limit:
            _handle_error(
                ctx,
                "APIError",
                "Tunnel rebuild completed, but SSH preflight still failed.",
                EXIT_API_ERROR,
                hint=f"Run 'inspire tunnel test -b {profile_name}' for diagnostics.",
            )
            return

        pause_s = retry_pause_seconds(
            reconnect_state.reconnect_attempt,
            base_pause=tunnel_retry_pause,
            progressive=True,
        )
        if pause_s > 0:
            time.sleep(pause_s)


def run_notebook_ssh(
    ctx: Context,
    *,
    notebook_id: str,
    wait: bool,
    pubkey: Optional[str],
    save_as: Optional[str],
    port: int,
    ssh_port: int,
    command: Optional[str],
    rtunnel_bin: Optional[str],
    debug_playwright: bool,
    setup_timeout: int,
) -> None:
    from inspire.bridge.tunnel import (
        BridgeProfile,
        get_ssh_command_args,
        has_internet_for_gpu_type,
        is_tunnel_available,
        load_tunnel_config,
        save_tunnel_config,
    )

    session = require_web_session(
        ctx,
        hint=(
            "Notebook SSH requires web authentication. "
            "Set [auth].username and configure password via INSPIRE_PASSWORD "
            'or [accounts."<username>"].password.'
        ),
    )

    base_url = get_base_url()
    config = load_config(ctx)
    notebook_id, _ = _resolve_notebook_id(
        ctx,
        session=session,
        config=config,
        base_url=base_url,
        identifier=notebook_id,
        json_output=False,
    )

    try:
        if wait:
            notebook_detail = browser_api_module.wait_for_notebook_running(
                notebook_id=notebook_id, session=session
            )
        else:
            notebook_detail = browser_api_module.get_notebook_detail(
                notebook_id=notebook_id, session=session
            )
    except NotebookFailedError as e:
        _handle_error(
            ctx,
            "NotebookFailed",
            f"Notebook failed to start: {e}",
            EXIT_API_ERROR,
            hint=e.events or "Check Events tab in web UI for details.",
        )
        return
    except TimeoutError as e:
        _handle_error(
            ctx,
            "Timeout",
            f"Timed out waiting for notebook to reach RUNNING: {e}",
            EXIT_API_ERROR,
        )
        return
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    current_user_detail: dict = {}
    try:
        current_user_detail = _get_current_user_detail(session, base_url=base_url)
    except Exception:
        current_user_detail = {}

    allowed, reason = _validate_notebook_account_access(
        current_user=current_user_detail,
        notebook_detail=notebook_detail,
    )
    if not allowed:
        configured_user = str(getattr(config, "username", "") or "").strip()
        user_label = configured_user or "current account"
        _handle_error(
            ctx,
            "ConfigError",
            f"Notebook/account mismatch detected before tunnel setup: {reason}.",
            EXIT_CONFIG_ERROR,
            hint=(
                f"Notebook '{notebook_id}' appears to belong to another account. "
                f"Switch [auth].username for this project (current: {user_label}) and ensure a "
                "matching password is available via INSPIRE_PASSWORD or global "
                '[accounts."<username>"].password.'
            ),
        )
        return

    gpu_info = (notebook_detail.get("resource_spec_price") or {}).get("gpu_info") or {}
    gpu_type = gpu_info.get("gpu_product_simple", "")
    has_internet = has_internet_for_gpu_type(gpu_type)

    tunnel_account = str(getattr(config, "username", "") or "").strip() or None
    profile_name = save_as or f"notebook-{notebook_id[:8]}"
    cached_config = load_tunnel_config(account=tunnel_account)

    if profile_name in cached_config.bridges:
        cached_bridge = cached_config.bridges[profile_name]
        cached_notebook_id = str(getattr(cached_bridge, "notebook_id", "") or "").strip()
        if cached_notebook_id == notebook_id:
            test_args = get_ssh_command_args(
                bridge_name=profile_name,
                config=cached_config,
                remote_command="echo ok",
            )
            try:
                result = subprocess.run(
                    test_args,
                    capture_output=True,
                    timeout=10,
                    text=True,
                )
                if result.returncode == 0 and "ok" in result.stdout:
                    click.echo("Using cached tunnel connection (fast path).", err=True)
                    if command is None:
                        _run_interactive_notebook_ssh_with_reconnect(
                            ctx,
                            profile_name=profile_name,
                            tunnel_account=tunnel_account,
                            session=session,
                            pubkey=pubkey,
                            rtunnel_bin=rtunnel_bin,
                            debug_playwright=debug_playwright,
                            setup_timeout=setup_timeout,
                            tunnel_retries=config.tunnel_retries,
                            tunnel_retry_pause=config.tunnel_retry_pause,
                        )
                        return
                    args = get_ssh_command_args(
                        bridge_name=profile_name,
                        config=cached_config,
                        remote_command=command,
                    )
                    os.execvp("ssh", args)
                    return
            except (subprocess.TimeoutExpired, Exception):
                pass
        else:
            if cached_notebook_id:
                click.echo(
                    (
                        f"Bridge profile '{profile_name}' targets notebook '{cached_notebook_id}'; "
                        f"refreshing tunnel for '{notebook_id}'."
                    ),
                    err=True,
                )
            else:
                click.echo(
                    (
                        f"Bridge profile '{profile_name}' has no notebook binding metadata; "
                        f"refreshing tunnel for '{notebook_id}'."
                    ),
                    err=True,
                )

    try:
        ssh_public_key = load_ssh_public_key(pubkey)
    except ValueError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    try:
        ssh_runtime = resolve_ssh_runtime_config(
            cli_overrides={"rtunnel_bin": rtunnel_bin},
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    try:
        proxy_url = browser_api_module.setup_notebook_rtunnel(
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_public_key=ssh_public_key,
            ssh_runtime=ssh_runtime,
            session=session,
            headless=not debug_playwright,
            timeout=setup_timeout,
        )
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to set up notebook tunnel: {e}", EXIT_API_ERROR)
        return

    bridge = BridgeProfile(
        name=profile_name,
        proxy_url=proxy_url,
        ssh_user="root",
        ssh_port=ssh_port,
        has_internet=has_internet,
        notebook_id=notebook_id,
        rtunnel_port=port,
    )

    tunnel_config = load_tunnel_config(account=tunnel_account)
    tunnel_config.add_bridge(bridge)
    save_tunnel_config(tunnel_config)

    if not is_tunnel_available(
        bridge_name=profile_name,
        config=tunnel_config,
        retries=6,
        retry_pause=1.5,
    ):
        proxy_status = _describe_proxy_http_status(proxy_url)
        _handle_error(
            ctx,
            "APIError",
            "Tunnel setup completed, but SSH preflight failed.",
            EXIT_API_ERROR,
            hint=(
                "Retry 'inspire notebook ssh <notebook-id>' in a few seconds, "
                f"or run 'inspire tunnel test -b {profile_name}' to inspect connectivity. "
                f"Proxy readiness report: {proxy_status} ({redact_proxy_url(proxy_url)})."
            ),
        )
        return

    internet_status = "yes" if has_internet else "no"
    gpu_label = gpu_type if gpu_type else "CPU"
    click.echo(
        f"Added bridge '{profile_name}' (internet: {internet_status}, GPU: {gpu_label})",
        err=True,
    )

    if command is None:
        _run_interactive_notebook_ssh_with_reconnect(
            ctx,
            profile_name=profile_name,
            tunnel_account=tunnel_account,
            session=session,
            pubkey=pubkey,
            rtunnel_bin=rtunnel_bin,
            debug_playwright=debug_playwright,
            setup_timeout=setup_timeout,
            tunnel_retries=config.tunnel_retries,
            tunnel_retry_pause=config.tunnel_retry_pause,
        )
        return

    args = get_ssh_command_args(
        bridge_name=profile_name,
        config=tunnel_config,
        remote_command=command,
    )
    os.execvp("ssh", args)


__all__ = ["load_ssh_public_key", "run_notebook_ssh"]
