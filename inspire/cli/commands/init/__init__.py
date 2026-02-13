"""Init command helpers.

This module is the stable import surface for the `inspire init` command and its helper
functions used by tests.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import click

from inspire.cli.context import (
    Context,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import (
    CONFIG_FILENAME,
    PROJECT_CONFIG_DIR,
    Config,
)

from .discover import _derive_shared_path_group, _init_discover_mode
from .env_detect import _detect_env_vars, _generate_toml_content
from .templates import CONFIG_TEMPLATE, _init_smart_mode, _init_template_mode


@click.command()
@click.option(
    "--json",
    "json_output_local",
    is_flag=True,
    help="Output as JSON (machine-readable). Equivalent to top-level --json.",
)
@click.option(
    "--global",
    "-g",
    "global_flag",
    is_flag=True,
    help="Force all options to global config (~/.config/inspire/)",
)
@click.option(
    "--project",
    "-p",
    "project_flag",
    is_flag=True,
    help="Force all options to project config (./.inspire/)",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Overwrite existing files without prompting",
)
@click.option(
    "--discover",
    is_flag=True,
    help="Discover projects/workspaces and write per-account catalog",
)
@click.option(
    "--probe-shared-path",
    is_flag=True,
    help=(
        "Probe shared filesystem paths by SSHing into a small CPU notebook per project "
        "(slow; creates notebooks)."
    ),
)
@click.option(
    "--probe-limit",
    type=int,
    default=0,
    show_default=True,
    help=(
        "Limit number of projects to probe (0 = all). "
        "Only effective with --discover --probe-shared-path."
    ),
)
@click.option(
    "--probe-keep-notebooks",
    is_flag=True,
    help=(
        "Keep probe notebooks running (do not stop them after probing). "
        "Only effective with --discover --probe-shared-path."
    ),
)
@click.option(
    "--probe-pubkey",
    "--pubkey",
    "probe_pubkey",
    default=None,
    help=(
        "SSH public key path for probing (defaults to ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub). "
        "Only effective with --discover --probe-shared-path."
    ),
)
@click.option(
    "--probe-timeout",
    type=int,
    default=900,
    show_default=True,
    help=(
        "Per-project probe timeout in seconds. "
        "Only effective with --discover --probe-shared-path."
    ),
)
@click.option(
    "--template",
    "-t",
    "template_flag",
    is_flag=True,
    help="Create template with placeholders (skip env var detection)",
)
@pass_context
def init(
    ctx: Context,
    json_output_local: bool,
    global_flag: bool,
    project_flag: bool,
    force: bool,
    discover: bool,
    probe_shared_path: bool,
    probe_limit: int,
    probe_keep_notebooks: bool,
    probe_pubkey: str | None,
    probe_timeout: int,
    template_flag: bool,
) -> None:
    """Initialize Inspire CLI configuration.

    Detects environment variables and creates config.toml files.
    By default, options are auto-split by scope: global options go to
    ~/.config/inspire/config.toml, project options go to ./.inspire/config.toml.

    Use --global or --project to force all options to a single file.
    Secrets (passwords, tokens) are never written to config files for security.

    If no environment variables are detected (or with --template), creates
    a template config with placeholder values.

    Use --discover to login via the web UI, discover accessible projects and
    compute groups, and write an account-scoped catalog to the global config.

    \b
    Examples:
        # Auto-detect env vars and split by scope
        inspire init

        \b
        # Force all options to global config
        inspire init --global

        \b
        # Force all options to project config
        inspire init --project

        \b
        # Create template with placeholders
        inspire init --template

        \b
        # Discover projects/workspaces and write per-account catalog
        inspire init --discover
    """
    ctx.json_output = bool(ctx.json_output or json_output_local)
    effective_json = ctx.json_output

    global_path, project_path = _get_config_paths()
    before = _snapshot_paths(global_path, project_path)
    warnings: list[str] = []

    if not discover and (
        probe_limit or probe_keep_notebooks or probe_pubkey or probe_timeout != 900
    ):
        warnings.append(
            "Probe options are only effective with --discover --probe-shared-path and were ignored."
        )

    try:
        if global_flag and project_flag:
            raise ValueError("Cannot specify both --global and --project")

        if discover:
            if template_flag:
                raise ValueError("Cannot combine --discover with --template")
            if global_flag or project_flag:
                raise ValueError("--discover always writes both global and project config")

            if effective_json and not force and (global_path.exists() or project_path.exists()):
                raise ValueError(
                    "JSON mode is non-interactive for discover updates; rerun with --force when "
                    "config files already exist."
                )

            _run_init_action(
                _init_discover_mode,
                effective_json,
                force,
                probe_shared_path=probe_shared_path,
                probe_limit=probe_limit,
                probe_keep_notebooks=probe_keep_notebooks,
                probe_pubkey=probe_pubkey,
                probe_timeout=probe_timeout,
            )

            _emit_init_json(
                mode="discover",
                target_paths=[global_path, project_path],
                before=before,
                detected=[],
                warnings=warnings,
                discover={
                    "probe_enabled": bool(probe_shared_path),
                    "probe_limit": int(probe_limit),
                    "probe_keep_notebooks": bool(probe_keep_notebooks),
                    "probe_timeout": int(probe_timeout),
                    "probe_pubkey_provided": bool(probe_pubkey),
                },
                effective_json=effective_json,
            )
            return

        if probe_shared_path:
            raise ValueError("--probe-shared-path requires --discover")

        if template_flag:
            if effective_json:
                if not global_flag and not project_flag:
                    # Match interactive default choice for machine mode.
                    project_flag = True

                target_path = global_path if global_flag else project_path
                if target_path.exists() and not force:
                    raise ValueError(
                        "JSON mode is non-interactive for overwrites; rerun with --force."
                    )
            else:
                click.echo("Creating template config with placeholders.\n")

            _run_init_action(_init_template_mode, effective_json, global_flag, project_flag, force)
            _emit_init_json(
                mode="template",
                target_paths=[global_path] if global_flag else [project_path],
                before=before,
                detected=[],
                warnings=warnings,
                effective_json=effective_json,
            )
            return

        detected = _detect_env_vars()

        if detected:
            if effective_json and not force:
                if global_flag and global_path.exists():
                    raise ValueError(
                        "JSON mode is non-interactive for overwrites; rerun with --force."
                    )
                if project_flag and project_path.exists():
                    raise ValueError(
                        "JSON mode is non-interactive for overwrites; rerun with --force."
                    )
                if (
                    not global_flag
                    and not project_flag
                    and (global_path.exists() or project_path.exists())
                ):
                    raise ValueError(
                        "JSON mode is non-interactive for overwrite prompts in auto-split mode; "
                        "rerun with --force."
                    )

            _run_init_action(
                _init_smart_mode, effective_json, detected, global_flag, project_flag, force
            )
            target_paths: list[Path]
            if global_flag:
                target_paths = [global_path]
            elif project_flag:
                target_paths = [project_path]
            else:
                has_global = any(opt.scope == "global" for opt, _ in detected)
                has_project = any(opt.scope == "project" for opt, _ in detected)
                target_paths = []
                if has_global:
                    target_paths.append(global_path)
                if has_project:
                    target_paths.append(project_path)
            _emit_init_json(
                mode="smart",
                target_paths=target_paths,
                before=before,
                detected=detected,
                warnings=warnings,
                effective_json=effective_json,
            )
            return

        if effective_json:
            if not global_flag and not project_flag:
                project_flag = True
            target_path = global_path if global_flag else project_path
            if target_path.exists() and not force:
                raise ValueError("JSON mode is non-interactive for overwrites; rerun with --force.")
        else:
            click.echo("No environment variables detected. Creating template config.\n")

        _run_init_action(_init_template_mode, effective_json, global_flag, project_flag, force)
        _emit_init_json(
            mode="template",
            target_paths=[global_path] if global_flag else [project_path],
            before=before,
            detected=[],
            warnings=warnings,
            effective_json=effective_json,
        )
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_GENERAL_ERROR)
    except SystemExit:
        raise
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)


def _run_init_action(func, json_mode: bool, *args, **kwargs) -> None:  # noqa: ANN001
    if not json_mode:
        func(*args, **kwargs)
        return

    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    try:
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
            func(*args, **kwargs)
    except SystemExit as e:
        combined = "\n".join([stdout_buffer.getvalue(), stderr_buffer.getvalue()])
        message = _extract_error_message(combined) or f"Command exited with code {e.code}"
        raise ValueError(message) from e


def _extract_error_message(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        trimmed = line.strip()
        if not trimmed:
            continue
        if trimmed.lower().startswith("error:"):
            return trimmed.split(":", 1)[1].strip()
        return trimmed
    return ""


def _get_config_paths() -> tuple[Path, Path]:
    global_path = Config.GLOBAL_CONFIG_PATH
    project_path = Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME
    return global_path, project_path


def _snapshot_paths(global_path: Path, project_path: Path) -> dict[str, dict[str, int | bool]]:
    snapshot: dict[str, dict[str, int | bool]] = {}
    for path in (global_path, project_path):
        exists = path.exists()
        snapshot[str(path)] = {
            "exists": exists,
            "mtime_ns": path.stat().st_mtime_ns if exists else 0,
        }
    return snapshot


def _resolve_write_state(
    before: dict[str, dict[str, int | bool]],
    after_path: Path,
) -> tuple[bool, bool]:
    key = str(after_path)
    prev = before.get(key, {"exists": False, "mtime_ns": 0})
    prev_exists = bool(prev.get("exists"))
    prev_mtime_ns = int(prev.get("mtime_ns", 0))
    now_exists = after_path.exists()
    if not now_exists:
        return False, bool(prev_exists)
    now_mtime_ns = after_path.stat().st_mtime_ns
    written = (not prev_exists) or (now_mtime_ns > prev_mtime_ns)
    skipped = bool(prev_exists and not written)
    return written, skipped


def _build_next_steps(mode: str) -> list[str]:
    if mode == "discover":
        return [
            'Ensure a password is available via INSPIRE_PASSWORD or [accounts."<username>"].password',
            "Run: inspire config show",
        ]
    return [
        "Set INSPIRE_USERNAME and INSPIRE_PASSWORD if needed",
        "Run: inspire config show",
    ]


def _emit_init_json(
    *,
    mode: str,
    target_paths: list[Path],
    before: dict[str, dict[str, int | bool]],
    detected: list[tuple],
    warnings: list[str],
    effective_json: bool,
    discover: dict[str, object] | None = None,
) -> None:
    if not effective_json:
        return

    files_written: list[str] = []
    files_skipped: list[str] = []
    for path in target_paths:
        written, skipped = _resolve_write_state(before, path)
        if written:
            files_written.append(str(path))
        elif skipped:
            files_skipped.append(str(path))

    secret_count = 0
    for option, _ in detected:
        if getattr(option, "secret", False):
            secret_count += 1

    payload: dict[str, object] = {
        "mode": mode,
        "files_written": files_written,
        "files_skipped": files_skipped,
        "detected_env_count": len(detected),
        "secret_env_count": secret_count,
        "warnings": warnings,
        "next_steps": _build_next_steps(mode),
    }
    if discover is not None:
        payload["discover"] = discover

    click.echo(json_formatter.format_json(payload, success=True))


__all__ = [
    "CONFIG_TEMPLATE",
    "_detect_env_vars",
    "_derive_shared_path_group",
    "_generate_toml_content",
    "init",
]
