from __future__ import annotations

import base64
import subprocess

from inspire.config.ssh_runtime import SshRuntimeConfig
from inspire.platform.web.browser_api.rtunnel.commands import resolve_rtunnel_setup_plan
from inspire.platform.web.browser_api.rtunnel.diagnostics import (
    _doctor_command,
    _summarize_doctor_output,
)
from inspire.platform.web.browser_api.rtunnel.logging import RtunnelTrace, format_trace_summary


def test_summarize_doctor_output_reports_observed_facts() -> None:
    plan = resolve_rtunnel_setup_plan(
        ssh_runtime=SshRuntimeConfig(apt_mirror_url="http://mirror.example/ubuntu/")
    )
    output = "\n".join(
        [
            "distro_id=debian",
            "distro_codename=bookworm",
            "apt_mirror_url=http://mirror.example/ubuntu/",
            "ps_sshd=0",
            "ps_dropbear=0",
        ]
    )

    report = _summarize_doctor_output(output, setup_plan=plan)

    assert "distro=debian bookworm" in report.observed
    assert "strategy=dropbear_mirror" in report.observed


def test_summarize_doctor_output_includes_package_state() -> None:
    plan = resolve_rtunnel_setup_plan(
        ssh_runtime=SshRuntimeConfig(apt_mirror_url="http://mirror.example/debian/")
    )
    output = "\n".join(
        [
            "distro_id=debian",
            "pkg:openssh-server:install ok unpacked",
            "ps_sshd=0",
            "ps_dropbear=0",
        ]
    )

    report = _summarize_doctor_output(output, setup_plan=plan)

    assert "packages=pkg:openssh-server:install ok unpacked" in report.observed


def test_format_trace_summary_includes_diagnosis_fields() -> None:
    trace = RtunnelTrace(
        run_id="abc12345",
        notebook_id="nb-1",
        account="user",
        port=31337,
        ssh_port=22222,
        headless=True,
        summary={
            "bootstrap_strategy": "dropbear_mirror",
            "diagnosis_observed": "distro=debian bookworm | strategy=dropbear_mirror",
            "diagnosis_excerpt": "distro_id=debian",
        },
    )

    summary = format_trace_summary(trace)

    assert "bootstrap_strategy=dropbear_mirror" in summary
    assert "diagnosis_observed=distro=debian bookworm | strategy=dropbear_mirror" in summary


def _decode_batched_script(wrapper: str) -> str:
    lines = wrapper.splitlines()
    assert lines[0].startswith("cat <<'")
    marker = lines[0].split("<<'", 1)[1].split("'", 1)[0]
    end = lines.index(marker)
    encoded = "".join(lines[1:end])
    return base64.b64decode(encoded).decode()


def test_doctor_command_payload_is_valid_bash() -> None:
    plan = resolve_rtunnel_setup_plan(
        ssh_runtime=SshRuntimeConfig(apt_mirror_url="http://mirror.example/ubuntu/")
    )

    wrapper = _doctor_command(port=39017, ssh_port=22222, ssh_runtime=None, setup_plan=plan)
    payload = _decode_batched_script(wrapper)

    result = subprocess.run(
        ["bash", "-n"],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert 'sed "s|^|log:${_log}:|"' in payload
    assert (
        '_ps_rtunnel=$(ss -ltn 2>/dev/null | grep -Ec "[[:space:]](:|\\[::\\]:)?$PORT[[:space:]]" || true)'
        in payload
    )
