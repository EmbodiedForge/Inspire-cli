"""Read-only host telemetry probes (``/proc``, ``getconf``) for a notebook bridge.

``inspire notebook resource --processes`` uses this when a host carries no ``nvidia-smi``, so the
command can still answer the question its name asks: what is this machine doing right now, and
where is it spending it.

Everything here is read-only by construction. The remote script only parses files under
``/proc``, asks ``getconf`` for two constants, and sleeps between the two CPU-time samples; it
opens no file for writing, creates no file, signals no process, and changes no state.

The remote half stays deliberately dumb: it emits raw tab-separated records and this module does
the arithmetic. A probe whose CPU percentage is computed in ``awk`` on the notebook cannot be
tested without a notebook, and cannot be reviewed from the diff either.

The cgroup half of the report (the cap, the counters, the oom record) reads ``/sys/fs/cgroup`` in
its own probe rather than growing a second reader of ``/proc`` and ``/sys`` here, so keep the two
readers' record tags apart.
"""

from __future__ import annotations

from typing import Any, Optional

#: Wall-clock gap between the two CPU-time samples, in seconds. ``top`` uses one second, and so
#: do we: a shorter window shows a scheduler quantum as 0%/100% noise, a longer one slows every
#: ``--watch`` refresh down for no extra truth.
SAMPLE_SECONDS = 1

#: Longest command line carried per process. A ``python -c`` blob can run to tens of kilobytes,
#: and one process must not push the other nine off the screen.
COMMAND_CHARS = 300

#: Default number of processes in the CPU view.
DEFAULT_PROCESSES = 10

#: Upper bound on that count: the point is the head of the distribution, and past a few dozen
#: rows the reader is scrolling, not reading.
MAX_PROCESSES = 50

# Record tags emitted by the remote script.
_TAG_KERNEL = "K"  # clock ticks per second, page size in KiB
_TAG_LOAD = "L"  # /proc/loadavg: 1 minute, 5 minutes, 15 minutes
_TAG_CORES = "C"  # online CPU entries in /proc/cpuinfo
_TAG_MEM = "M"  # MemTotal, MemAvailable in KiB
_TAG_RSS = "R"  # pid, resident set in KiB
_TAG_TICKS_A = "A"  # pid, utime+stime in clock ticks (first sample)
_TAG_TICKS_B = "B"  # pid, utime+stime in clock ticks (second sample)
_TAG_UPTIME_A = "U1"  # seconds since boot, first sample
_TAG_UPTIME_B = "U2"  # seconds since boot, second sample
_TAG_CMD = "W"  # pid, command line

# ``__LIMIT__``, ``__SAMPLE__`` and ``__COMMAND__`` are replaced with validated integers; every
# other character below reaches the notebook verbatim. Neither str.format() nor %-formatting can
# be used here: the awk programs are full of braces and every printf is full of %s.
_REMOTE_PROBE = """\
__clk=$(getconf CLK_TCK 2>/dev/null || echo 100)
__pg=$(( $(getconf PAGESIZE 2>/dev/null || echo 4096) / 1024 ))
printf 'K\\t%s\\t%s\\n' "$__clk" "$__pg"
awk 'NR==1{printf "L\\t%s\\t%s\\t%s\\n", $1, $2, $3}' /proc/loadavg 2>/dev/null
awk '/^processor[[:space:]]*:/{n++} END{printf "C\\t%d\\n", n+0}' /proc/cpuinfo 2>/dev/null
awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{printf "M\\t%d\\t%d\\n", t+0, a+0}' /proc/meminfo 2>/dev/null
__stat() { awk -v pg="$__pg" 'FNR==1{ if (match($0, /\\)[^)]*$/)) { t=substr($0, RSTART+2); sub(/^[[:space:]]+/, "", t); if (split(t, f, /[[:space:]]+/) >= 22) { p=FILENAME; sub(/^\\/proc\\//, "", p); sub(/\\/stat$/, "", p); printf "%s\\t%d\\t%d\\n", p, f[22]*pg, f[12]+f[13] } } }' "$@" 2>/dev/null; }
__ticks() { awk 'FNR==1{ if (match($0, /\\)[^)]*$/)) { t=substr($0, RSTART+2); sub(/^[[:space:]]+/, "", t); if (split(t, f, /[[:space:]]+/) >= 13) { p=FILENAME; sub(/^\\/proc\\//, "", p); sub(/\\/stat$/, "", p); printf "%s\\t%d\\n", p, f[12]+f[13] } } }' "$@" 2>/dev/null; }
__top=$(__stat /proc/[0-9]*/stat | sort -k2,2nr | head -n __LIMIT__)
if [ -z "$__top" ]; then exit 0; fi
__pids=$(printf '%s\\n' "$__top" | cut -f1)
printf '%s\\n' "$__top" | awk -F'\\t' '{printf "R\\t%s\\t%s\\n", $1, $2; printf "A\\t%s\\t%s\\n", $1, $3}'
__files=""
for __p in $__pids; do __files="$__files /proc/$__p/stat"; done
awk 'NR==1{printf "U1\\t%s\\n", $1}' /proc/uptime 2>/dev/null
sleep __SAMPLE__
awk 'NR==1{printf "U2\\t%s\\n", $1}' /proc/uptime 2>/dev/null
__ticks $__files | awk -F'\\t' '{printf "B\\t%s\\t%s\\n", $1, $2}'
for __p in $__pids; do __c=$(tr '\\000\\n\\t' '   ' < /proc/$__p/cmdline 2>/dev/null | cut -c 1-__COMMAND__); if [ -z "$__c" ]; then __c=$(cut -c 1-__COMMAND__ /proc/$__p/comm 2>/dev/null); fi; printf 'W\\t%s\\t%s\\n' "$__p" "$__c"; done
"""


def build_host_proc_probe(
    *,
    processes: int = DEFAULT_PROCESSES,
    sample_seconds: int = SAMPLE_SECONDS,
) -> str:
    """Return the read-only shell probe that samples one host's CPU, memory and processes.

    ``processes`` is how many resident-set leaders to carry. The probe needs nothing but
    ``awk``, coreutils and ``/proc``: no ``ps``, no ``free``, no ``top``, because notebook images
    differ in which of those they ship, and a missing tool must not turn a live host into an
    error line.
    """
    return (
        _REMOTE_PROBE.replace("__LIMIT__", str(int(processes)))
        .replace("__SAMPLE__", str(int(sample_seconds)))
        .replace("__COMMAND__", str(COMMAND_CHARS))
    )


def _as_float(value: str) -> float:
    return float(value.strip())


def _as_int(value: str) -> int:
    return int(float(value.strip()))


def _mib(kib: float) -> float:
    return round(kib / 1024.0, 1)


def parse_host_proc_probe(stdout: str) -> dict[str, Any]:
    """Turn the probe's tab-separated records into a CPU/memory payload.

    Absent records stay absent instead of becoming zeros: a host that answers no ``MemTotal``
    has an *unknown* memory size, which is a different claim from "0 MiB total".
    """
    loads: list[float] = []
    cores: Optional[int] = None
    clk: Optional[float] = None
    mem_total_kib: Optional[float] = None
    mem_avail_kib: Optional[float] = None
    uptime_a: Optional[float] = None
    uptime_b: Optional[float] = None
    rss_kib: dict[str, float] = {}
    ticks_a: dict[str, float] = {}
    ticks_b: dict[str, float] = {}
    commands: dict[str, str] = {}

    for raw in stdout.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        tag = parts[0]
        try:
            if tag == _TAG_LOAD and len(parts) >= 4:
                loads = [_as_float(parts[1]), _as_float(parts[2]), _as_float(parts[3])]
            elif tag == _TAG_CORES and len(parts) >= 2:
                cores = _as_int(parts[1])
            elif tag == _TAG_KERNEL and len(parts) >= 2:
                clk = _as_float(parts[1])
            elif tag == _TAG_MEM and len(parts) >= 3:
                mem_total_kib = _as_float(parts[1])
                mem_avail_kib = _as_float(parts[2])
            elif tag == _TAG_UPTIME_A and len(parts) >= 2:
                uptime_a = _as_float(parts[1])
            elif tag == _TAG_UPTIME_B and len(parts) >= 2:
                uptime_b = _as_float(parts[1])
            elif tag == _TAG_RSS and len(parts) >= 3:
                rss_kib[parts[1]] = _as_float(parts[2])
            elif tag == _TAG_TICKS_A and len(parts) >= 3:
                ticks_a[parts[1]] = _as_float(parts[2])
            elif tag == _TAG_TICKS_B and len(parts) >= 3:
                ticks_b[parts[1]] = _as_float(parts[2])
            elif tag == _TAG_CMD and len(parts) >= 3:
                # Re-join, so a command that survived the remote tab-mangling keeps its shape.
                commands[parts[1]] = "\t".join(parts[2:]).strip()
        except ValueError:
            # One unreadable record is a damaged read, not a damaged host: drop it and keep the
            # rest, because a partial CPU view still beats an error line.
            continue

    window: Optional[float] = None
    if uptime_a is not None and uptime_b is not None and uptime_b > uptime_a:
        window = uptime_b - uptime_a

    processes: list[dict[str, Any]] = []
    for pid, rss in sorted(rss_kib.items(), key=lambda kv: kv[1], reverse=True):
        cpu_percent: Optional[float] = None
        if clk and pid in ticks_a and pid in ticks_b:
            span = window if window is not None else float(SAMPLE_SECONDS)
            delta_ticks = max(0.0, ticks_b[pid] - ticks_a[pid])
            cpu_percent = round(100.0 * (delta_ticks / clk) / span, 1)
        processes.append(
            {
                "pid": _as_int(pid),
                "rss_mib": _mib(rss),
                "cpu_percent": cpu_percent,
                "command": commands.get(pid, ""),
            }
        )

    memory: Optional[dict[str, Any]] = None
    if mem_total_kib is not None:
        available_kib = mem_avail_kib if mem_avail_kib is not None else 0.0
        used_kib = max(0.0, mem_total_kib - available_kib)
        memory = {
            "total_mib": _mib(mem_total_kib),
            "available_mib": _mib(available_kib),
            "used_mib": _mib(used_kib),
            "used_percent": round(100.0 * used_kib / mem_total_kib, 1) if mem_total_kib else 0.0,
        }

    return {
        "load_1m": loads[0] if loads else None,
        "load_5m": loads[1] if len(loads) > 1 else None,
        "load_15m": loads[2] if len(loads) > 2 else None,
        "cpu_count": cores,
        "memory": memory,
        "processes": processes,
        "sample_seconds": round(window, 2) if window is not None else float(SAMPLE_SECONDS),
    }


def has_host_telemetry(payload: dict[str, Any]) -> bool:
    """True when the probe read anything at all from the host."""
    return bool(
        payload.get("load_1m") is not None
        or payload.get("cpu_count")
        or payload.get("memory")
        or payload.get("processes")
    )


__all__ = [
    "COMMAND_CHARS",
    "DEFAULT_PROCESSES",
    "MAX_PROCESSES",
    "SAMPLE_SECONDS",
    "build_host_proc_probe",
    "has_host_telemetry",
    "parse_host_proc_probe",
]
