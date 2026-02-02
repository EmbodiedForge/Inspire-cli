"""GPU keepalive utilities for maintaining utilization above threshold."""

from __future__ import annotations

import shlex

# Script that maintains ~50% GPU utilization on ALL available GPUs
KEEPALIVE_SCRIPT = '''
import torch
import time
import sys

def keepalive():
    """Maintain GPU utilization above 40% on all GPUs with matrix operations."""
    gpu_count = torch.cuda.device_count()
    if gpu_count == 0:
        print("No GPU found, exiting keepalive")
        return

    print(f"GPU keepalive started on {gpu_count} GPU(s)")
    size = 4096  # Matrix size for ~50% utilization

    while True:
        for gpu_id in range(gpu_count):
            try:
                device = f"cuda:{gpu_id}"
                a = torch.randn(size, size, device=device)
                b = torch.randn(size, size, device=device)
                for _ in range(10):
                    c = torch.matmul(a, b)
                torch.cuda.synchronize(device)
                del a, b, c
            except Exception as e:
                print(f"Keepalive error on GPU {gpu_id}: {e}", file=sys.stderr)
        torch.cuda.empty_cache()
        time.sleep(1)

if __name__ == "__main__":
    keepalive()
'''


def get_keepalive_command() -> str:
    """Return shell command to run keepalive script in background.

    Returns:
        Shell command string that runs the keepalive script via nohup.
    """
    return f'nohup python -c {shlex.quote(KEEPALIVE_SCRIPT)} > /tmp/keepalive.log 2>&1 &'
