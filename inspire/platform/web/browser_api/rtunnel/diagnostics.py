"""Failure diagnostics for notebook rtunnel/SSH setup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from inspire.config.ssh_runtime import SshRuntimeConfig
from inspire.platform.web.browser_api.core import (
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _run_in_thread,
)
from inspire.platform.web.session import WebSession, get_web_session

from .commands import RtunnelSetupPlan, resolve_rtunnel_setup_plan
from .logging import trace_event
from .terminal import _build_batch_setup_script, _run_terminal_command_capture_via_websocket

_LOG = logging.getLogger("inspire.platform.web.browser_api.rtunnel.doctor")
_DOCTOR_DONE_MARKER = "INSPIRE_RTUNNEL_DOCTOR_DONE"


@dataclass(frozen=True)
class RtunnelDoctorReport:
    observed: str
    excerpt: str
    raw_output: str


def _doctor_command(
    *,
    port: int,
    ssh_port: int,
    ssh_runtime: Optional[SshRuntimeConfig],
    setup_plan: RtunnelSetupPlan,
) -> str:
    apt_mirror_url = (ssh_runtime.apt_mirror_url if ssh_runtime else "") or ""
    dropbear_deb_dir = (ssh_runtime.dropbear_deb_dir if ssh_runtime else "") or ""
    sshd_deb_dir = (ssh_runtime.sshd_deb_dir if ssh_runtime else "") or ""
    lines = [
        "set +e",
        'echo "doctor_version=1"',
        "if [ -r /etc/os-release ]; then . /etc/os-release; fi",
        'echo "distro_id=${ID:-}"',
        'echo "distro_codename=${VERSION_CODENAME:-}"',
        f'echo "bootstrap_mode={setup_plan.bootstrap_mode}"',
        f'echo "bootstrap_strategy={setup_plan.bootstrap_strategy}"',
        f'echo "apt_mirror_url={apt_mirror_url}"',
        f'echo "dropbear_deb_dir={dropbear_deb_dir}"',
        f'echo "sshd_deb_dir={sshd_deb_dir}"',
        f'echo "rtunnel_port={port}"',
        f'echo "ssh_port={ssh_port}"',
        "_has_sshd_bin=$( [ -x /usr/sbin/sshd ] && echo 1 || echo 0 )",
        "_has_dropbear_bin=$( [ -x /usr/sbin/dropbear ] && echo 1 || echo 0 )",
        '_ps_sshd=$(ps -ef | grep -c \\"[s]shd -p $SSH_PORT\\" 2>/dev/null || true)',
        '_ps_dropbear=$(ps -ef | grep -c \\"[d]ropbear.*-p.*$SSH_PORT\\" 2>/dev/null || true)',
        '_ps_rtunnel=$(ss -ltn 2>/dev/null | grep -Ec "[[:space:]](:|\\[::\\]:)?$PORT[[:space:]]" || true)',
        'echo "has_sshd_bin=${_has_sshd_bin}"',
        'echo "has_dropbear_bin=${_has_dropbear_bin}"',
        'echo "ps_sshd=${_ps_sshd}"',
        'echo "ps_dropbear=${_ps_dropbear}"',
        'echo "ps_rtunnel=${_ps_rtunnel}"',
        "if command -v dpkg-query >/dev/null 2>&1; then",
        "  for _pkg in openssh-server dropbear-bin libwrap0 libtomcrypt1 libtommath1; do",
        '    _status=$(dpkg-query -W -f="${Status}" "$_pkg" 2>/dev/null || true)',
        '    [ -n "$_status" ] && echo "pkg:${_pkg}:${_status}"',
        "  done",
        "fi",
        'if [ -x /usr/sbin/sshd ]; then ldd /usr/sbin/sshd 2>/dev/null | tail -n 12 | sed "s/^/ldd_sshd:/" || true; fi',
        "for _log in /tmp/setup_ssh.log /tmp/dropbear.log /tmp/rtunnel-server.log; do",
        '  if [ -f "$_log" ]; then',
        '    echo "log_begin:${_log}"',
        '    tail -n 25 "$_log" 2>/dev/null | sed "s|^|log:${_log}:|" || true',
        '    echo "log_end:${_log}"',
        "  fi",
        "done",
        f'echo "{_DOCTOR_DONE_MARKER}"',
    ]
    return _build_batch_setup_script(lines)


def _excerpt(output: str, *, limit_lines: int = 16) -> str:
    lines = [line.rstrip() for line in str(output or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[:limit_lines])


def _summarize_doctor_output(
    output: str,
    *,
    setup_plan: RtunnelSetupPlan,
) -> RtunnelDoctorReport:
    text = str(output or "")
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and ":" not in line.split("=", 1)[0]:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    observed_parts: list[str] = []
    distro_id = values.get("distro_id", "").strip()
    distro_codename = values.get("distro_codename", "").strip()
    if distro_id or distro_codename:
        observed_parts.append(
            f"distro={(' '.join(part for part in [distro_id, distro_codename] if part)).strip()}"
        )
    observed_parts.append(f"strategy={setup_plan.bootstrap_strategy}")
    for key in ("has_sshd_bin", "has_dropbear_bin", "ps_sshd", "ps_dropbear", "ps_rtunnel"):
        value = values.get(key)
        if value not in {None, ""}:
            observed_parts.append(f"{key}={value}")
    pkg_lines = [line.strip() for line in text.splitlines() if line.startswith("pkg:")]
    if pkg_lines:
        observed_parts.append("packages=" + "; ".join(pkg_lines[:3]))
    if "libwrap.so.0" in text:
        observed_parts.append("ldd_sshd_missing=libwrap.so.0")
    if "connect: connection refused" in text.lower():
        observed_parts.append("rtunnel_target=connection_refused")
    if setup_plan.bootstrap_strategy == "dropbear_bundle" and values.get("has_dropbear_bin") == "0":
        observed_parts.append("bundle_dropbear_missing=1")

    return RtunnelDoctorReport(
        observed=" | ".join(part for part in observed_parts if part),
        excerpt=_excerpt(text),
        raw_output=text,
    )


def _collect_notebook_rtunnel_diagnostics_sync(
    *,
    notebook_id: str,
    port: int,
    ssh_port: int,
    ssh_runtime: Optional[SshRuntimeConfig],
    session: Optional[WebSession],
    headless: bool,
) -> RtunnelDoctorReport | None:
    from playwright.sync_api import sync_playwright

    from inspire.platform.web.browser_api.playwright_notebooks import open_notebook_lab

    if session is None:
        session = get_web_session()

    setup_plan = resolve_rtunnel_setup_plan(ssh_runtime=ssh_runtime)
    batch_cmd = _doctor_command(
        port=port,
        ssh_port=ssh_port,
        ssh_runtime=ssh_runtime,
        setup_plan=setup_plan,
    )
    trace_event("doctor_collect_start", strategy=setup_plan.bootstrap_strategy)

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        context = _new_context(browser, storage_state=session.storage_state)
        page = context.new_page()
        try:
            lab_frame = open_notebook_lab(page, notebook_id=notebook_id, timeout=30000)
            result = _run_terminal_command_capture_via_websocket(
                context=context,
                lab_frame=lab_frame,
                batch_cmd=batch_cmd,
                timeout_ms=30000,
                completion_marker=_DOCTOR_DONE_MARKER,
            )
            output = str(result.get("output", "") or "").strip()
            if not output:
                trace_event("doctor_collect_empty")
                return None
            report = _summarize_doctor_output(output, setup_plan=setup_plan)
            _LOG.debug("doctor_observed=%s", report.observed)
            _LOG.debug("doctor_output=\n%s", report.raw_output)
            trace_event("doctor_collect_complete", observed=report.observed)
            return report
        except Exception as exc:
            trace_event("doctor_collect_failed", error=exc)
            _LOG.debug("doctor_collect_failed error=%s", exc)
            return None
        finally:
            try:
                context.close()
            finally:
                browser.close()


def collect_notebook_rtunnel_diagnostics(
    *,
    notebook_id: str,
    port: int,
    ssh_port: int,
    ssh_runtime: Optional[SshRuntimeConfig] = None,
    session: Optional[WebSession] = None,
    headless: bool = True,
) -> RtunnelDoctorReport | None:
    if _in_asyncio_loop():
        return _run_in_thread(
            _collect_notebook_rtunnel_diagnostics_sync,
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_runtime=ssh_runtime,
            session=session,
            headless=headless,
        )
    return _collect_notebook_rtunnel_diagnostics_sync(
        notebook_id=notebook_id,
        port=port,
        ssh_port=ssh_port,
        ssh_runtime=ssh_runtime,
        session=session,
        headless=headless,
    )


__all__ = [
    "RtunnelDoctorReport",
    "_doctor_command",
    "collect_notebook_rtunnel_diagnostics",
]
