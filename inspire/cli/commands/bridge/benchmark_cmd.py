"""Bridge benchmark command for measuring SSH connection performance."""

from __future__ import annotations

import statistics
import sys
import time
from typing import Optional

import click

from inspire.bridge.tunnel import (
    is_paramiko_available,
    load_tunnel_config,
    run_ssh_command,
    run_ssh_command_pooled,
)
from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_SUCCESS,
    pass_context,
)
from inspire.cli.utils.common import json_option
from inspire.cli.utils.errors import emit_error
from inspire.cli.utils.notebook_cli import resolve_json_output
from inspire.cli.utils.output import emit_success


@click.command("benchmark")
@click.option(
    "-b",
    "--bridge",
    help="Bridge profile to use for benchmarking",
)
@click.option(
    "-n",
    "--count",
    type=int,
    default=10,
    show_default=True,
    help="Number of commands to run for each mode",
)
@click.option(
    "--command",
    default="echo ok",
    show_default=True,
    help="Command to execute for benchmarking",
)
@click.option(
    "--warmup",
    type=int,
    default=2,
    show_default=True,
    help="Number of warmup runs before measurement",
)
@json_option
@pass_context
def benchmark_command(
    ctx: Context,
    bridge: Optional[str],
    count: int,
    command: str,
    warmup: int,
    json_output: bool = False,
) -> Optional[int]:
    """Benchmark SSH connection performance with and without connection pooling.

    Measures the time to execute commands via SSH tunnel, comparing:
    - Subprocess-based SSH (new connection each time)
    - Paramiko connection pool (persistent connections)

    Expected improvement: ~10x faster with pooling (500ms -> 50ms per command).

    \b
    Examples:
        inspire bridge benchmark
        inspire bridge benchmark -n 20 --command "hostname"
        inspire bridge benchmark -b mybridge --count 5
        inspire --json bridge benchmark
    """
    json_output = resolve_json_output(ctx, json_output)

    # Check if paramiko is available
    if not is_paramiko_available():
        emit_error(
            ctx,
            error_type="DependencyError",
            message="paramiko is required for connection pooling",
            hint="Install with: pip install paramiko",
            exit_code=EXIT_CONFIG_ERROR,
        )
        sys.exit(EXIT_CONFIG_ERROR)

    # Load tunnel config
    try:
        config = load_tunnel_config()
    except Exception as e:
        emit_error(
            ctx,
            error_type="ConfigError",
            message=f"Failed to load tunnel config: {e}",
            exit_code=EXIT_CONFIG_ERROR,
        )
        sys.exit(EXIT_CONFIG_ERROR)

    # Get bridge
    bridge_profile = config.get_bridge(bridge)
    if bridge_profile is None:
        if bridge:
            hint = "Run 'inspire tunnel list' to see available bridges."
            emit_error(
                ctx,
                error_type="ConfigError",
                message=f"Bridge '{bridge}' not found.",
                hint=hint,
                exit_code=EXIT_CONFIG_ERROR,
            )
        else:
            emit_error(
                ctx,
                error_type="ConfigError",
                message="No bridge configured.",
                hint="Run 'inspire tunnel add <name> <url>' first.",
                exit_code=EXIT_CONFIG_ERROR,
            )
        sys.exit(EXIT_CONFIG_ERROR)

    bridge_name = bridge_profile.name

    if not json_output:
        click.echo(f"Benchmarking SSH performance on bridge '{bridge_name}'...")
        click.echo(f"Command: {command}")
        click.echo(f"Runs: {count} (+ {warmup} warmup)")
        click.echo()

    # Warmup runs to establish baseline
    if warmup > 0 and not json_output:
        click.echo(f"Warming up with {warmup} runs...", err=True)

    for _ in range(warmup):
        try:
            run_ssh_command(
                command=command,
                bridge_name=bridge_name,
                config=config,
                timeout=30,
                capture_output=True,
            )
        except Exception as e:
            if not json_output:
                click.echo(f"Warmup failed: {e}", err=True)

    # Benchmark subprocess-based SSH
    if not json_output:
        click.echo(f"Running subprocess-based SSH benchmark ({count} runs)...", err=True)

    subprocess_times: list[float] = []
    subprocess_succeeded = 0
    subprocess_failed = 0
    for i in range(count):
        start = time.perf_counter()
        try:
            result = run_ssh_command(
                command=command,
                bridge_name=bridge_name,
                config=config,
                timeout=30,
                capture_output=True,
            )
            if result.returncode != 0:
                subprocess_failed += 1
                if not json_output:
                    click.echo(f"Run {i+1} failed with exit code {result.returncode}", err=True)
            else:
                subprocess_succeeded += 1
        except Exception as e:
            subprocess_failed += 1
            if not json_output:
                click.echo(f"Run {i+1} failed: {e}", err=True)
        elapsed = time.perf_counter() - start
        subprocess_times.append(elapsed)

    # Benchmark pooled SSH
    if not json_output:
        click.echo(f"Running pooled SSH benchmark ({count} runs)...", err=True)

    pooled_times: list[float] = []
    pooled_succeeded = 0
    pooled_failed = 0
    for i in range(count):
        start = time.perf_counter()
        try:
            result = run_ssh_command_pooled(
                command=command,
                bridge_name=bridge_name,
                config=config,
                timeout=30,
                capture_output=True,
            )
            if result.returncode != 0:
                pooled_failed += 1
                if not json_output:
                    click.echo(f"Run {i+1} failed with exit code {result.returncode}", err=True)
            else:
                pooled_succeeded += 1
        except Exception as e:
            pooled_failed += 1
            if not json_output:
                click.echo(f"Run {i+1} failed: {e}", err=True)
        elapsed = time.perf_counter() - start
        pooled_times.append(elapsed)

    # Calculate statistics
    def calc_stats(times: list[float]) -> dict:
        if not times:
            return {"min": 0, "max": 0, "mean": 0, "median": 0, "stdev": 0}
        return {
            "min": min(times),
            "max": max(times),
            "mean": statistics.mean(times),
            "median": statistics.median(times),
            "stdev": statistics.stdev(times) if len(times) > 1 else 0,
        }

    subprocess_stats = calc_stats(subprocess_times)
    pooled_stats = calc_stats(pooled_times)

    total_succeeded = subprocess_succeeded + pooled_succeeded
    total_failed = subprocess_failed + pooled_failed
    total_runs = total_succeeded + total_failed

    # Calculate improvement
    if pooled_stats["mean"] > 0:
        speedup = subprocess_stats["mean"] / pooled_stats["mean"]
        reduction = (
            (subprocess_stats["mean"] - pooled_stats["mean"]) / subprocess_stats["mean"] * 100
        )
    else:
        speedup = 0
        reduction = 0

    output = {
        "bridge": bridge_name,
        "command": command,
        "runs": count,
        "succeeded": total_succeeded,
        "failed": total_failed,
        "subprocess": {
            "succeeded": subprocess_succeeded,
            "failed": subprocess_failed,
            "times_ms": [t * 1000 for t in subprocess_times],
            "stats_ms": {k: v * 1000 for k, v in subprocess_stats.items()},
        },
        "pooled": {
            "succeeded": pooled_succeeded,
            "failed": pooled_failed,
            "times_ms": [t * 1000 for t in pooled_times],
            "stats_ms": {k: v * 1000 for k, v in pooled_stats.items()},
        },
        "improvement": {
            "speedup": round(speedup, 2),
            "time_reduction_percent": round(reduction, 1),
        },
    }
    if not json_output:
        click.echo()
        click.echo("Results:")
        click.echo("=" * 50)

        def fmt_ms(val: float) -> str:
            return f"{val * 1000:.1f} ms"

        click.echo("\nSubprocess-based SSH:")
        click.echo(f"  Runs:   {subprocess_succeeded} succeeded, {subprocess_failed} failed")
        click.echo(f"  Mean:   {fmt_ms(subprocess_stats['mean'])}")
        click.echo(f"  Median: {fmt_ms(subprocess_stats['median'])}")
        click.echo(f"  Min:    {fmt_ms(subprocess_stats['min'])}")
        click.echo(f"  Max:    {fmt_ms(subprocess_stats['max'])}")
        click.echo(f"  Stdev:  {fmt_ms(subprocess_stats['stdev'])}")

        click.echo("\nPooled SSH (paramiko):")
        click.echo(f"  Runs:   {pooled_succeeded} succeeded, {pooled_failed} failed")
        click.echo(f"  Mean:   {fmt_ms(pooled_stats['mean'])}")
        click.echo(f"  Median: {fmt_ms(pooled_stats['median'])}")
        click.echo(f"  Min:    {fmt_ms(pooled_stats['min'])}")
        click.echo(f"  Max:    {fmt_ms(pooled_stats['max'])}")
        click.echo(f"  Stdev:  {fmt_ms(pooled_stats['stdev'])}")

        click.echo("\nImprovement:")
        click.echo(f"  Speedup: {speedup:.1f}x faster")
        click.echo(f"  Time reduction: {reduction:.1f}%")

        if speedup >= 5:
            click.echo("\n✓ Excellent improvement! Connection pooling is working well.")
        elif speedup >= 2:
            click.echo("\n✓ Good improvement from connection pooling.")
        elif speedup >= 1.2:
            click.echo("\n⚠ Modest improvement. Network conditions may vary.")
        else:
            click.echo("\n⚠ No significant improvement. Check connection health.")

    # Timing stats include failed runs, so a benchmark where nothing worked must not
    # report success: surface the failure and exit non-zero for scripts and CI.
    if total_runs > 0 and total_succeeded == 0:
        emit_error(
            ctx,
            error_type="CommandFailed",
            message=f"All {total_runs} benchmark runs failed",
            hint=(
                "Check the bridge with 'inspire tunnel status' and rerun with a simpler "
                "--command such as 'echo ok'."
            ),
            exit_code=EXIT_GENERAL_ERROR,
            data=output,
        )
        sys.exit(EXIT_GENERAL_ERROR)

    if json_output:
        emit_success(ctx, payload=output)
    return EXIT_SUCCESS


__all__ = ["benchmark_command"]
