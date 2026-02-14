"""Smart GPU keepalive: auto-start when idle, auto-stop when training runs."""

from __future__ import annotations

import shlex

# Smart keepalive script that:
# 1. Monitors GPU utilization via nvidia-smi every 30s
# 2. If ALL GPUs < 40% for 2 consecutive hours, starts fake workload
# 3. If ANY GPU >= 40% (real training started), stops fake workload immediately
# 4. Minimal memory footprint (~200MB per GPU, released when not needed)
KEEPALIVE_SCRIPT = r'''
import subprocess
import time
import sys
import os
import signal
import atexit

UTIL_THRESHOLD = 40       # GPU util % below which we consider "idle"
IDLE_GRACE_SECS = 2 * 3600  # 2 hours of idle before keepalive kicks in
POLL_INTERVAL = 30        # Check GPU util every 30 seconds
WORK_DURATION = 20        # Seconds of matmul work per burst
WORK_PAUSE = 10           # Seconds of pause between bursts (keeps avg ~50%)
PID_FILE = "/tmp/keepalive.pid"

def get_gpu_utils():
    """Query nvidia-smi for per-GPU utilization percentages."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            text=True, timeout=10,
        )
        return [int(x.strip()) for x in out.strip().split("\n") if x.strip()]
    except Exception as e:
        print(f"[keepalive] nvidia-smi error: {e}", flush=True)
        return []

def run_burst(gpu_count):
    """Run a short burst of matmul on all GPUs to raise utilization."""
    import torch
    size = 4096
    deadline = time.time() + WORK_DURATION
    while time.time() < deadline:
        for gpu_id in range(gpu_count):
            try:
                device = f"cuda:{gpu_id}"
                a = torch.randn(size, size, device=device)
                b = torch.randn(size, size, device=device)
                for _ in range(5):
                    c = torch.matmul(a, b)
                torch.cuda.synchronize(device)
                del a, b, c
            except Exception as e:
                print(f"[keepalive] burst error GPU {gpu_id}: {e}", flush=True)
        torch.cuda.empty_cache()
        time.sleep(0.5)

def main():
    # Write PID file and register cleanup
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    def _cleanup_pid():
        try:
            os.remove(PID_FILE)
        except OSError:
            pass

    atexit.register(_cleanup_pid)

    def _signal_handler(signum, frame):
        _cleanup_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    print(f"[keepalive] Smart keepalive started (pid={os.getpid()})", flush=True)
    print(f"[keepalive] Config: threshold={UTIL_THRESHOLD}%, grace={IDLE_GRACE_SECS}s, poll={POLL_INTERVAL}s", flush=True)

    idle_since = None   # timestamp when all GPUs first went below threshold
    active = False      # whether we're currently doing keepalive bursts

    while True:
        utils = get_gpu_utils()
        if not utils:
            time.sleep(POLL_INTERVAL)
            continue

        gpu_count = len(utils)
        max_util = max(utils)
        avg_util = sum(utils) / len(utils)

        # Check if real workload is running (any GPU above threshold)
        real_work = max_util >= UTIL_THRESHOLD

        if real_work:
            if active:
                print(f"[keepalive] Real workload detected (max_util={max_util}%), stopping keepalive bursts", flush=True)
                active = False
                # Free any GPU memory
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:
                    pass
            idle_since = None
            time.sleep(POLL_INTERVAL)
            continue

        # All GPUs below threshold
        now = time.time()
        if idle_since is None:
            idle_since = now
            print(f"[keepalive] GPUs idle (avg={avg_util:.0f}%), starting grace period", flush=True)

        idle_duration = now - idle_since

        if not active:
            if idle_duration < IDLE_GRACE_SECS:
                remaining = IDLE_GRACE_SECS - idle_duration
                if int(remaining) % 300 < POLL_INTERVAL:  # Log every ~5 min
                    print(f"[keepalive] Idle for {idle_duration:.0f}s, grace remaining: {remaining:.0f}s", flush=True)
                time.sleep(POLL_INTERVAL)
                continue
            else:
                print(f"[keepalive] Grace period expired, starting keepalive bursts", flush=True)
                active = True

        # Active: do a burst then pause
        run_burst(gpu_count)
        time.sleep(WORK_PAUSE)

if __name__ == "__main__":
    main()
'''


def get_keepalive_command() -> str:
    """Return shell command to run smart keepalive script in background.

    The script monitors GPU utilization and only generates fake workload
    when GPUs have been idle (<40% util) for 2+ hours. It automatically
    stops when real training starts.

    Returns:
        Shell command string that runs the keepalive script via nohup.
    """
    return f"nohup python -u -c {shlex.quote(KEEPALIVE_SCRIPT)} > /tmp/keepalive.log 2>&1 &"


def get_keepalive_stop_command() -> str:
    """Return shell command to stop the keepalive process via its PID file.

    Handles stale PID files (process already dead) gracefully.
    """
    return (
        'PID_FILE="/tmp/keepalive.pid"; '
        'if [ -f "$PID_FILE" ]; then '
        'PID=$(cat "$PID_FILE"); '
        'if kill -0 "$PID" 2>/dev/null; then '
        'kill "$PID" && echo "[keepalive] Stopped (pid=$PID)"; '
        "else "
        'echo "[keepalive] Stale PID file (process $PID not running)"; '
        'rm -f "$PID_FILE"; '
        "fi; "
        "else "
        'echo "[keepalive] No PID file found (/tmp/keepalive.pid)"; '
        "fi"
    )


def get_keepalive_status_command() -> str:
    """Return shell command to check if the keepalive process is running."""
    return (
        'PID_FILE="/tmp/keepalive.pid"; '
        'if [ -f "$PID_FILE" ]; then '
        'PID=$(cat "$PID_FILE"); '
        'if kill -0 "$PID" 2>/dev/null; then '
        'echo "[keepalive] Running (pid=$PID)"; '
        "else "
        'echo "[keepalive] Not running (stale PID file)"; '
        'rm -f "$PID_FILE"; '
        "fi; "
        "else "
        'echo "[keepalive] Not running (no PID file)"; '
        "fi"
    )
