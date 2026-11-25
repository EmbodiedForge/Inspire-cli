"""Notebook doctor command for diagnosing SSH tunnel and connectivity issues."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from inspire.bridge.tunnel import load_tunnel_config
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import (
    get_base_url,
    load_config,
    require_web_session,
    resolve_json_output,
)
from inspire.platform.web import session as web_session_module
from inspire.platform.web.session.browser import (
    MISSING_BROWSER_HINT,
    PLAYWRIGHT_INSTALL_HINT,
    MissingBrowserError,
)
from inspire.platform.web import browser_api as browser_api_module


# In-notebook probes. Both deliberately run over the Jupyter shell transport: when SSH is the
# thing being diagnosed, a check that needs SSH can only prove that SSH works.
# Each branch keeps the listing tool's own exit status: `ss | tail` reports tail's status, so a
# failing ss or netstat would reach the doctor as a clean-but-empty listing.
_LISTENERS_COMMAND = (
    "if command -v ss >/dev/null 2>&1; then "
    "__probe_out=$(ss -tln 2>/dev/null); __probe_rc=$?; "
    'printf "%s\\n" "$__probe_out" | tail -n +2; '
    "elif command -v netstat >/dev/null 2>&1; then "
    "__probe_out=$(netstat -tln 2>/dev/null); __probe_rc=$?; "
    'printf "%s\\n" "$__probe_out" | tail -n +3; '
    "else awk 'NR>1 && $4==\"0A\" {print $2}' /proc/net/tcp /proc/net/tcp6; __probe_rc=$?; fi; "
    "exit $__probe_rc"
)

# 24 characters of the base64 blob identify a key well enough to compare while staying useless
# to anyone who later reads the diagnostic output or the notebook's shell history.
_AUTH_KEYS_COMMAND = (
    "awk '{print substr($2,1,24)}' /root/.ssh/authorized_keys 2>/dev/null | sort -u"
)


def _run_in_notebook_shell(
    session: web_session_module.WebSession, notebook_id: str, command: str, timeout: int = 45
) -> tuple[str, int]:
    """Run *command* through the notebook's Jupyter shell; return (stdout, returncode)."""
    from inspire.platform.web.browser_api.notebook_shell import execute_notebook_shell_command

    result = execute_notebook_shell_command(
        notebook_id=notebook_id, command=command, session=session, timeout=timeout
    )
    return result.stdout or "", result.returncode


def _parse_public_listeners(text: str) -> list[str]:
    """Return ``*:port`` for every listener bound to all interfaces.

    Understands ``ss``/``netstat`` columns and the hex ``addr:port`` pairs printed from
    ``/proc/net/tcp*``, because notebook images differ in which tools they carry. Loopback and
    explicitly bound addresses are ignored on purpose: this check is about reachability from
    outside the container, not about the notebook talking to itself.
    """
    found: list[str] = []
    for line in text.splitlines():
        # ss puts the local address in column 4, netstat in column 4 as well, and the /proc
        # fallback prints it alone - so scan the line for the first address-shaped token
        # instead of hard-coding a column. Peer columns like 0.0.0.0:* fall out at the port
        # check, since '*' is neither a decimal nor a 4-hex-digit port.
        for token in line.split():
            if ":" not in token:
                continue
            host, _, port = token.rpartition(":")
            if not port:
                continue
            if port.isdigit():
                port_number = port
            elif len(port) == 4 and all(char in "0123456789abcdefABCDEF" for char in port):
                port_number = str(int(port, 16))
            else:
                continue

            bare = host.replace("[", "").replace("]", "")
            wildcard = host in ("0.0.0.0", "*", "::", "[::]")
            if not wildcard and len(bare) in (8, 32) and set(bare) <= {"0"}:
                wildcard = True  # /proc/net hex encoding of the unspecified address

            if wildcard:
                entry = f"*:{port_number}"
                if entry not in found:
                    found.append(entry)
                break  # one local address per line; anything later is the peer
    return found


def _resolve_local_public_key(bridge_profile: object) -> tuple[Optional[str], str]:
    """Return the base64 blob this bridge authenticates with, and where it came from."""
    candidates: list[Path] = []
    identity = getattr(bridge_profile, "identity_file", None) if bridge_profile else None
    if identity:
        candidates.append(Path(f"{identity}.pub"))
    candidates.append(Path.home() / ".ssh" / "id_ed25519.pub")
    candidates.append(Path.home() / ".ssh" / "id_rsa.pub")

    for path in candidates:
        try:
            if not path.is_file():
                continue
            fields = path.read_text(encoding="utf-8").split()
        except OSError:
            continue
        if len(fields) >= 2:
            return fields[1], str(path)
    return None, " or ".join(str(candidate) for candidate in candidates)


def _authorized_keys_state(remote_prefixes: list[str], local_blob: Optional[str]) -> str:
    """Classify what the notebook has installed against the key we authenticate with.

    ``missing`` means nothing is installed, so SSH cannot authenticate no matter how healthy the
    tunnel looks. ``drift`` means keys are installed but none is ours - the usual signature of a
    rebuilt container, since notebook filesystems are ephemeral and the key is appended once by
    the setup script.
    """
    if not remote_prefixes:
        return "missing"
    if not local_blob:
        # Nothing to compare against: saying "drift" here would blame the notebook for a
        # gap on our side.
        return "unknown-local"
    if any(local_blob.startswith(prefix) for prefix in remote_prefixes):
        return "ours-present"
    return "drift"


_DEEP_SSH_COUNTERS = ("ps_sshd", "ps_dropbear", "ps_rtunnel")


def _parse_deep_ssh_counters(observed: str) -> dict[str, int]:
    """Pull the process/listener counters out of the deep diagnostics ``observed`` summary.

    The in-notebook probe reports them as ``key=value`` pairs joined with ``|``; a counter that
    is missing or unparsable counts as zero, because the probe only prints what it could read.
    """
    counters: dict[str, int] = {}
    for part in str(observed or "").split("|"):
        key, _, value = part.strip().partition("=")
        if key in _DEEP_SSH_COUNTERS:
            try:
                counters[key] = int(value.strip())
            except ValueError:
                continue
    return counters


@click.command("doctor")
@click.argument("notebook", required=False)
@click.option(
    "-b",
    "--bridge",
    help="Bridge profile name to diagnose (alternative to notebook ID)",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show detailed diagnostic information",
)
@pass_context
def notebook_doctor_cmd(
    ctx: Context,
    notebook: Optional[str],
    bridge: Optional[str],
    verbose: bool,
) -> None:
    """Diagnose notebook SSH tunnel and connectivity issues.

    Checks notebook status, SSH tunnel health, and common configuration issues.
    Useful for debugging connection problems.

    \b
    Examples:
        inspire notebook doctor abc123
        inspire notebook doctor -b my-bridge
        inspire notebook doctor -b my-bridge --verbose
    """
    json_output = resolve_json_output(ctx, False)

    # Resolve notebook identifier
    notebook_id: Optional[str] = None
    bridge_profile = None

    if bridge:
        tunnel_config = load_tunnel_config()
        bridge_profile = tunnel_config.get_bridge(bridge)
        if bridge_profile is None:
            _handle_error(
                ctx,
                "ConfigError",
                f"Bridge profile '{bridge}' not found.",
                EXIT_CONFIG_ERROR,
                hint="Run 'inspire tunnel list' to see available bridges.",
            )
            return
        notebook_id = getattr(bridge_profile, "notebook_id", None)
        if not notebook_id:
            _handle_error(
                ctx,
                "ConfigError",
                f"Bridge profile '{bridge}' has no notebook_id binding.",
                EXIT_CONFIG_ERROR,
                hint="Re-create the bridge with 'inspire notebook ssh <id> --save-as <name>'.",
            )
            return
    elif notebook:
        notebook_id = notebook
    else:
        _handle_error(
            ctx,
            "ValidationError",
            "Provide a notebook ID or use -b/--bridge to specify a bridge profile.",
            EXIT_CONFIG_ERROR,
            hint="Run 'inspire notebook list' to see available notebooks.",
        )
        return

    session = require_web_session(
        ctx,
        hint=(
            "Notebook doctor requires web authentication. "
            "Set [auth].username/password in config.toml or "
            "INSPIRE_USERNAME/INSPIRE_PASSWORD."
        ),
    )

    base_url = get_base_url()
    config = load_config(ctx)

    diagnostics: dict = {
        "notebook_id": notebook_id,
        "checks": {},
        "issues": [],
        "recommendations": [],
    }

    if bridge:
        diagnostics["bridge"] = bridge

    # Check 1: Resolve notebook ID via API
    if not json_output:
        click.echo(f"Diagnosing notebook '{notebook_id}'...", err=True)
        click.echo("-" * 50, err=True)

    try:
        from .notebook_lookup import _resolve_notebook_id

        resolved_id, _ = _resolve_notebook_id(
            ctx,
            session=session,
            config=config,
            base_url=base_url,
            identifier=notebook_id,
            json_output=False,
        )
        diagnostics["checks"]["notebook_resolution"] = {
            "status": "ok",
            "resolved_id": resolved_id,
        }
        # Every check below hits /api/v1/notebook/<id>; without adopting the resolved
        # ID a name argument makes them all report "record does not exist".
        notebook_id = resolved_id
        diagnostics["notebook_id"] = resolved_id
        if verbose and not json_output:
            click.echo(f"✓ Notebook resolved: {resolved_id}", err=True)
    except Exception as e:
        diagnostics["checks"]["notebook_resolution"] = {
            "status": "error",
            "error": str(e),
        }
        diagnostics["issues"].append(f"Cannot resolve notebook: {e}")
        diagnostics["recommendations"].append(
            "Verify the notebook ID/name is correct and you have access."
        )
        if not json_output:
            click.echo(f"✗ Cannot resolve notebook: {e}", err=True)

    # Check 2: Get notebook status from API
    try:
        from inspire.platform.web.browser_api.core import _request_json as browser_request

        data = browser_request(
            session,
            "GET",
            f"/api/v1/notebook/{notebook_id}",
            referer=f"{base_url}/jobs/interactiveModeling",
            timeout=30,
        )
        if data.get("code") == 0:
            notebook_data = data.get("data", {})
            status = notebook_data.get("status", "UNKNOWN")
            diagnostics["checks"]["notebook_api_status"] = {
                "status": "ok",
                "notebook_status": status,
            }
            if verbose and not json_output:
                click.echo(f"✓ Notebook API status: {status}", err=True)

            if status != "RUNNING":
                diagnostics["issues"].append(f"Notebook is not RUNNING (current: {status})")
                diagnostics["recommendations"].append(
                    f"Start the notebook with 'inspire notebook start {notebook_id}'"
                )
                if not json_output:
                    click.echo(f"⚠ Notebook is {status}, not RUNNING", err=True)
            if status in {"FAILED", "ERROR"}:
                events = browser_api_module.get_notebook_events(notebook_id, session=session)
                diagnostics["checks"]["notebook_events"] = {
                    "status": "ok" if events else "missing",
                    "events": events,
                }
                if events:
                    diagnostics["issues"].append(f"Notebook scheduling events:\n{events}")
                    if not json_output:
                        click.echo("Notebook events:", err=True)
                        click.echo(events, err=True)
                else:
                    diagnostics["recommendations"].append(
                        "No notebook events were returned; inspect the Events tab before deleting "
                        "the failed notebook."
                    )
        else:
            error_msg = data.get("message", "Unknown error")
            diagnostics["checks"]["notebook_api_status"] = {
                "status": "error",
                "error": error_msg,
            }
            diagnostics["issues"].append(f"API error: {error_msg}")
            if not json_output:
                click.echo(f"✗ API error: {error_msg}", err=True)
    except Exception as e:
        diagnostics["checks"]["notebook_api_status"] = {
            "status": "error",
            "error": str(e),
        }
        diagnostics["issues"].append(f"Cannot fetch notebook status: {e}")
        if not json_output:
            click.echo(f"✗ Cannot fetch notebook status: {e}", err=True)

    # Check 3: Check bridge profile if provided
    if bridge_profile:
        diagnostics["checks"]["bridge_profile"] = {
            "status": "ok",
            "bridge_name": bridge,
            "has_internet": getattr(bridge_profile, "has_internet", None),
            "ssh_port": getattr(bridge_profile, "ssh_port", None),
        }
        if verbose and not json_output:
            click.echo(f"✓ Bridge profile '{bridge}' found", err=True)

    # Check 4: Test SSH connectivity if notebook is RUNNING
    if diagnostics["checks"].get("notebook_api_status", {}).get("notebook_status") == "RUNNING":
        if bridge:
            try:
                from inspire.bridge.tunnel import is_tunnel_available

                tunnel_config = load_tunnel_config()
                available = is_tunnel_available(
                    bridge_name=bridge,
                    config=tunnel_config,
                    retries=2,
                    retry_pause=1.0,
                )
                diagnostics["checks"]["ssh_tunnel"] = {
                    "status": "ok" if available else "error",
                    "available": available,
                }
                if not json_output:
                    if available:
                        click.echo("✓ SSH tunnel is available", err=True)
                    else:
                        click.echo("✗ SSH tunnel is NOT available", err=True)

                if not available:
                    diagnostics["issues"].append("SSH tunnel connectivity failed")
                    diagnostics["recommendations"].append(
                        "Try 'inspire notebook ssh' to rebuild the tunnel, "
                        "or use 'inspire notebook exec' as a workaround."
                    )
            except Exception as e:
                diagnostics["checks"]["ssh_tunnel"] = {
                    "status": "error",
                    "error": str(e),
                }
                diagnostics["issues"].append(f"SSH tunnel check failed: {e}")
                if not json_output:
                    click.echo(f"✗ SSH tunnel check failed: {e}", err=True)

    # Check 5: Check JupyterLab accessibility
    # Try HTTP-based check first (works without Playwright)
    if not json_output:
        click.echo("Checking JupyterLab accessibility...", err=True)

    try:
        from inspire.platform.web.browser_api.rtunnel.http_lab_resolution import (
            resolve_lab_url_http,
        )

        lab_url = resolve_lab_url_http(notebook_id, session, timeout=30)
        if lab_url:
            diagnostics["checks"]["jupyterlab_access"] = {
                "status": "ok",
                "method": "http",
            }
            if not json_output:
                click.echo("✓ JupyterLab is accessible via HTTP", err=True)
        else:
            raise RuntimeError("Could not resolve JupyterLab URL")
    except Exception as http_err:
        # HTTP check failed; try Playwright if available
        try:
            from inspire.platform.web.browser_api.playwright_notebooks import (
                open_notebook_lab,
            )
            from playwright.sync_api import sync_playwright
            from inspire.platform.web.browser_api.core import (
                _launch_browser,
                _new_context,
            )

            with sync_playwright() as p:
                browser = _launch_browser(p, headless=True)
                context = _new_context(browser, storage_state=session.storage_state)
                page = context.new_page()
                try:
                    _ = open_notebook_lab(page, notebook_id=notebook_id, timeout=30000)
                    diagnostics["checks"]["jupyterlab_access"] = {
                        "status": "ok",
                        "method": "browser",
                    }
                    if not json_output:
                        click.echo("✓ JupyterLab is accessible via WebSocket", err=True)
                except Exception as e:
                    diagnostics["checks"]["jupyterlab_access"] = {
                        "status": "error",
                        "error": str(e),
                    }
                    diagnostics["issues"].append(f"JupyterLab not accessible: {e}")
                    diagnostics["recommendations"].append(
                        "Ensure the notebook is RUNNING and JupyterLab is initialized."
                    )
                    if not json_output:
                        click.echo(f"✗ JupyterLab not accessible: {e}", err=True)
                finally:
                    context.close()
                    browser.close()
        except MissingBrowserError as browser_err:
            # The browser binary is a separate install; report it as its own failed
            # check instead of falling through to a Playwright stack trace.
            diagnostics["checks"]["jupyterlab_access"] = {
                "status": "error",
                "error": str(browser_err),
            }
            diagnostics["issues"].append("Playwright Chromium is not installed on this machine")
            diagnostics["recommendations"].append(
                f"Install the browser runtime once:\n{MISSING_BROWSER_HINT}"
            )
            if not json_output:
                click.echo(f"✗ {browser_err}", err=True)
        except ImportError:
            # Playwright not installed; report HTTP error
            diagnostics["checks"]["jupyterlab_access"] = {
                "status": "error",
                "error": f"HTTP check failed: {http_err}",
            }
            diagnostics["issues"].append(
                f"JupyterLab not accessible (Playwright not installed): {http_err}"
            )
            diagnostics["recommendations"].append(
                f"Install Playwright for full diagnostics:\n{PLAYWRIGHT_INSTALL_HINT}"
            )
            if not json_output:
                click.echo(
                    f"✗ JupyterLab not accessible (HTTP failed, Playwright not installed): {http_err}",
                    err=True,
                )
        except Exception as browser_err:
            diagnostics["checks"]["jupyterlab_access"] = {
                "status": "error",
                "error": str(browser_err),
            }
            diagnostics["issues"].append(f"JupyterLab not accessible: {browser_err}")
            diagnostics["recommendations"].append(
                "Ensure the notebook is RUNNING and JupyterLab is initialized."
            )
            if not json_output:
                click.echo(f"✗ JupyterLab not accessible: {browser_err}", err=True)

    # Check 6: what the notebook listens on, and Check 7: whether our key is still installed.
    # Both speak Jupyter, so they still answer when the SSH path under test is the broken one.
    if diagnostics["checks"].get("notebook_api_status", {}).get("notebook_status") == "RUNNING":
        if not json_output:
            click.echo("Checking in-notebook listeners...", err=True)
        try:
            listeners_out, listeners_rc = _run_in_notebook_shell(
                session, notebook_id, _LISTENERS_COMMAND
            )
            public_listeners = _parse_public_listeners(listeners_out)
            diagnostics["checks"]["exposed_listeners"] = {
                "status": (
                    "error" if listeners_rc != 0 else "warning" if public_listeners else "ok"
                ),
                "returncode": listeners_rc,
                "all_interface_listeners": public_listeners,
            }
            if listeners_rc != 0:
                # An empty listing from a failed probe says nothing, so it must not read as
                # "nothing is exposed".
                diagnostics["issues"].append(
                    f"Listener probe failed inside the notebook (exit {listeners_rc}); neither "
                    "ss, netstat, nor the /proc/net/tcp fallback produced a listing"
                )
                if not json_output:
                    click.echo(f"! listener probe failed (exit {listeners_rc})", err=True)
            elif public_listeners:
                joined = ", ".join(public_listeners)
                diagnostics["issues"].append(f"Notebook listens on all interfaces: {joined}")
                diagnostics["recommendations"].append(
                    "Platform docs do not state whether notebooks can reach each other, so "
                    "treat anything bound to 0.0.0.0 as reachable. Bind your own services to "
                    "127.0.0.1 or put auth in front of them; jupyter and the injected "
                    "code-server are the platform's and cannot be changed from inside."
                )
                if not json_output:
                    click.echo(f"! listening on all interfaces: {joined}", err=True)
            elif not json_output:
                click.echo("✓ no all-interface listeners found", err=True)
        except Exception as exc:  # pragma: no cover - depends on notebook state
            diagnostics["checks"]["exposed_listeners"] = {"status": "error", "error": str(exc)}
            if not json_output:
                click.echo(f"! could not inspect listeners: {exc}", err=True)

        if not json_output:
            click.echo("Checking authorized_keys...", err=True)
        try:
            keys_out, keys_rc = _run_in_notebook_shell(session, notebook_id, _AUTH_KEYS_COMMAND)
            remote_prefixes = [line.strip() for line in keys_out.splitlines() if line.strip()]
            local_blob, local_source = _resolve_local_public_key(bridge_profile)
            # A probe that could not run prints nothing, and nothing looks exactly like "missing"
            # keys unless the exit status is kept: the check failing is not a verdict on the keys.
            state = (
                "probe-failed"
                if keys_rc != 0
                else _authorized_keys_state(remote_prefixes, local_blob)
            )
            diagnostics["checks"]["authorized_keys"] = {
                "status": (
                    "error" if keys_rc != 0 else "ok" if state == "ours-present" else "warning"
                ),
                "state": state,
                "returncode": keys_rc,
                "remote_key_count": len(remote_prefixes),
                "local_public_key": local_source,
            }
            if keys_rc != 0:
                diagnostics["issues"].append(
                    f"authorized_keys probe failed inside the notebook (exit {keys_rc}); "
                    "/root/.ssh/authorized_keys could not be read, so this check says nothing "
                    "about which keys are installed"
                )
                diagnostics["recommendations"].append(
                    "Re-run this doctor once the notebook's Jupyter shell answers again; every "
                    "in-notebook check shares that transport."
                )
            elif state == "missing":
                diagnostics["issues"].append(
                    "/root/.ssh/authorized_keys is empty or missing in the notebook"
                )
                diagnostics["recommendations"].append(
                    "Re-run 'inspire notebook ssh <id>' to reinstall the key. Notebook "
                    "filesystems are ephemeral, so a restarted or rebuilt notebook starts "
                    "without our key even when the bridge profile still looks fine."
                )
            elif state == "drift":
                diagnostics["issues"].append(
                    f"authorized_keys exists but does not contain the key from {local_source}"
                )
                diagnostics["recommendations"].append(
                    "Re-run 'inspire notebook ssh <id>' with the key you actually use, or "
                    "point the bridge at the right identity_file. Authenticating with a "
                    "different key than the one installed fails as a permission error that "
                    "looks like a tunnel problem."
                )
            elif state == "unknown-local":
                diagnostics["issues"].append(
                    f"No local public key found to compare against (looked for {local_source})"
                )
                diagnostics["recommendations"].append(
                    "Set [ssh] identity_file (or --identity) so the bridge names the key it "
                    "uses; without it this check cannot say whether the installed key is ours."
                )
            if not json_output:
                if keys_rc != 0:
                    click.echo(f"! authorized_keys probe failed (exit {keys_rc})", err=True)
                elif state == "ours-present":
                    click.echo("✓ our public key is installed in the notebook", err=True)
                else:
                    click.echo(
                        f"! authorized_keys {state} ("
                        f"{len(remote_prefixes)} key(s) installed, ours expected from "
                        f"{local_source})",
                        err=True,
                    )
        except Exception as exc:  # pragma: no cover - depends on notebook state
            diagnostics["checks"]["authorized_keys"] = {"status": "error", "error": str(exc)}
            if not json_output:
                click.echo(f"! could not inspect authorized_keys: {exc}", err=True)

    # Check 8: Deep SSH diagnostics (sshd/dropbear processes, packages, logs)
    # Only run when verbose, as it launches a separate Playwright browser
    if (
        verbose
        and diagnostics["checks"].get("notebook_api_status", {}).get("notebook_status") == "RUNNING"
    ):
        if not json_output:
            click.echo("Running deep SSH diagnostics...", err=True)
        try:
            from inspire.platform.web.browser_api.rtunnel.diagnostics import (
                collect_notebook_rtunnel_diagnostics,
            )

            port = getattr(bridge_profile, "rtunnel_port", None) or 31337
            ssh_port = getattr(bridge_profile, "ssh_port", 22222)

            report = collect_notebook_rtunnel_diagnostics(
                notebook_id=notebook_id,
                port=port,
                ssh_port=ssh_port,
                session=session,
                headless=True,
            )

            if report is not None:
                counters = _parse_deep_ssh_counters(report.observed)
                sshd_running = any(counters.get(name, 0) > 0 for name in _DEEP_SSH_COUNTERS)
                diagnostics["checks"]["deep_ssh"] = {
                    "status": "ok" if sshd_running else "error",
                    "observed": report.observed,
                    "excerpt": report.excerpt,
                    "process_counts": {name: counters.get(name, 0) for name in _DEEP_SSH_COUNTERS},
                }
                if not sshd_running:
                    diagnostics["issues"].append(
                        "No sshd, dropbear, or rtunnel listener is running inside the notebook "
                        "(ps_sshd, ps_dropbear and ps_rtunnel are all zero), so SSH cannot "
                        "authenticate however healthy the tunnel looks"
                    )
                    diagnostics["recommendations"].append(
                        f"Re-install the SSH runtime with 'inspire notebook ssh {notebook_id}', "
                        "then re-run this doctor with --verbose and read the setup logs in the "
                        "excerpt."
                    )
                if verbose and not json_output:
                    mark = "✓" if sshd_running else "✗"
                    click.echo(f"{mark} Deep SSH: {report.observed}", err=True)
                    if report.excerpt:
                        click.echo(f"  Log excerpt: {report.excerpt[:200]}", err=True)
            else:
                diagnostics["checks"]["deep_ssh"] = {
                    "status": "skipped",
                    "reason": "Diagnostic command returned no output",
                }
                if not json_output:
                    click.echo("⚠ Deep SSH diagnostics returned no output", err=True)
        except Exception as e:
            diagnostics["checks"]["deep_ssh"] = {
                "status": "error",
                "error": str(e),
            }
            if not json_output:
                click.echo(f"✗ Deep SSH diagnostics failed: {e}", err=True)

    # Checks that need a live notebook are recorded as skipped instead of left out: a stopped
    # notebook used to contribute an almost-empty check list and still be called healthy.
    if diagnostics["checks"].get("notebook_api_status", {}).get("notebook_status") != "RUNNING":
        for skipped_check in ("ssh_tunnel", "exposed_listeners", "authorized_keys", "deep_ssh"):
            diagnostics["checks"].setdefault(
                skipped_check,
                {"status": "skipped", "reason": "notebook is not RUNNING"},
            )

    not_ok = {
        name: str(check.get("status"))
        for name, check in diagnostics["checks"].items()
        if isinstance(check, dict) and check.get("status") != "ok"
    }
    # An issue can be recorded without a check flipping out of "ok" (and a check can fail
    # without adding an issue), so the verdict needs both signals.
    all_ok = not diagnostics["issues"] and not not_ok

    diagnostics["overall_status"] = "healthy" if all_ok else "issues_found"

    if json_output:
        click.echo(json_formatter.format_json(diagnostics))
    else:
        click.echo("-" * 50, err=True)
        if all_ok:
            click.echo("✓ All checks passed! Notebook is healthy.", err=True)
        else:
            if diagnostics["issues"]:
                click.echo(f"✗ Found {len(diagnostics['issues'])} issue(s):", err=True)
                for issue in diagnostics["issues"]:
                    click.echo(f"  - {issue}", err=True)
            failed = {name: status for name, status in not_ok.items() if status != "skipped"}
            skipped = [name for name, status in not_ok.items() if status == "skipped"]
            if failed:
                click.echo(
                    "! checks not ok: " + ", ".join(f"{n}={s}" for n, s in sorted(failed.items())),
                    err=True,
                )
            if skipped:
                click.echo("! skipped checks: " + ", ".join(sorted(skipped)), err=True)
            if diagnostics["recommendations"]:
                click.echo("\nRecommendations:", err=True)
                for rec in diagnostics["recommendations"]:
                    click.echo(f"  • {rec}", err=True)


__all__ = ["notebook_doctor_cmd"]
