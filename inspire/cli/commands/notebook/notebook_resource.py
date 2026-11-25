"""``inspire notebook resource`` - one screen per notebook: entitled, enforced, in use, remembered.

Four questions about one instance, read from four sources, printed together and kept apart.

* **declaration** - what the platform says the instance was given (`notebook status`:
  `memory_gib`, `cpu_count`, `shm_gib`). A promise, not a measurement.
* **enforced** - what the container's own cgroup caps it at (`/sys/fs/cgroup`: `memory.max`,
  `memory.current`, `memory.high`, `memory.events`, `cpu.max`, `pids.max`, `pids.current`), read
  over the same Jupyter shell `notebook exec` uses. This is the trust check, and it is the source
  the incident in the note below was invisible to.
* **live** - what is using it right now: current memory and pids from the cgroup, plus, under
  ``--processes``, the per-process table over the notebook's SSH bridge (GPU utilisation where
  `nvidia-smi` answers, the CPU/``/proc`` view where it does not).
* **history** - the platform's own usage series over time (``--since``: `cpu_usage_rate` and
  `memory_usage_rate`, how much was used, never how much was allowed).

The three views used to be three commands, and `docker stats` is the argument for one: its
`MEM USAGE / LIMIT` column puts the ceiling on the same line as the usage, and it is the
fallback to the *host's* total memory when no limit is set that this command exists to defeat.
Here the pairing runs one column further, to the declaration. The sections stay separately named
in text and in ``--json`` because their disagreement *is* the finding: a rate cannot tell you a
ceiling, a ceiling cannot tell you what is running, and neither can tell you what the platform
believes it handed out.

Why this command exists (incident, 2026-11-06, CPU notebook ``pi-scale-big``): inside the container
``free -g`` answers the **host's** memory (1510 GiB measured) while the cgroup caps the container at
400 GiB (``/sys/fs/cgroup/memory.max`` = 429496729600). A workload sized from ``/proc/meminfo``
therefore runs past the real ceiling. When it does, the container shows nothing at all: no
``CAP_SYSLOG`` so no ``dmesg``, no syslog, and the OOM-killed process leaves an empty log. The only
trace left is ``oom_kill 2`` in ``/sys/fs/cgroup/memory.events``. 41 parallel arms then sat looking
alive for hours because every tool in the box - ``free``, ``ps``, ``nvidia-smi`` - either lied or
was absent.

So the enforced section reads the container's own ``/sys/fs/cgroup`` and ``/proc`` files through the
existing notebook exec plumbing (the Jupyter shell, so no GPU, no SSH tunnel, no extra platform
route) and prints them beside the platform's own declaration for the same instance. Four rules
shape the output:

* **Unknown is not zero, and the container's view is not the limit.** A missing or unreadable
  file is reported as unknown, and ``MemTotal`` disagreeing with ``memory.max`` is the incident
  itself, so it is printed as a warning rather than resolved by silently picking one number.
* **A fault is the exit code, not a paragraph.** ``memory.events.oom_kill`` is cumulative for the
  cgroup's lifetime, so it says the kernel OOM-killer *has run here*, not that it just did; a
  declaration that disagrees with the enforced cgroup means the box is not the one that was paid
  for. Either one is a hard alarm and exits ``EXIT_GENERAL_ERROR`` - the same code
  the standalone limits probe exited with - while the full report still goes to stdout, because
  the alarm is a finding *about* the container, not a failed read of it.
* **A half-read probe is an error, not a short report.** The script closes with an ``@@end``
  sentinel; without it the capture was cut and the remaining files are unobserved, not absent.
* **Each source answers for itself.** History needs a web session, the live per-process view needs
  a notebook-backed SSH bridge, the enforced view needs the Jupyter shell. A source that cannot
  answer is named as unreachable, for that source, and the sections that can answer still do.

``--local`` runs the identical cgroup script on the machine the CLI runs on (no platform call, no
session), which is also how a fixture tree is pointed at it: ``INSPIRE_LIMITS_CGROUP_ROOT`` and
``INSPIRE_LIMITS_PROC_DIR`` relocate the two roots. One script, one parser, so the offline test
path cannot drift from the live one.
"""

from __future__ import annotations

import concurrent.futures
import csv
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Optional

import click

from inspire.bridge.tunnel import is_tunnel_available, load_tunnel_config, run_ssh_command
from inspire.bridge.tunnel.models import BridgeProfile, TunnelConfig
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_TIMEOUT,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.notebook_cli import (
    get_base_url,
    load_config,
    require_web_session,
    resolve_json_output,
)
from inspire.cli.utils.resource_metrics_cli import (
    metrics_payload,
    metrics_text_lines,
    parse_metric_types,
)

from .proc_probe import (
    DEFAULT_PROCESSES,
    MAX_PROCESSES,
    build_host_proc_probe,
    has_host_telemetry,
    parse_host_proc_probe,
)

#: Near-ceiling thresholds, as a percentage of the cgroup limit. 85% is "shrink the fan-out",
#: 95% is "the next allocation is the one that gets a process killed".
WARN_PERCENT = 85.0
CRITICAL_PERCENT = 95.0

#: Bytes in a GiB. The platform declares memory in GiB and the cgroup reports bytes, and 400 GiB
#: really is 429496729600 bytes, so the comparison is exact rather than approximate.
GIB = 1024**3

#: How far the platform's declaration may sit from the enforced cgroup before the two stop
#: describing the same instance. Memory is exact arithmetic (400 GiB really is 429496729600
#: bytes), so its tolerance only covers rounding; a CPU quota is a quota/period ratio, so it gets
#: the slack a ratio needs. Past either one the merged report raises an alarm, not a comment.
MEMORY_DRIFT_TOLERANCE = 0.005
CPU_DRIFT_TOLERANCE = 0.02

#: cgroup v2 files read verbatim from inside the container. Every one is optional: the report says
#: which were missing instead of treating a missing file as a zero.
_CGROUP_FILES: tuple[str, ...] = (
    "memory.max",
    "memory.high",
    "memory.current",
    "memory.peak",
    "memory.stat",
    "memory.events",
    "memory.events.local",
    "cpu.max",
    "cpu.stat",
    "pids.max",
    "pids.current",
    "pids.peak",
)

_PROBE_TEMPLATE = r"""
# The two roots are relocatable (see the module docstring): unset in every real call, and set by the
# fixture tests to point the same script at a fake tree.
__root="${INSPIRE_LIMITS_CGROUP_ROOT:-/sys/fs/cgroup}"
__proc="${INSPIRE_LIMITS_PROC_DIR:-/proc}"

# /proc/self/cgroup names *our* cgroup. In a container with its own cgroup namespace that is "/"
# and $__root is already the capped directory; on a host it is a slice/scope path under the root.
__self=$(awk -F: '$1 == "0" {print $3; exit}' "$__proc/self/cgroup" 2>/dev/null)
__dir="$__root"
case "$__self" in
  ""|"/") ;;
  *) if [ -d "$__root$__self" ]; then __dir="$__root$__self"; fi ;;
esac

# The memory/pids files only exist where the controller was delegated. If our own cgroup has
# none, walk up to the nearest ancestor that does; that is the limit the kernel enforces on us.
if [ ! -f "$__dir/memory.max" ]; then
  __walk="$__dir"
  while [ "$__walk" != "$__root" ] && [ "$__walk" != "/" ]; do
    __walk=$(dirname "$__walk")
    if [ -f "$__walk/memory.max" ]; then
      __dir="$__walk"
      break
    fi
  done
fi

printf '@@cgroup_root\n%s\n' "$__root"
printf '@@cgroup_dir\n%s\n' "$__dir"
printf '@@self_cgroup\n%s\n' "${__self:-unknown}"
printf '@@cgroup_version\n'
if [ -f "$__root/cgroup.controllers" ] || [ -f "$__dir/cgroup.controllers" ]; then
  printf 'v2\n'
elif [ -d "$__root/memory" ]; then
  printf 'v1\n'
else
  printf 'unknown\n'
fi

for __f in {files}; do
  printf '@@%s\n' "$__f"
  if [ ! -e "$__dir/$__f" ]; then
    printf '<missing>\n'
  elif __out=$(cat "$__dir/$__f" 2>/dev/null); then
    if [ -n "$__out" ]; then printf '%s\n' "$__out"; else printf '<empty>\n'; fi
  else
    printf '<unreadable>\n'
  fi
done

printf '@@meminfo\n'
if __out=$(awk '/^Mem(Total|Available):/ {print $1, $2}' "$__proc/meminfo" 2>/dev/null); then
  if [ -n "$__out" ]; then printf '%s\n' "$__out"; else printf '<empty>\n'; fi
else
  printf '<unreadable>\n'
fi

printf '@@cpuinfo_processors\n'
__cpus=$(awk '/^processor[ \t]*:/ {c++} END {print c+0}' "$__proc/cpuinfo" 2>/dev/null)
if [ -n "$__cpus" ]; then printf '%s\n' "$__cpus"; else printf '<unreadable>\n'; fi

# Counted with globs, not `ps | wc -l`: `ps` is absent from slim images, and a pipe would report
# the status of `wc` for a `ps` that never ran - an empty answer dressed up as a clean one.
printf '@@process_count\n'
__n=0
for __d in "$__proc"/[0-9]*; do
  if [ -d "$__d" ]; then __n=$((__n + 1)); fi
done
printf '%s\n' "$__n"

printf '@@thread_count\n'
__t=0
for __d in "$__proc"/[0-9]*/task/*; do
  if [ -d "$__d" ]; then __t=$((__t + 1)); fi
done
printf '%s\n' "$__t"

printf '@@end\n'
"""

#: The probe is read-only by construction: ``cat``/``awk`` over ``/sys/fs/cgroup`` and ``/proc``,
#: plus directory globs to count tasks. Nothing is written, killed, or created.
CGROUP_PROBE_COMMAND: str = _PROBE_TEMPLATE.replace("{files}", " ".join(_CGROUP_FILES)).strip()

#: Section sentinels the probe prints instead of content, each meaning a different unknown.
_SENTINELS = frozenset({"<missing>", "<unreadable>", "<empty>"})

_END_SENTINEL = "end"


# --------------------------------------------------------------------------- probe parsing


def parse_probe_sections(stdout: str) -> dict[str, Optional[str]]:
    """Split probe output into ``{section: text}``.

    The value is ``None`` when the probe could not read that file (the ``<missing>``,
    ``<unreadable>`` and ``<empty>`` sentinels), and the key is absent when the probe never
    printed the section at all. Both are different from a value of ``"0"``, which is why the
    caller is never handed a defaulted dict.
    """
    sections: dict[str, Optional[str]] = {}
    name: Optional[str] = None
    buffer: list[str] = []

    def flush() -> None:
        if name is None:
            return
        body = "\n".join(buffer).strip()
        sections[name] = None if body in _SENTINELS else body

    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("@@"):
            flush()
            name = stripped[2:].strip()
            buffer = []
        elif name is not None:
            buffer.append(line)
    flush()
    return sections


def probe_is_complete(stdout: str, sections: dict[str, Optional[str]]) -> bool:
    """True when the probe printed its closing sentinel.

    A truncated capture (the Jupyter terminal keeps the tail of an over-long stream and says so)
    would otherwise be read as "the files after the cut are missing", which is an invented finding
    about the notebook rather than a fact about the capture.
    """
    return _END_SENTINEL in sections or f"@@{_END_SENTINEL}" in stdout


# --------------------------------------------------------------------------- value helpers


def _first_token(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    for line in value.splitlines():
        for token in line.split():
            return token
    return None


def _parse_int(value: Optional[str]) -> Optional[int]:
    token = _first_token(value)
    if token is None:
        return None
    try:
        return int(token)
    except ValueError:
        return None


def _parse_key_values(value: Optional[str]) -> Optional[dict[str, int]]:
    """Parse ``key value`` lines (``memory.events``, ``memory.stat``, ``cpu.stat``)."""
    if value is None:
        return None
    parsed: dict[str, int] = {}
    for line in value.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            parsed[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return parsed


def _bytes_to_gib(value: Optional[int]) -> Optional[float]:
    return None if value is None else round(value / GIB, 2)


def _percent(part: Optional[int], whole: Optional[int]) -> Optional[float]:
    if part is None or whole is None or whole <= 0:
        return None
    return round(100.0 * part / whole, 1)


def _ceiling_level(used_percent: Optional[float]) -> str:
    """Classify a used/limit percentage, with ``unknown`` for a percentage we could not compute.

    ``unknown`` is deliberately not folded into ``ok``: an unmeasurable ceiling is not a safe one.
    """
    if used_percent is None:
        return "unknown"
    if used_percent >= CRITICAL_PERCENT:
        return "critical"
    if used_percent >= WARN_PERCENT:
        return "warning"
    return "ok"


# --------------------------------------------------------------------------- report building


def _limit_state(raw: Optional[str], parsed: Optional[int]) -> str:
    """``capped`` / ``unlimited`` / ``unknown`` - the three are different answers.

    ``memory.max`` really does contain the literal ``max`` when nothing is capped, so "unlimited"
    is an observation and never a default. A file we could not read is ``unknown``, which is what
    keeps "no limit reported" from reading as "no limit set".
    """
    if raw is None:
        return "unknown"
    if parsed is None:
        return "unlimited" if "max" in raw.split() else "unknown"
    return "capped"


def build_enforced_report(
    sections: dict[str, Optional[str]],
    *,
    platform_declaration: Optional[dict[str, Any]] = None,
    complete: bool = True,
) -> dict[str, Any]:
    """Turn probe sections (plus the platform's declaration) into the command's payload.

    Pure: no I/O, so a fixture tree can be pushed through the same code a live notebook goes
    through. ``warnings`` and ``alarms`` carry every judgement in prose, and ``verdict`` is
    ``alarm`` / ``warning`` / ``ok`` / ``unknown``.
    """
    warnings: list[str] = []
    alarms: list[str] = []

    version = sections.get("cgroup_version") or "unknown"
    if version == "v1":
        warnings.append(
            "cgroup v1 layout detected: this reader parses the v2 unified tree only, so every "
            "limit below is unknown rather than absent. Read "
            "memory.limit_in_bytes, memory.usage_in_bytes, memory.failcnt, "
            "cpu.cfs_quota_us/cpu.cfs_period_us and pids.max by hand on this host."
        )
    elif version == "unknown":
        warnings.append(
            "neither a cgroup v2 unified tree nor a v1 memory controller directory was found, so "
            "no limit could be read; nothing here should be read as 'uncapped'"
        )

    # --- memory -------------------------------------------------------------------
    memory_raw = sections.get("memory.max")
    memory_limit = _parse_int(memory_raw)
    limit_state = _limit_state(memory_raw, memory_limit)
    memory_current = _parse_int(sections.get("memory.current"))
    memory_high = _parse_int(sections.get("memory.high"))
    high_state = _limit_state(sections.get("memory.high"), memory_high)
    memory_peak = _parse_int(sections.get("memory.peak"))
    memory_stat = _parse_key_values(sections.get("memory.stat")) or {}
    used_percent = _percent(memory_current, memory_limit) if limit_state == "capped" else None

    for name, raw in (
        ("memory.max", memory_raw),
        ("memory.current", sections.get("memory.current")),
    ):
        if raw is None:
            warnings.append(f"{name} could not be read, so memory headroom is unknown, not zero")

    memory: dict[str, Any] = {
        "limit_state": limit_state,
        "limit_bytes": memory_limit if limit_state != "unlimited" else None,
        "limit_gib": _bytes_to_gib(memory_limit) if limit_state != "unlimited" else None,
        "high_bytes": memory_high,
        "high_state": high_state,
        "current_bytes": memory_current,
        "peak_bytes": memory_peak,
        "anon_bytes": memory_stat.get("anon"),
        "file_bytes": memory_stat.get("file"),
        "used_percent": used_percent,
        "ceiling_level": _ceiling_level(used_percent),
    }
    if limit_state == "unlimited":
        warnings.append(
            "memory.max is 'max': this cgroup has no memory cap of its own, so the host's memory "
            "is the real ceiling and any sibling in a wider cgroup can take it"
        )
    if memory_high is not None and memory_limit is not None and memory_high <= memory_limit:
        warnings.append(
            f"memory.high is {_bytes_to_gib(memory_high)} GiB, at or below memory.max: the kernel "
            "throttles and reclaims hard at the high watermark before it ever reaches the hard limit"
        )
    if memory["ceiling_level"] == "critical":
        warnings.append(
            f"memory.current is {used_percent}% of memory.max (>= {CRITICAL_PERCENT:.0f}%): an "
            "allocation from here is likely to be answered by the OOM-killer, not by memory"
        )
    elif memory["ceiling_level"] == "warning":
        warnings.append(
            f"memory.current is {used_percent}% of memory.max (>= {WARN_PERCENT:.0f}%): headroom "
            "is thin enough that one more worker can trigger an OOM kill"
        )
    if memory_peak is not None and limit_state == "capped" and used_percent is not None:
        peak_percent = _percent(memory_peak, memory_limit)
        memory["peak_percent"] = peak_percent
        if (
            peak_percent is not None
            and peak_percent > used_percent + 5.0
            and peak_percent >= WARN_PERCENT
        ):
            warnings.append(
                f"memory.peak reached {peak_percent}% of memory.max while current use is "
                f"{used_percent}%: this cgroup has already been near the ceiling and backed off"
            )

    # --- memory.events -------------------------------------------------------------
    events = _parse_key_values(sections.get("memory.events"))
    if events is None:
        warnings.append(
            "memory.events could not be read: the only surviving trace of a past OOM kill is in "
            "that file, so an OOM in this container would be invisible to this command"
        )
    oom_kill = int(events.get("oom_kill", 0)) if events else 0
    if events:
        if oom_kill > 0:
            alarms.append(
                f"memory.events oom_kill = {oom_kill}: the kernel OOM-killer has run {oom_kill} "
                "time(s) in this cgroup since it was created (the counter is cumulative, so it "
                "does not say when). Nothing inside the container records which process died."
            )
        if events.get("max", 0) > 0:
            warnings.append(
                f"memory.events max = {events['max']}: the cgroup hit memory.max that many times "
                "and was forced to reclaim instead of being given memory"
            )
        if events.get("high", 0) > 0:
            warnings.append(
                f"memory.events high = {events['high']}: the cgroup crossed memory.high, so work "
                "ran throttled in kernel reclaim rather than failing"
            )
        if events.get("oom", 0) > 0:
            warnings.append(
                f"memory.events oom = {events['oom']}: an allocation inside the cgroup could not "
                "be satisfied even after reclaim"
            )

    # --- cpu -----------------------------------------------------------------------
    cpu_raw = sections.get("cpu.max")
    cpu_quota: Optional[int] = None
    cpu_period: Optional[int] = None
    cpu_limit_cores: Optional[float] = None
    if cpu_raw is not None:
        tokens = (cpu_raw.split() + [""])[:2]
        if tokens[0].isdigit():
            cpu_quota = int(tokens[0])
        if tokens[1].isdigit():
            cpu_period = int(tokens[1])
        if cpu_quota is not None and cpu_period:
            cpu_limit_cores = round(cpu_quota / cpu_period, 2)
    cpu_unlimited = cpu_raw is not None and cpu_limit_cores is None and "max" in cpu_raw.split()
    cpu_stat = _parse_key_values(sections.get("cpu.stat")) or {}
    nr_periods = cpu_stat.get("nr_periods")
    nr_throttled = cpu_stat.get("nr_throttled")
    throttled_percent = _percent(nr_throttled, nr_periods)

    cpu: dict[str, Any] = {
        "state": "unknown" if cpu_raw is None else ("unlimited" if cpu_unlimited else "capped"),
        "quota": cpu_quota,
        "period": cpu_period,
        "limit_cores": cpu_limit_cores,
        "nr_periods": nr_periods,
        "nr_throttled": nr_throttled,
        "throttled_usec": cpu_stat.get("throttled_usec"),
        "throttled_percent": throttled_percent,
        "cpuinfo_processors": _parse_int(sections.get("cpuinfo_processors")),
    }
    if cpu_raw is None:
        warnings.append(
            "cpu.max could not be read, so the CPU cap is unknown: the cpu controller is not "
            "delegated to this cgroup, and /proc/cpuinfo reports the host's CPU count, not a quota"
        )
    elif cpu_unlimited:
        warnings.append(
            "cpu.max is 'max': no CPU quota is enforced here, so this container competes for the "
            "host's CPUs and will be slowed by neighbours rather than throttled by its own quota"
        )
    if throttled_percent is not None and throttled_percent >= 5.0:
        warnings.append(
            f"{throttled_percent}% of scheduling periods were throttled ({nr_throttled} of "
            f"{nr_periods}): the workload already wants more CPU than cpu.max grants"
        )

    # --- pids ----------------------------------------------------------------------
    pids_max = _parse_int(sections.get("pids.max"))
    pids_current = _parse_int(sections.get("pids.current"))
    pids_peak = _parse_int(sections.get("pids.peak"))
    pids_percent = _percent(pids_current, pids_max)
    pids: dict[str, Any] = {
        "max": pids_max,
        "current": pids_current,
        "peak": pids_peak,
        "used_percent": pids_percent,
        "ceiling_level": _ceiling_level(pids_percent),
    }
    if sections.get("pids.max") is None:
        warnings.append("pids.max could not be read, so the process/thread ceiling is unknown")
    if pids["ceiling_level"] in {"warning", "critical"}:
        warnings.append(
            f"pids.current is {pids_percent}% of pids.max: a fork bomb of worker threads can fail "
            "with EAGAIN ('can't allocate memory' from pthread_create) long before memory runs out"
        )

    # --- the container's own view of memory ----------------------------------------
    meminfo_raw = sections.get("meminfo")
    meminfo_parsed: dict[str, int] = {}
    if meminfo_raw is not None:
        for line in meminfo_raw.splitlines():
            parts = line.replace(":", " ").split()
            if len(parts) >= 2 and parts[1].isdigit():
                # /proc/meminfo values are KiB; report bytes like every other memory field.
                meminfo_parsed[parts[0]] = int(parts[1]) * 1024
    memtotal = meminfo_parsed.get("MemTotal")
    container_view = {
        "meminfo_memtotal_bytes": memtotal,
        "meminfo_memtotal_gib": _bytes_to_gib(memtotal),
        "meminfo_memavailable_bytes": meminfo_parsed.get("MemAvailable"),
        "memtotal_vs_limit_ratio": (
            round(memtotal / memory_limit, 2)
            if memtotal and limit_state == "capped" and memory_limit
            else None
        ),
    }
    if memtotal is None:
        warnings.append(
            "MemTotal could not be read from /proc/meminfo, so the container's own view of memory "
            "is unknown and this command cannot say whether it disagrees with the cgroup"
        )
    elif limit_state == "capped" and memory_limit:
        ratio = memtotal / memory_limit
        if ratio > 1.05:
            warnings.append(
                f"/proc/meminfo MemTotal is {_bytes_to_gib(memtotal)} GiB but memory.max caps this "
                f"container at {_bytes_to_gib(memory_limit)} GiB ({ratio:.1f}x). free(1), "
                "MemAvailable-based allocators and most frameworks read /proc/meminfo, so anything "
                "sized from it will over-allocate and be OOM-killed. Size from memory.max."
            )
        elif ratio < 0.95:
            warnings.append(
                f"/proc/meminfo MemTotal is {_bytes_to_gib(memtotal)} GiB, below the memory.max cap "
                f"of {_bytes_to_gib(memory_limit)} GiB: the host exposes less than the cgroup "
                "allows, so the smaller of the two is the real ceiling"
            )

    # --- platform declaration ------------------------------------------------------
    # Every disagreement recorded here is a *warning* inside this source and an alarm for the
    # merged verdict: the source's own report says what it noticed, and the command decides what
    # a caller has to act on. One comparison, two renderings, so the two can never drift apart.
    platform: Optional[dict[str, Any]] = None
    declaration_drift: list[dict[str, Any]] = []
    if platform_declaration is not None:
        platform = dict(platform_declaration)
        declared_gib = platform.get("memory_gib")
        if declared_gib and limit_state == "capped" and memory_limit is not None:
            declared_bytes = float(declared_gib) * GIB
            drift = abs(memory_limit - declared_bytes) / declared_bytes
            if drift > MEMORY_DRIFT_TOLERANCE:
                warnings.append(
                    f"the platform declares {declared_gib} GiB for this instance while the "
                    f"container's memory.max is {_bytes_to_gib(memory_limit)} GiB: one of the two "
                    "is stale, and the cgroup is the one the kernel enforces"
                )
                declaration_drift.append(
                    {
                        "resource": "memory",
                        "declared": f"{declared_gib} GiB",
                        "enforced": f"{_bytes_to_gib(memory_limit)} GiB (memory.max)",
                    }
                )
        declared_cpu = platform.get("cpu_count")
        if declared_cpu and cpu_limit_cores is not None:
            if abs(cpu_limit_cores - float(declared_cpu)) / float(declared_cpu) > (
                CPU_DRIFT_TOLERANCE
            ):
                warnings.append(
                    f"the platform declares {declared_cpu} CPU while cpu.max grants "
                    f"{cpu_limit_cores} cores: the quota, not the declaration, is what runs"
                )
                declaration_drift.append(
                    {
                        "resource": "cpu",
                        "declared": f"{declared_cpu} CPU",
                        "enforced": f"{cpu_limit_cores} cores (cpu.max)",
                    }
                )

    # --- is the workload actually running -----------------------------------------
    process_count = _parse_int(sections.get("process_count"))
    thread_count = _parse_int(sections.get("thread_count"))
    processes = {"count": process_count, "thread_count": thread_count}
    if process_count is None:
        warnings.append(
            "the process count could not be read, so 'is my workload running' is unanswered"
        )
    elif (
        thread_count is not None
        and pids_current is not None
        and pids_current > 2 * max(thread_count, 1)
    ):
        warnings.append(
            f"pids.current ({pids_current}) is far above the {thread_count} threads this PID "
            "namespace can see: the cgroup is shared with tasks outside this container, so pids "
            "headroom is not yours alone"
        )

    if not complete:
        warnings.append(
            "the probe output did not reach its end sentinel, so it was truncated in transit and "
            "any file named after the cut is unobserved rather than missing"
        )

    if alarms:
        verdict = "alarm"
    elif warnings:
        verdict = "warning"
    else:
        # "ok" only for a tree we actually read as v2; anything else said nothing.
        verdict = "ok" if version == "v2" else "unknown"

    return {
        "cgroup": {
            "version": version,
            "root": sections.get("cgroup_root"),
            "dir": sections.get("cgroup_dir"),
            "self_cgroup": sections.get("self_cgroup"),
        },
        "memory": memory,
        "memory_events": events,
        "memory_events_local": _parse_key_values(sections.get("memory.events.local")),
        "oom_kill": oom_kill,
        "cpu": cpu,
        "pids": pids,
        "processes": processes,
        "container_memory_view": container_view,
        "platform": platform,
        "declaration_drift": declaration_drift,
        "thresholds": {"warn_percent": WARN_PERCENT, "critical_percent": CRITICAL_PERCENT},
        "verdict": verdict,
        "warnings": warnings,
        "alarms": alarms,
    }


def platform_declaration_from_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Pull the platform's own declaration out of a notebook detail record.

    `quota` is what the instance was given; `start_config.shared_memory_size` is the SHM size it
    was created with. Both are declarations, not measurements - which is the point of printing
    them beside the cgroup instead of instead of them.
    """
    quota = detail.get("quota") or {}
    start_config = detail.get("start_config") or {}
    shm = start_config.get("shared_memory_size") or 0
    return {
        "memory_gib": quota.get("memory_size"),
        "cpu_count": quota.get("cpu_count"),
        "shm_gib": shm or None,
        "gpu_count": quota.get("gpu_count"),
        "source": "notebook status (platform declaration, not a measurement)",
    }


# --------------------------------------------------------------------------- formatting


def _gib_text(value: Optional[float]) -> str:
    return "unknown" if value is None else f"{value:.1f} GiB"


#: The kernel's own order, so the events line reads like the file instead of alphabetically.
_EVENT_ORDER = ("low", "high", "max", "oom", "oom_kill", "oom_group_kill")


def _events_text(events: dict[str, int]) -> str:
    ordered = [key for key in _EVENT_ORDER if key in events]
    ordered += sorted(key for key in events if key not in _EVENT_ORDER)
    return " ".join(f"{key}={events[key]}" for key in ordered)


def _mark(level: str) -> str:
    if level == "critical":
        return click.style("CRITICAL", fg="red", bold=True)
    if level in {"warning", "unknown"}:
        return click.style(level.upper(), fg="yellow")
    return click.style("ok", fg="green")


def format_enforced_lines(report: dict[str, Any]) -> list[str]:
    """Render the enforced source's own block: the cgroup tree, field by labelled field.

    The alarms and warnings are the merged report's to print, once, and the platform's declaration
    is the pairing table's to print: this block holds only what the container's own files say. The
    prose itself still has to survive being pasted into a ticket, because the incident this command
    exists for was an unobservable one.
    """
    memory = report["memory"]
    cpu = report["cpu"]
    pids = report["pids"]
    processes = report["processes"]
    events = report["memory_events"] or {}
    view = report["container_memory_view"]

    def field(label: str, value: str) -> str:
        return f"  {label:<15}: {value}"

    lines: list[str] = [
        field(
            "Cgroup",
            f"{report['cgroup'].get('dir') or 'unknown'} "
            f"(cgroup {report['cgroup'].get('version') or 'unknown'} layout)",
        ),
    ]

    limit_text = (
        "no cap (memory.max = max)"
        if memory["limit_state"] == "unlimited"
        else (
            f"{_gib_text(memory['limit_gib'])} (memory.max = {memory['limit_bytes']})"
            if memory["limit_state"] == "capped"
            else "unknown (memory.max unreadable)"
        )
    )
    lines.append(field("Memory limit", limit_text))

    high = memory["high_bytes"]
    lines.append(
        field(
            "Memory high",
            (
                _gib_text(_bytes_to_gib(high))
                if high is not None
                else (
                    "max (unused)"
                    if memory.get("high_state") == "unlimited"
                    else "unknown (memory.high unreadable)"
                )
            ),
        )
    )

    used = memory["used_percent"]
    used_text = (
        f"{_gib_text(_bytes_to_gib(memory['current_bytes']))} of "
        f"{_gib_text(memory['limit_gib'])} - {used}%"
        if used is not None
        else f"{_gib_text(_bytes_to_gib(memory['current_bytes']))} used, no cap to divide by"
    )
    lines.append(field("Memory used", f"{used_text}  [{_mark(memory['ceiling_level'])}]"))
    if memory.get("anon_bytes") is not None:
        lines.append(
            field(
                "Of which anon",
                f"{_gib_text(_bytes_to_gib(memory['anon_bytes']))} (page cache "
                f"{_gib_text(_bytes_to_gib(memory['file_bytes']))} is reclaimable)",
            )
        )
    if memory.get("peak_bytes") is not None:
        lines.append(
            field(
                "Memory peak",
                _gib_text(_bytes_to_gib(memory["peak_bytes"]))
                + (
                    f" - {memory['peak_percent']}% of the cap"
                    if memory.get("peak_percent") is not None
                    else ""
                ),
            )
        )

    if cpu["state"] == "capped" and cpu["limit_cores"] is not None:
        quota_note = ""
        host_cpus = cpu.get("cpuinfo_processors")
        if host_cpus and cpu["limit_cores"] and host_cpus > 1.25 * cpu["limit_cores"]:
            # The CPU twin of the MemTotal lie: /proc/cpuinfo is the host's, and it is what
            # multiprocessing.cpu_count() and every thread-pool default read.
            quota_note = f" (/proc/cpuinfo shows {host_cpus} CPUs; the quota is what runs)"
        lines.append(
            field(
                "CPU quota",
                f"{cpu['limit_cores']} cores (cpu.max = {cpu['quota']} {cpu['period']}){quota_note}",
            )
        )
    else:
        lines.append(
            field(
                "CPU quota",
                "unlimited" if cpu["state"] == "unlimited" else "unknown (cpu.max unreadable)",
            )
        )
    if cpu.get("throttled_percent") is not None:
        lines.append(
            field(
                "CPU throttled",
                f"{cpu['throttled_percent']}% of periods "
                f"({cpu['nr_throttled']} of {cpu['nr_periods']})",
            )
        )

    lines.append(
        field(
            "PIDs",
            (
                f"{pids['current']} / {pids['max']} - {pids['used_percent']}%"
                if pids["used_percent"] is not None
                else f"{pids['current']} current, pids.max unknown"
            )
            + f"  [{_mark(pids['ceiling_level'])}]",
        )
    )
    lines.append(
        field(
            "Processes",
            f"{processes['count']} processes, {processes['thread_count']} threads",
        )
    )
    lines.append(
        field(
            "memory.events",
            _events_text(events) if events else "unreadable - a past OOM kill would be invisible",
        )
    )

    lines.append("")
    lines.append("Cross-check (what the container believes / what enforces / what is declared)")
    lines.append(
        field(
            "/proc/meminfo",
            f"{_gib_text(view['meminfo_memtotal_gib'])} MemTotal"
            + (
                f" - {view['memtotal_vs_limit_ratio']}x the cgroup cap"
                if view.get("memtotal_vs_limit_ratio") is not None
                else ""
            ),
        )
    )
    return lines


def _fetch_notebook_detail(session: Any, notebook_id: str) -> dict[str, Any]:
    """The platform's declaration for this instance, raised rather than swallowed.

    Raising is the point: the caller reports a silent platform as the *declaration* source being
    unreachable, and still reads the container's own files over the Jupyter shell, which the
    declaration endpoint has nothing to do with.
    """
    from inspire.platform.web import browser_api as browser_api_module

    return browser_api_module.get_notebook_detail(notebook_id=notebook_id, session=session) or {}


def _run_probe_in_notebook(session: Any, notebook_id: str, timeout: int) -> tuple[str, int]:
    """Run the probe through the notebook's Jupyter shell, reusing the exec plumbing.

    Failures raise on purpose: a shell that will not answer makes the **enforced** source
    unreachable, and the sections that can still answer do. Turning that into a report line is the
    command's job, not this function's.
    """
    from inspire.platform.web.browser_api.notebook_shell import execute_notebook_shell_command

    result = execute_notebook_shell_command(
        notebook_id=notebook_id,
        command=CGROUP_PROBE_COMMAND,
        session=session,
        timeout=timeout,
    )
    return result.stdout or "", result.returncode


# --------------------------------------------------------------------------- live source: the bridge

#: The one GPU question asked of the host, in the shape nvidia-smi answers it.
_NVIDIA_SMI_QUERY = (
    "nvidia-smi "
    "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu "
    "--format=csv,noheader,nounits"
)

_GPU_TIMEOUT_SECONDS = 15
# The CPU probe sleeps for its own sample window and then walks /proc, so it gets the window
# plus room for a slow shell startup on a loaded node.
_CPU_TIMEOUT_SECONDS = 25


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bridge_notebook_id(bridge: BridgeProfile) -> str:
    return str(getattr(bridge, "notebook_id", "") or "").strip()


def _select_target_bridges(
    ctx: Context,
    *,
    tunnel_config: TunnelConfig,
    bridge_name: Optional[str],
) -> list[BridgeProfile]:
    if bridge_name:
        bridge = tunnel_config.get_bridge(bridge_name)
        if bridge is None:
            _handle_error(
                ctx,
                "ConfigError",
                f"Bridge '{bridge_name}' not found.",
                EXIT_CONFIG_ERROR,
                hint="Run 'inspire tunnel list' to see saved bridge profiles.",
            )
        notebook_id = _bridge_notebook_id(bridge)
        if not notebook_id:
            _handle_error(
                ctx,
                "ConfigError",
                (f"Bridge '{bridge_name}' is not notebook-backed (missing notebook_id metadata)."),
                EXIT_CONFIG_ERROR,
                hint="Recreate it with 'inspire notebook ssh <notebook-id> --save-as <name>'.",
            )
        return [bridge]

    bridges = [b for b in tunnel_config.list_bridges() if _bridge_notebook_id(b)]
    if not bridges:
        _handle_error(
            ctx,
            "ConfigError",
            "No notebook-backed tunnel profiles found.",
            EXIT_CONFIG_ERROR,
            hint=("Create one first: 'inspire notebook ssh <notebook-id> --save-as <name>'."),
        )

    return sorted(bridges, key=lambda b: b.name)


def _parse_smi_float(value: str) -> float:
    token = value.strip()
    if not token or token.upper() == "N/A":
        return 0.0
    return float(token)


def _parse_smi_int(value: str) -> int:
    token = value.strip()
    if not token or token.upper() == "N/A":
        return 0
    return int(float(token))


def _parse_nvidia_smi_output(stdout: str, stderr: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        stderr_msg = stderr.strip()
        if stderr_msg:
            raise ValueError(stderr_msg.splitlines()[0])
        raise ValueError("No GPU rows returned from nvidia-smi.")

    gpus: list[dict[str, Any]] = []
    reader = csv.reader(lines)
    for row in reader:
        if len(row) < 5:
            raise ValueError("Unexpected nvidia-smi output format.")
        try:
            gpus.append(
                {
                    "index": _parse_smi_int(row[0]),
                    "util_percent": _parse_smi_float(row[1]),
                    "mem_used_mib": _parse_smi_int(row[2]),
                    "mem_total_mib": _parse_smi_int(row[3]),
                    "temp_c": _parse_smi_float(row[4]),
                }
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Unable to parse nvidia-smi output.") from exc

    if not gpus:
        raise ValueError("No GPU devices detected.")
    return gpus


def _aggregate_gpu_metrics(gpus: list[dict[str, Any]]) -> dict[str, Any]:
    gpu_count = len(gpus)
    mem_used = int(sum(int(g["mem_used_mib"]) for g in gpus))
    mem_total = int(sum(int(g["mem_total_mib"]) for g in gpus))
    util_avg = (
        round(sum(float(g["util_percent"]) for g in gpus) / gpu_count, 1) if gpu_count else 0.0
    )
    mem_used_percent = round((100.0 * mem_used / mem_total), 1) if mem_total > 0 else 0.0
    return {
        "gpu_count": gpu_count,
        "util_avg_percent": util_avg,
        "mem_used_total_mib": mem_used,
        "mem_total_mib": mem_total,
        "mem_used_percent": mem_used_percent,
    }


def _first_line(text: str, fallback: str) -> str:
    cleaned = (text or "").strip()
    return cleaned.splitlines()[0] if cleaned else fallback


def _run_remote_probe(
    bridge: BridgeProfile,
    *,
    tunnel_config: TunnelConfig,
    command: str,
    timeout: int,
    timeout_message: str,
) -> tuple[Optional[subprocess.CompletedProcess], Optional[str]]:
    """Run *command* on the bridge. Returns ``(completed, transport_error)``.

    ``transport_error`` is set only when nothing came back at all (timeout, ssh failure); a
    command that ran and failed is returned intact so the caller can read *why* from stderr.
    """
    try:
        completed = run_ssh_command(
            command,
            bridge_name=bridge.name,
            config=tunnel_config,
            timeout=timeout,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        return None, timeout_message
    except Exception as exc:  # pragma: no cover - defensive guard around subprocess path
        return None, str(exc)
    return completed, None


def _collect_gpu_metrics(
    bridge: BridgeProfile,
    *,
    tunnel_config: TunnelConfig,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Return ``(gpus, error)`` from nvidia-smi; ``gpus`` is empty whenever there is no GPU read."""
    completed, transport_error = _run_remote_probe(
        bridge,
        tunnel_config=tunnel_config,
        command=_NVIDIA_SMI_QUERY,
        timeout=_GPU_TIMEOUT_SECONDS,
        timeout_message="Timed out while running nvidia-smi.",
    )
    if transport_error or completed is None:
        return [], transport_error or "nvidia-smi returned no result."

    if getattr(completed, "returncode", 1) != 0:
        stderr = getattr(completed, "stderr", "") or ""
        stdout = getattr(completed, "stdout", "") or ""
        return [], _first_line(stderr or stdout, f"Remote command failed ({completed.returncode}).")

    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    try:
        return _parse_nvidia_smi_output(stdout, stderr), None
    except ValueError as exc:
        return [], str(exc)


def _collect_cpu_metrics(
    bridge: BridgeProfile,
    *,
    tunnel_config: TunnelConfig,
    processes: int,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Return ``(cpu_view, error)`` from the read-only /proc probe on the same host."""
    completed, transport_error = _run_remote_probe(
        bridge,
        tunnel_config=tunnel_config,
        command=build_host_proc_probe(processes=processes),
        timeout=_CPU_TIMEOUT_SECONDS,
        timeout_message="Timed out while sampling /proc on the notebook host.",
    )
    if transport_error or completed is None:
        return None, transport_error or "CPU probe returned no result."

    if getattr(completed, "returncode", 1) != 0:
        stderr = getattr(completed, "stderr", "") or ""
        stdout = getattr(completed, "stdout", "") or ""
        return None, _first_line(stderr or stdout, f"CPU probe failed ({completed.returncode}).")

    stdout = getattr(completed, "stdout", "") or ""
    payload = parse_host_proc_probe(stdout)
    if not has_host_telemetry(payload):
        return None, _first_line(
            stdout or (getattr(completed, "stderr", "") or ""),
            "No /proc telemetry returned from the host.",
        )
    return payload, None


def _collect_bridge_metrics(
    bridge: BridgeProfile,
    *,
    tunnel_config: TunnelConfig,
    no_check: bool,
    processes: int,
) -> dict[str, Any]:
    notebook_id = _bridge_notebook_id(bridge)
    result: dict[str, Any] = {
        "bridge": bridge.name,
        "notebook_id": notebook_id,
        "connected": False,
        # "gpu" when nvidia-smi answered, "cpu" for the /proc fallback, None when nothing did.
        "mode": None,
        "gpus": [],
        "aggregate": None,
        "cpu": None,
        # Why the GPU probe stayed silent. Kept even when the CPU view succeeds, so a driver
        # problem on a GPU host is reported rather than quietly answered with CPU numbers.
        "gpu_error": None,
        "error": None,
    }

    if not no_check:
        tunnel_ready = is_tunnel_available(
            bridge_name=bridge.name,
            config=tunnel_config,
            retries=0,
            retry_pause=0.0,
            progressive=False,
        )
        if not tunnel_ready:
            result["error"] = "SSH tunnel is not responding."
            return result

    gpus, gpu_error = _collect_gpu_metrics(bridge, tunnel_config=tunnel_config)
    if gpus:
        result["connected"] = True
        result["mode"] = "gpu"
        result["gpus"] = gpus
        result["aggregate"] = _aggregate_gpu_metrics(gpus)
        return result

    # No GPU read. That is the normal shape of a CPU-only pod, not an outage, so ask the same
    # host what its CPUs and memory are doing. A hung nvidia-smi still gets the CPU probe: the
    # two reads share no code path on the far side, so the second one is not a repeat of the
    # first - only an extra round trip on a host that is genuinely unreachable with --no-check.
    result["gpu_error"] = gpu_error
    cpu, cpu_error = _collect_cpu_metrics(bridge, tunnel_config=tunnel_config, processes=processes)
    if cpu is not None:
        result["connected"] = True
        result["mode"] = "cpu"
        result["cpu"] = cpu
        return result

    result["error"] = cpu_error or gpu_error or "No telemetry returned from this host."
    return result


def _collect_all_metrics(
    bridges: list[BridgeProfile],
    *,
    tunnel_config: TunnelConfig,
    no_check: bool,
    processes: int,
) -> list[dict[str, Any]]:
    if len(bridges) == 1:
        return [
            _collect_bridge_metrics(
                bridges[0],
                tunnel_config=tunnel_config,
                no_check=no_check,
                processes=processes,
            )
        ]

    results: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(bridges)) as pool:
        futures = {
            pool.submit(
                _collect_bridge_metrics,
                bridge,
                tunnel_config=tunnel_config,
                no_check=no_check,
                processes=processes,
            ): bridge.name
            for bridge in bridges
        }
        for future in concurrent.futures.as_completed(futures):
            bridge_name = futures[future]
            try:
                results[bridge_name] = future.result()
            except Exception as exc:  # pragma: no cover - defensive guard around thread pool
                bridge = next((b for b in bridges if b.name == bridge_name), None)
                results[bridge_name] = {
                    "bridge": bridge_name,
                    "notebook_id": _bridge_notebook_id(bridge) if bridge else "",
                    "connected": False,
                    "mode": None,
                    "gpus": [],
                    "aggregate": None,
                    "cpu": None,
                    "gpu_error": None,
                    "error": str(exc),
                }

    return [results[bridge.name] for bridge in bridges if bridge.name in results]


def _build_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    """Count what was sampled. A CPU-only host that answered is a success, not a failure."""
    gpu = sum(1 for item in items if item.get("mode") == "gpu")
    cpu = sum(1 for item in items if item.get("mode") == "cpu")
    ok = gpu + cpu
    return {"total": len(items), "ok": ok, "failed": len(items) - ok, "gpu": gpu, "cpu": cpu}


def sample_live(
    ctx: Context,
    *,
    tunnel_config: TunnelConfig,
    targets: list[BridgeProfile],
    no_check: bool,
    processes: int,
) -> dict[str, Any]:
    """One sample of the live sections, for a single run or one round of ``--watch``."""
    payload: dict[str, Any] = {
        "timestamp": _utc_timestamp(),
        "items": _collect_all_metrics(
            targets,
            tunnel_config=tunnel_config,
            no_check=no_check,
            processes=processes,
        ),
    }
    payload["summary"] = _build_summary(payload["items"])
    return payload


def _short_notebook_id(notebook_id: str) -> str:
    text = (notebook_id or "").strip()
    if not text:
        return "-"
    if len(text) <= 14:
        return text
    return f"{text[:11]}..."


def _human_size(mib: float) -> str:
    """Render a size in GiB at or above 1 GiB, MiB below it."""
    if mib >= 1024.0:
        return f"{mib / 1024.0:.1f} GiB"
    return f"{mib:.1f} MiB"


def _format_mem_value(used_mib: float, total_mib: float, used_percent: float) -> str:
    """Render ``used/total (percent)`` for the CPU view; the unit is always printed."""
    if max(used_mib, total_mib) >= 1024.0:
        body = f"{used_mib / 1024.0:.1f}/{total_mib / 1024.0:.1f} GiB"
    else:
        body = f"{used_mib:.1f}/{total_mib:.1f} MiB"
    return f"{body} ({used_percent:.1f}%)"


def _format_cpu_view(item: dict[str, Any]) -> list[str]:
    """Render the CPU view block for one bridge, clearly labelled as CPU."""
    cpu = item.get("cpu") or {}
    bridge = str(item.get("bridge", ""))
    notebook_id = _short_notebook_id(str(item.get("notebook_id", "")))

    def number(value: Any, spec: str = "{:.2f}") -> str:
        if value is None:
            return "-"
        try:
            return spec.format(float(value))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return "-"

    cores = cpu.get("cpu_count")
    load = "  ".join(
        value
        for value in (
            number(cpu.get("load_1m")),
            number(cpu.get("load_5m")),
            number(cpu.get("load_15m")),
        )
    )
    # Say why a GPU command became a CPU view: "no GPU here" and "the GPU driver is broken"
    # both land in this section, and only the reason tells them apart.
    gpu_error = str(item.get("gpu_error", "") or "").strip()
    title = f"CPU view - {bridge} ({notebook_id})"
    if gpu_error:
        title = f"{title}  [GPU probe: {gpu_error}]"
    lines = [
        title,
        (f"  Cores: {cores if cores not in (None, 0) else '-'}   " f"Load avg (1/5/15): {load}"),
    ]

    memory = cpu.get("memory") or {}
    if memory:
        lines.append(
            "  Memory: "
            f"{_human_size(float(memory.get('used_mib', 0.0)))} used / "
            f"{_human_size(float(memory.get('total_mib', 0.0)))} total "
            f"({number(memory.get('used_percent'), '{:.1f}')}%), "
            f"{_human_size(float(memory.get('available_mib', 0.0)))} available"
        )
    else:
        lines.append("  Memory: - (no MemTotal in the host's /proc/meminfo)")

    rows = cpu.get("processes") or []
    sample = number(cpu.get("sample_seconds"), "{:.1f}")
    lines.append(f"  Heaviest processes by RSS (CPU% sampled over {sample} s):")
    if not rows:
        lines.append("    -")
        return lines

    lines.append(f"{'PID':>8} {'RSS MiB':>10} {'CPU%':>6}  COMMAND")
    for row in rows:
        command = str(row.get("command", "") or "").strip() or "-"
        cpu_percent = row.get("cpu_percent")
        lines.append(
            f"{str(row.get('pid', '-')):>8} {number(row.get('rss_mib'), '{:.1f}'):>10} "
            f"{number(cpu_percent, '{:.1f}'):>6}  {command}"
        )

    lines.append(
        "  Scope: /proc on the notebook host. Load, cores and memory are node-wide unless the "
        "pod isolates them; the process list is the notebook's own."
    )
    return lines


def format_live_lines(payload: dict[str, Any]) -> str:
    """Render the live source: one row per bridge sampled, then the CPU view of each."""
    lines = [
        "Notebook Telemetry (tunnel-backed)",
        f"Sample: {payload['timestamp']}",
        "",
        (
            f"{'Bridge':<22} {'Notebook':<15} {'Kind':<4} {'GPUs':>4} "
            f"{'Util/Load':>10} {'Memory':>26} Status"
        ),
        "-" * 100,
    ]

    cpu_views: list[dict[str, Any]] = []

    for item in payload["items"]:
        bridge = str(item.get("bridge", ""))
        notebook_id = _short_notebook_id(str(item.get("notebook_id", "")))
        aggregate = item.get("aggregate") or {}
        cpu = item.get("cpu") or {}
        error = str(item.get("error", "") or "")
        mode = str(item.get("mode", "") or "")

        if mode == "gpu" and aggregate:
            gpus = int(aggregate.get("gpu_count", 0))
            util_avg = float(aggregate.get("util_avg_percent", 0.0))
            mem_used = int(aggregate.get("mem_used_total_mib", 0))
            mem_total = int(aggregate.get("mem_total_mib", 0))
            mem_pct = float(aggregate.get("mem_used_percent", 0.0))
            memory = f"{mem_used}/{mem_total} MiB ({mem_pct:.1f}%)"
            lines.append(
                f"{bridge:<22} {notebook_id:<15} {'GPU':<4} {gpus:>4} "
                f"{f'{util_avg:.1f}%':>10} {memory:>26} {click.style('ok', fg='green')}"
            )
            continue

        if mode == "cpu":
            memory_block = cpu.get("memory") or {}
            if memory_block:
                memory = _format_mem_value(
                    float(memory_block.get("used_mib", 0.0)),
                    float(memory_block.get("total_mib", 0.0)),
                    float(memory_block.get("used_percent", 0.0)),
                )
            else:
                memory = "-"
            load = cpu.get("load_1m")
            load_text = f"load {load:.1f}" if isinstance(load, (int, float)) else "-"
            cpu_views.append(item)
            lines.append(
                f"{bridge:<22} {notebook_id:<15} {'CPU':<4} {'-':>4} "
                f"{load_text:>10} {memory:>26} {click.style('ok', fg='green')}"
            )
            continue

        status = click.style("error", fg="red")
        reason = error or "Unknown failure"
        # Dashes, not zeros: this host answered nothing, so "0 GPUs" would be a claim we did
        # not earn and "0%" would read as an idle machine.
        lines.append(
            f"{bridge:<22} {notebook_id:<15} {'-':<4} {'-':>4} {'-':>10} {'-':>26} {status} {reason}"
        )

    for item in cpu_views:
        lines.append("")
        lines.extend(_format_cpu_view(item))

    summary = payload["summary"]
    lines.extend(
        [
            "",
            f"Summary: {summary['ok']}/{summary['total']} bridge(s) sampled "
            f"({summary['gpu']} GPU, {summary['cpu']} CPU).",
        ]
    )
    return "\n".join(lines)


def _live_load_line(payload: dict[str, Any]) -> Optional[str]:
    """The sampled CPU picture in one line, from the first bridge that answered."""
    for item in payload.get("items") or []:
        cpu = item.get("cpu") or {}
        if not cpu:
            continue

        def number(value: Any) -> str:
            if value is None:
                return "-"
            try:
                return f"{float(value):.2f}"
            except (TypeError, ValueError):  # pragma: no cover - defensive
                return "-"

        cores = cpu.get("cpu_count")
        core_text = f" over {cores} cores" if cores not in (None, 0) else ""
        return (
            f"load {number(cpu.get('load_1m'))} / {number(cpu.get('load_5m'))} / "
            f"{number(cpu.get('load_15m'))}{core_text} (node-wide unless the pod isolates it)"
        )
    return None


# --------------------------------------------------------------------------- history source

#: Units accepted by ``--since``, in seconds. A bare number is seconds, the unit the platform's
#: own metric API counts windows in.
_SINCE_UNITS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_since_window(value: str) -> int:
    """Turn ``--since 6h`` into seconds (21600).

    Deliberately narrow: a count with one optional unit, or a bare count of seconds. Anything
    else is refused rather than guessed at, because a window read wrong reads as a chart of the
    wrong day, and a chart is not something this command prints an alarm about.
    """
    text = str(value).strip().lower()
    match = re.fullmatch(r"(\d+)\s*([smhd])?", text) if text else None
    if match is None:
        raise ValueError(
            f"cannot read --since {value!r}; use a count of seconds (3600) or of s/m/h/d "
            "(45m, 6h, 2d)"
        )
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("--since must be a window longer than zero")
    return amount * _SINCE_UNITS[match.group(2) or "s"]


# --------------------------------------------------------------------------- the pairing


def _drift_alarm(record: dict[str, Any]) -> str:
    """Alarm prose for one recorded disagreement between the declaration and the cgroup."""
    return (
        f"the platform declares {record.get('declared')} {record.get('resource')} while the "
        f"container enforces {record.get('enforced')}: the instance is not the one that was "
        "declared, and the cgroup is the half the kernel runs - size, schedule and fan out from "
        "it, and treat the declaration as the claim to take back to the platform"
    )


def _pairing_rows(
    declaration: Optional[dict[str, Any]],
    enforced: Optional[dict[str, Any]],
    live: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The ``MEM USAGE / LIMIT`` pairing, extended one column to the platform's declaration.

    One row per resource, three columns, and no arithmetic across a column boundary: a row whose
    three cells disagree is the finding, which is the whole reason they are printed side by side
    instead of being reconciled into one number.
    """
    declared = declaration or {}
    memory = (enforced or {}).get("memory") or {}
    cpu = (enforced or {}).get("cpu") or {}
    pids = (enforced or {}).get("pids") or {}
    # Every cell starts as "not probed": a source that never spoke has an unanswered cell, which
    # is not the same claim as a cell that reads "no cap".
    not_probed = "not probed"
    memory_enforced = memory_in_use = cpu_enforced = cpu_in_use = not_probed
    pids_enforced = pids_in_use = not_probed

    if enforced is not None and memory.get("limit_state") == "capped":
        memory_enforced = f"{_gib_text(memory.get('limit_gib'))} (memory.max)"
    elif enforced is not None and memory.get("limit_state") == "unlimited":
        memory_enforced = "no cap (memory.max = max)"
    elif enforced is not None:
        memory_enforced = "unknown (memory.max unreadable)"

    if enforced is not None:
        current_text = _gib_text(_bytes_to_gib(memory.get("current_bytes")))
        used_percent = memory.get("used_percent")
        memory_in_use = (
            f"{current_text} ({used_percent}%)"
            if used_percent is not None
            else f"{current_text}, no cap to divide by"
        )

    if enforced is not None:
        if cpu.get("state") == "capped" and cpu.get("limit_cores") is not None:
            cpu_enforced = f"{cpu['limit_cores']} cores (cpu.max)"
        elif cpu.get("state") == "unlimited":
            cpu_enforced = "no quota (cpu.max = max)"
        else:
            cpu_enforced = "unknown (cpu.max unreadable)"
        load_line = _live_load_line(live) if live else None
        cpu_in_use = load_line or "not sampled (pass --processes)"

    if enforced is not None:
        pids_max = pids.get("max")
        pids_enforced = (
            f"{pids_max} (pids.max)" if pids_max is not None else "unknown (pids.max unreadable)"
        )
        pids_current = pids.get("current")
        pids_used = pids.get("used_percent")
        pids_in_use = (
            f"{pids_current} ({pids_used}%)" if pids_used is not None else f"{pids_current} current"
        )

    rows = [
        {
            "resource": "memory",
            "declared": (
                f"{declared['memory_gib']} GiB" if declared.get("memory_gib") else "undeclared"
            ),
            "enforced": memory_enforced,
            "in_use": memory_in_use,
            "level": memory.get("ceiling_level") or "unknown",
        },
        {
            "resource": "cpu",
            "declared": f"{declared['cpu_count']}" if declared.get("cpu_count") else "undeclared",
            "enforced": cpu_enforced,
            "in_use": cpu_in_use,
            # An unreadable cpu.max is unknown; a readable one with no sample is nothing to mark,
            # which is a different statement than "unmeasurable ceiling".
            "level": "unknown" if enforced is not None and cpu.get("state") == "unknown" else "",
        },
        {
            "resource": "shm",
            "declared": f"{declared['shm_gib']} GiB" if declared.get("shm_gib") else "undeclared",
            "enforced": "no cgroup file for it",
            "in_use": "no cgroup file for it",
            "level": "",
        },
        {
            "resource": "pids",
            "declared": "undeclared",
            "enforced": pids_enforced,
            "in_use": pids_in_use,
            "level": pids.get("ceiling_level") or "unknown",
        },
    ]
    return rows


# --------------------------------------------------------------------------- the merged report

#: The four sources, in the order the report prints them. Named because a caller must be able to
#: ask for one of them by key: collapsing them is the regression this command exists to avoid.
SOURCE_NAMES: tuple[str, ...] = ("declaration", "enforced", "live", "history")


def source_state(
    transport: str,
    *,
    requested: bool = True,
    reachable: bool = True,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """One source's own reachability record: what it reads, whether it spoke, and why not.

    ``requested`` False is a different answer from ``reachable`` False: the first says the caller
    never asked (no ``--since``, no notebook), the second says it was asked and stayed silent.
    """
    return {
        "transport": transport,
        "requested": requested,
        "reachable": reachable,
        "reason": reason,
    }


def build_resource_report(
    *,
    declaration: Optional[dict[str, Any]],
    enforced: Optional[dict[str, Any]],
    live: Optional[dict[str, Any]],
    history: Optional[dict[str, Any]],
    reachability: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Merge the four sources into one report without merging their numbers.

    Each source's payload is carried under its own key, untouched. The judgement fields
    (``verdict``, ``alarms``, ``warnings``) live once, at the top: the enforced source's own
    ``verdict``/``alarms``/``warnings``/``platform`` keys are lifted out of its section rather than
    copied, so a reader cannot find two alarm lists that disagree. A source that was asked and
    could not answer adds a warning naming itself, and does not silence the others.
    """
    alarms: list[str] = []
    warnings: list[str] = []
    oom_kill = 0
    enforced_verdict: Optional[str] = None
    enforced_section: Optional[dict[str, Any]] = None

    if enforced is not None:
        enforced_section = dict(enforced)
        enforced_verdict = enforced_section.pop("verdict", None)
        alarms.extend(str(item) for item in enforced_section.pop("alarms", []) or [])
        warnings.extend(str(item) for item in enforced_section.pop("warnings", []) or [])
        enforced_section.pop("platform", None)  # the declaration source owns that key
        oom_kill = int(enforced_section.get("oom_kill") or 0)
        for record in enforced_section.get("declaration_drift") or []:
            if isinstance(record, dict):
                alarms.append(_drift_alarm(record))

    for name in SOURCE_NAMES:
        state = reachability.get(name) or {}
        if state.get("requested") and not state.get("reachable"):
            warnings.append(
                f"{name} could not answer ({state.get('reason') or 'no reason given'}); "
                f"the other sections here say nothing about what {name} would have said"
            )

    if alarms:
        verdict = "alarm"
    elif warnings:
        verdict = "warning"
    elif enforced_verdict == "ok":
        # "ok" is earned by reading a v2 tree, not by the absence of a complaint: a run where the
        # only source that spoke was the live view has not measured a ceiling at all.
        verdict = "ok"
    else:
        verdict = "unknown"

    return {
        "sources": {name: reachability.get(name, source_state("unknown")) for name in SOURCE_NAMES},
        "declaration": declaration,
        "enforced": enforced_section,
        "live": live,
        "history": history,
        "pairing": _pairing_rows(declaration, enforced_section, live),
        "oom_kill": oom_kill,
        "verdict": verdict,
        "alarms": alarms,
        "warnings": warnings,
    }


def _answered_sources(report: dict[str, Any]) -> list[str]:
    return [
        name
        for name in SOURCE_NAMES
        if (report["sources"].get(name) or {}).get("requested")
        and (report["sources"].get(name) or {}).get("reachable")
    ]


def _format_pairing(rows: list[dict[str, Any]]) -> list[str]:
    headers = ("RESOURCE", "DECLARED", "ENFORCED", "IN USE")
    cells = [(row["resource"], row["declared"], row["enforced"], row["in_use"]) for row in rows]
    widths = [
        max(len(header), *(len(row[column]) for row in cells))
        for column, header in enumerate(headers)
    ]
    lines = [
        "  " + "  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True))
    ]
    for row, row_cells in zip(rows, cells, strict=True):
        # The last column is not padded: it carries the longest text, and padding it would only
        # pad the end of the line.
        body = "  ".join(
            cell.ljust(width) for cell, width in zip(row_cells[:-1], widths[:-1], strict=True)
        )
        level = str(row.get("level") or "")
        mark = f"  [{_mark(level)}]" if level else ""
        lines.append(f"  {body}  {row_cells[-1]}{mark}")
    return lines


def format_resource_lines(
    report: dict[str, Any], *, title: str, history_lines: list[str]
) -> list[str]:
    """Render the merged report: pairing, then current use, then the opt-in sections."""
    lines: list[str] = [title, "=" * 100]

    lines.append("Entitlement vs enforcement (what is declared / what is enforced / what is used)")
    lines.extend(_format_pairing(report["pairing"]))

    lines.append("")
    lines.append("Current usage")
    enforced = report.get("enforced") or {}
    memory = enforced.get("memory") or {}
    pids = enforced.get("pids") or {}
    if enforced:
        used = memory.get("used_percent")
        lines.append(
            f"  {'Memory now':<15}: "
            + (
                f"{_gib_text(_bytes_to_gib(memory.get('current_bytes')))} of "
                f"{_gib_text(memory.get('limit_gib'))} - {used}%"
                if used is not None
                else f"{_gib_text(_bytes_to_gib(memory.get('current_bytes')))} used, "
                "no cap to divide by"
            )
            + f"  [{_mark(memory.get('ceiling_level') or 'unknown')}]"
        )
        lines.append(
            f"  {'PIDs now':<15}: "
            + (
                f"{pids.get('current')} of {pids.get('max')} - {pids.get('used_percent')}%"
                if pids.get("used_percent") is not None
                else f"{pids.get('current')} current, pids.max unknown"
            )
            + f"  [{_mark(pids.get('ceiling_level') or 'unknown')}]"
        )
    else:
        lines.append(
            f"  {'Memory now':<15}: not probed (the cgroup probe did not answer: "
            f"{(report['sources'].get('enforced') or {}).get('reason') or 'not requested'})"
        )
    live = report.get("live")
    load_line = _live_load_line(live) if live else None
    lines.append(f"  {'CPU now':<15}: " + (load_line or "not sampled (pass --processes)"))
    declaration = report.get("declaration")
    if declaration:
        lines.append(
            f"  {'Declared by':<15}: "
            f"{declaration.get('memory_gib')} GiB memory, {declaration.get('cpu_count')} CPU"
            + (f", {declaration['shm_gib']} GiB SHM" if declaration.get("shm_gib") else "")
            + f"  ({declaration.get('source') or 'platform declaration'})"
        )
    else:
        lines.append(
            f"  {'Declared by':<15}: not consulted "
            f"({(report['sources'].get('declaration') or {}).get('reason') or 'not requested'})"
        )

    if enforced:
        lines.append("")
        lines.append("Enforced by the container's own cgroup (/sys/fs/cgroup, read inside)")
        lines.extend(format_enforced_lines(enforced))

    if report.get("live") is not None:
        lines.append("")
        lines.append(format_live_lines(report["live"]))

    if history_lines:
        lines.append("")
        lines.append("History (the platform's own usage series)")
        lines.extend(history_lines)

    if report["alarms"]:
        lines.append("")
        for alarm in report["alarms"]:
            lines.append(click.style(f"!! ALARM: {alarm}", fg="red", bold=True))
    if report["warnings"]:
        lines.append("")
        for warning in report["warnings"]:
            lines.append(click.style(f"!! WARNING: {warning}", fg="yellow"))
    if not report["alarms"] and not report["warnings"]:
        lines.append("")
        lines.append(
            click.style("No limit mismatch, no near-ceiling use, no recorded OOM kill.", fg="green")
        )

    answered = _answered_sources(report)
    lines.append("")
    lines.append(
        "Sources: "
        + ", ".join(
            f"{name}={(report['sources'].get(name) or {}).get('transport')} "
            f"{'answered' if (report['sources'].get(name) or {}).get('reachable') else 'silent'}"
            for name in SOURCE_NAMES
            if (report["sources"].get(name) or {}).get("requested")
        )
        + ("; requested: " + ", ".join(answered) if answered else "; no source answered")
    )
    return lines


# --------------------------------------------------------------------------- the command


def _run_probe_locally(timeout: int) -> tuple[str, int]:
    """Run the cgroup probe on this machine. Same script, so the two paths cannot drift."""
    shell = shutil.which("sh") or ("/bin/sh" if os.path.exists("/bin/sh") else None)
    if shell is None:
        raise RuntimeError("no /bin/sh to read /sys/fs/cgroup and /proc with")
    completed = subprocess.run(
        [shell, "-c", CGROUP_PROBE_COMMAND],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return completed.stdout or "", completed.returncode


def _read_cgroup_source(
    ctx: Context,
    *,
    stdout: str,
    returncode: int,
    resolved_id: Optional[str],
    platform_declaration: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Turn a finished probe capture into the enforced report, or abort on a half-read one.

    A capture that never reached its ``@@end`` sentinel is an error rather than a short report:
    read as data, the files after the cut would come out as *missing*, which is an invented
    finding about the notebook instead of a fact about the capture.
    """
    sections = parse_probe_sections(stdout)
    complete = probe_is_complete(stdout, sections)
    if returncode != 0 or not complete or not sections:
        _handle_error(
            ctx,
            "ExecError",
            (
                f"The cgroup probe did not complete (exit {returncode}"
                f"{'' if complete else ', output truncated'}"
                f"{'' if sections else ', no sections read'}). "
                "Nothing here is a limit; the container's cgroup state is unknown."
            ),
            EXIT_API_ERROR,
            hint=(
                "Re-run once the notebook's Jupyter shell answers ('inspire notebook exec -n "
                f"{resolved_id or '<notebook>'} -- true'); the probe is a single read-only command."
            ),
            data={
                "returncode": returncode,
                "exit_code": returncode,
                "complete": complete,
                "sections_read": sorted(sections),
            },
        )
        return None  # pragma: no cover - _handle_error exits
    return build_enforced_report(
        sections,
        platform_declaration=platform_declaration,
        complete=complete,
    )


@click.command("resource")
@click.argument("notebook", required=False)
@click.option(
    "--local",
    is_flag=True,
    help="Probe the machine this CLI runs on instead of a notebook (no platform cross-check).",
)
@click.option(
    "--bridge",
    "-b",
    help=(
        "Live view only: sample one notebook-backed bridge instead of every one of them. It "
        "cannot name the instance for the platform sources - pass the NOTEBOOK argument for the "
        "declaration, the cgroup cross-check, or --since history."
    ),
)
@click.option(
    "--processes",
    "-n",
    "processes",
    type=click.IntRange(1, MAX_PROCESSES),
    is_flag=False,
    flag_value=DEFAULT_PROCESSES,
    default=None,
    help=(
        f"Add the per-process live table - GPU utilisation where nvidia-smi answers, the /proc "
        f"CPU view where it does not. Optional row count, 1..{MAX_PROCESSES}, default "
        f"{DEFAULT_PROCESSES}."
    ),
)
@click.option(
    "--since",
    default=None,
    help=(
        "Add the platform's usage history over this window: a count of seconds (3600) or of "
        "s/m/h/d (45m, 6h, 2d)."
    ),
)
@click.option(
    "--interval",
    type=int,
    default=300,
    show_default=True,
    help=(
        "Sample interval for --since, in seconds: 60, 300, 1800 or 3600. Each one may span only "
        "so much history (5h, 25h, 6d, 30d), and a longer window is reported as trimmed."
    ),
)
@click.option(
    "--run-index",
    type=int,
    default=-1,
    show_default=False,
    help="Zero-based notebook run index for --since; -1 selects latest.",
)
@click.option(
    "--metric",
    "metric_types",
    multiple=True,
    help="Metric type for --since. Repeatable or comma-separated.",
)
@click.option("--series", is_flag=True, help="Include the full time series in the history section.")
@click.option("--raw", is_flag=True, help="Platform-shaped metric payloads in the history section.")
@click.option(
    "--watch",
    is_flag=True,
    help="Re-sample the live sections until interrupted (not with --json, and not with --since).",
)
@click.option(
    "--watch-interval",
    type=float,
    default=2.0,
    show_default=True,
    help="Refresh interval in seconds for --watch.",
)
@click.option(
    "--timeout",
    default=60,
    show_default=True,
    help="Seconds to wait for the in-notebook cgroup probe.",
)
@click.option(
    "--no-check",
    is_flag=True,
    help="Skip tunnel preflight checks and run the live probes directly.",
)
@pass_context
def notebook_resource_cmd(
    ctx: Context,
    notebook: Optional[str],
    local: bool,
    bridge: Optional[str],
    processes: Optional[int],
    since: Optional[str],
    interval: int,
    run_index: int,
    metric_types: tuple[str, ...],
    series: bool,
    raw: bool,
    watch: bool,
    watch_interval: float,
    timeout: int,
    no_check: bool,
) -> None:
    """Report what a notebook is entitled to, what its container enforces, and what uses it.

    One read-only screen over four sources, printed as separate sections and as separate `--json`
    keys: the platform's DECLARATION (`notebook status`: memory_gib, cpu_count, shm_gib), what the
    container's own cgroup ENFORCES (`/sys/fs/cgroup`: memory.max, memory.current, memory.high,
    memory.events, cpu.max, pids.max, pids.current - read over the Jupyter shell `notebook exec`
    uses), what is IN USE now (the cgroup's own counters, plus the per-process live table under
    `--processes`), and the platform's usage HISTORY over time (`--since`). Nothing is reconciled
    into one number, because their disagreement is the finding: `free` reads `/proc/meminfo` and
    answers the host rather than the cap, and a usage rate never tells you a ceiling.

    \b
    The verdict is the exit code, as it was for `notebook limits`: a recorded `memory.events`
    oom_kill, or a declaration that disagrees with the enforced cgroup, prints the full report and
    exits non-zero. Unknown is reported as unknown rather than as zero, and a probe cut off before
    its end sentinel is an error rather than a short report.

    \b
    Name a NOTEBOOK for the platform sources. `--bridge/-b` on its own samples the live view over
    saved notebook-backed tunnels and leaves the declaration unasked; `--local` runs the identical
    cgroup probe on this machine. Each source reports its own reachability, so one that cannot
    answer does not silence the others. Strictly read-only: cat, awk, nvidia-smi and directory
    globs over /proc and /sys/fs/cgroup, with nothing written, killed or changed.

    \b
    Examples:
        inspire notebook resource pi-scale-big
        inspire notebook resource pi-scale-big --processes 20
        inspire notebook resource pi-scale-big --since 6h --interval 300 --series
        inspire notebook resource --bridge mybridge --watch
        inspire notebook resource --local          # this machine, no platform call
        INSPIRE_LIMITS_CGROUP_ROOT=/tmp/tree inspire notebook resource --local
    """
    json_output = resolve_json_output(ctx, False)
    if interval <= 0:
        _handle_error(
            ctx, "ValidationError", "--interval must be greater than 0", EXIT_VALIDATION_ERROR
        )
        return
    if watch_interval <= 0:
        _handle_error(
            ctx, "ValidationError", "--watch-interval must be greater than 0", EXIT_VALIDATION_ERROR
        )
        return
    if timeout <= 0:
        _handle_error(ctx, "ValidationError", "--timeout must be positive", EXIT_VALIDATION_ERROR)
        return
    if watch and json_output:
        _handle_error(
            ctx,
            "ValidationError",
            "--watch is not supported with --json.",
            EXIT_VALIDATION_ERROR,
            hint="Use a single sample with '--json' (without --watch).",
        )
        return
    if watch and since:
        _handle_error(
            ctx,
            "ValidationError",
            "--watch re-samples the live sections; --since is one history window, not a live view.",
            EXIT_VALIDATION_ERROR,
            hint="Drop --watch to read the history once, or drop --since to watch the container.",
        )
        return
    if run_index < -1:
        _handle_error(ctx, "ValidationError", "--run-index must be >= -1", EXIT_VALIDATION_ERROR)
        return

    since_seconds: Optional[int] = None
    if since:
        try:
            since_seconds = parse_since_window(since)
        except ValueError as exc:
            _handle_error(ctx, "ValidationError", str(exc), EXIT_VALIDATION_ERROR)
            return

    if local and (notebook or bridge or processes is not None or since):
        _handle_error(
            ctx,
            "ValidationError",
            "--local probes this machine's cgroup, so it takes no notebook argument, --bridge, "
            "--processes or --since.",
            EXIT_VALIDATION_ERROR,
            hint=(
                "Drop --local to probe a notebook (and reach the platform's declaration and "
                "history), or drop the other arguments to probe this machine."
            ),
        )
        return
    if not local and not notebook and not bridge and processes is None:
        _handle_error(
            ctx,
            "ValidationError",
            "Name a NOTEBOOK, or pass --bridge/-b (or --processes) for the live view over saved "
            "notebook-backed tunnels, or use --local to probe this machine.",
            EXIT_VALIDATION_ERROR,
            hint="Run 'inspire notebook list' to see available notebooks.",
        )
        return

    while True:
        report, title, history_lines = _collect_once(
            ctx,
            json_output=json_output,
            notebook=notebook,
            local=local,
            bridge=bridge,
            processes=processes,
            since_seconds=since_seconds,
            interval=interval,
            run_index=run_index,
            metric_types=metric_types,
            series=series,
            raw=raw,
            timeout=timeout,
            no_check=no_check,
        )

        if json_output:
            click.echo(
                json_formatter.format_json(
                    report,
                    success=bool(_answered_sources(report)) and report["verdict"] != "alarm",
                )
            )
        else:
            click.echo(
                "\n".join(format_resource_lines(report, title=title, history_lines=history_lines))
            )

        if not _answered_sources(report):
            asked = [
                name
                for name in SOURCE_NAMES
                if (report["sources"].get(name) or {}).get("requested")
            ]
            _handle_error(
                ctx,
                "APIError",
                f"No source answered for this report ({', '.join(asked) or 'nothing was asked'}).",
                EXIT_API_ERROR,
                hint=(
                    "The platform sources need a web session and a RUNNING notebook; the live "
                    "view needs a notebook-backed tunnel ('inspire notebook ssh <id> --save-as "
                    "<name>', then 'inspire tunnel test -b <name>')."
                ),
                data={"sources": report["sources"]},
            )
            return

        _raise_for_alarm(ctx, report)

        if not watch:
            return
        try:
            time.sleep(watch_interval)
        except KeyboardInterrupt:
            return
        click.echo("")


def _collect_once(
    ctx: Context,
    *,
    json_output: bool,
    notebook: Optional[str],
    local: bool,
    bridge: Optional[str],
    processes: Optional[int],
    since_seconds: Optional[int],
    interval: int,
    run_index: int,
    metric_types: tuple[str, ...],
    series: bool,
    raw: bool,
    timeout: int,
    no_check: bool,
) -> tuple[dict[str, Any], str, list[str]]:
    """Ask each source this invocation asked of, and record for itself why it stayed silent.

    Returns the merged report, its title, and the rendered history lines (text only - the JSON
    envelope carries the history payload, not its own rendering).
    """
    reachability: dict[str, dict[str, Any]] = {
        "declaration": source_state("notebook-status", requested=False),
        "enforced": source_state("local", requested=False),
        "live": source_state("ssh-tunnel", requested=False),
        "history": source_state("web-session", requested=False),
    }
    declaration: Optional[dict[str, Any]] = None
    enforced: Optional[dict[str, Any]] = None
    live: Optional[dict[str, Any]] = None
    history: Optional[dict[str, Any]] = None
    history_lines: list[str] = []
    detail: dict[str, Any] = {}
    resolved_id = ""
    title = "Notebook resource (this machine)"
    # Bridge scope is everything a tunnel can answer without naming the instance to the platform.
    bridge_scope = not local and notebook is None
    live_wanted = processes is not None or bridge_scope

    if local:
        reachability["enforced"] = source_state("local")
        try:
            stdout, returncode = _run_probe_locally(timeout)
        except subprocess.TimeoutExpired:
            _handle_error(
                ctx, "Timeout", f"The local probe did not finish within {timeout}s.", EXIT_TIMEOUT
            )
            return {}, title, []
        except OSError as exc:
            _handle_error(ctx, "ExecError", f"Local probe failed to run: {exc}", EXIT_API_ERROR)
            return {}, title, []
        enforced = _read_cgroup_source(ctx, stdout=stdout, returncode=returncode, resolved_id=None)

    if notebook:
        session = require_web_session(
            ctx,
            hint=(
                "Notebook resource requires web authentication. "
                "Set [auth].username/password in config.toml or "
                "INSPIRE_USERNAME/INSPIRE_PASSWORD."
            ),
        )
        from .notebook_lookup import _resolve_notebook_id

        resolved_id, _ = _resolve_notebook_id(
            ctx,
            session=session,
            config=load_config(ctx),
            base_url=get_base_url(),
            identifier=notebook,
            json_output=json_output,
        )
        title = f"Notebook resource: {notebook} ({resolved_id})"

        reachability["declaration"] = source_state("notebook-status")
        try:
            detail = _fetch_notebook_detail(session, resolved_id)
        except Exception as exc:  # noqa: BLE001 - the source answers for its own reach
            reachability["declaration"] = source_state(
                "notebook-status", reachable=False, reason=str(exc)
            )
        else:
            declaration = platform_declaration_from_detail(detail)
            title = f"Notebook resource: {detail.get('name') or resolved_id} ({resolved_id})"
            status = str(detail.get("status") or "").upper()
            if status and status != "RUNNING":
                _handle_error(
                    ctx,
                    "APIError",
                    (
                        f"Notebook '{resolved_id}' is {status}; the cgroup and live sections run "
                        "inside the container."
                    ),
                    EXIT_API_ERROR,
                    hint=f"Start it with 'inspire notebook start {resolved_id}' and re-run.",
                )

        reachability["enforced"] = source_state("notebook-exec")
        try:
            stdout, returncode = _run_probe_in_notebook(session, resolved_id, timeout)
        except Exception as exc:  # noqa: BLE001 - the source answers for its own reach
            reachability["enforced"] = source_state(
                "notebook-exec",
                reachable=False,
                reason=str(exc)
                or (
                    f"the notebook's shell did not answer within {timeout}s "
                    f"({type(exc).__name__})"
                ),
            )
        else:
            enforced = _read_cgroup_source(
                ctx,
                stdout=stdout,
                returncode=returncode,
                resolved_id=resolved_id,
                platform_declaration=declaration,
            )

        if since_seconds is not None:
            reachability["history"] = source_state("web-session")
            try:
                history = _fetch_history(
                    session=session,
                    notebook_id=resolved_id,
                    metric_types=metric_types,
                    interval=interval,
                    run_index=run_index,
                    raw=raw,
                    since_seconds=since_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - the source answers for its own reach
                history = None
                reachability["history"] = source_state(
                    "web-session", reachable=False, reason=str(exc)
                )
            else:
                history_lines = metrics_text_lines(history, label="Notebook", include_series=series)

    if live_wanted:
        reachability["live"] = source_state("ssh-tunnel")
        tunnel_config = load_tunnel_config()
        targets = _select_target_bridges(ctx, tunnel_config=tunnel_config, bridge_name=bridge)
        if resolved_id:
            targets = [b for b in targets if _bridge_notebook_id(b) == resolved_id]
        if not targets:
            reachability["live"] = source_state(
                "ssh-tunnel",
                reachable=False,
                reason=(
                    f"no notebook-backed bridge points at '{resolved_id}'. Create one with "
                    f"'inspire notebook ssh {resolved_id} --save-as <name>'."
                ),
            )
        else:
            live = sample_live(
                ctx,
                tunnel_config=tunnel_config,
                targets=targets,
                no_check=no_check,
                processes=processes if processes is not None else DEFAULT_PROCESSES,
            )
            if not int((live.get("summary") or {}).get("ok") or 0):
                reachability["live"] = source_state(
                    "ssh-tunnel",
                    reachable=False,
                    reason="no bridge answered: "
                    + "; ".join(
                        f"{item.get('bridge')}={item.get('error') or 'no telemetry'}"
                        for item in live.get("items") or []
                    ),
                )

    report = build_resource_report(
        declaration=declaration,
        enforced=enforced,
        live=live,
        history=(
            metrics_payload(history, id_key="notebook_id", raw=raw, include_series=series)
            if history is not None
            else None
        ),
        reachability=reachability,
    )
    if resolved_id:
        report["notebook_id"] = resolved_id
        report["name"] = detail.get("name")
        report["notebook_status"] = detail.get("status")
    return report, title, history_lines


def _fetch_history(
    *,
    session: Any,
    notebook_id: str,
    metric_types: tuple[str, ...],
    interval: int,
    run_index: int,
    raw: bool,
    since_seconds: int,
) -> dict[str, Any]:
    """The platform's own usage series for one window. Raises when the platform refuses it."""
    from inspire.platform.web import browser_api as browser_api_module

    return browser_api_module.get_notebook_resource_metrics(
        notebook_id,
        metric_types=parse_metric_types(metric_types),
        interval_seconds=interval,
        run_index=run_index,
        raw=raw,
        since_seconds=since_seconds,
        session=session,
    )


def _raise_for_alarm(ctx: Context, report: dict[str, Any]) -> None:
    """The alarm keeps the exit code. Prose alone is what let 41 dead arms look alive for hours."""
    if not report["alarms"]:
        return
    oom_kill = int(report.get("oom_kill") or 0)
    enforced = report.get("enforced") or {}
    memory = enforced.get("memory") or {}
    if oom_kill > 0:
        message = (
            f"memory.events oom_kill = {oom_kill}: the kernel OOM-killer has run in this cgroup. "
            "See the report above for the limit it hit."
        )
    else:
        message = str(report["alarms"][0])
    _handle_error(
        ctx,
        "LimitsAlarm",
        message,
        EXIT_GENERAL_ERROR,
        hint=(
            "Nothing else inside the container records which process died. Treat every child "
            "that 'looks alive' as suspect, and size the workload from memory.max, not "
            "/proc/meminfo."
        ),
        data={
            "verdict": report["verdict"],
            "oom_kill": oom_kill,
            "memory_limit_bytes": memory.get("limit_bytes"),
            "memory_current_bytes": memory.get("current_bytes"),
            "memory_used_percent": memory.get("used_percent"),
            "memtotal_gib": (enforced.get("container_memory_view") or {}).get(
                "meminfo_memtotal_gib"
            ),
            "warnings": report["warnings"],
        },
    )


__all__ = [
    "CGROUP_PROBE_COMMAND",
    "CRITICAL_PERCENT",
    "SOURCE_NAMES",
    "WARN_PERCENT",
    "build_enforced_report",
    "build_resource_report",
    "format_enforced_lines",
    "format_live_lines",
    "format_resource_lines",
    "notebook_resource_cmd",
    "parse_probe_sections",
    "parse_since_window",
    "platform_declaration_from_detail",
    "probe_is_complete",
    "source_state",
]
