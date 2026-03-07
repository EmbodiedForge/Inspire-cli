"""Tests for notebook post-start action resolution."""

from __future__ import annotations

from pathlib import Path

from inspire.cli.utils.keepalive import KEEPALIVE_LOG, KEEPALIVE_STARTED_MARKER
from inspire.cli.utils.notebook_post_start import (
    POST_START_LOG,
    POST_START_PRESET_KEEPALIVE,
    POST_START_STARTED_MARKER,
    resolve_notebook_post_start_spec,
)
from inspire.config.models import Config


def _config(*, post_start: str = POST_START_PRESET_KEEPALIVE) -> Config:
    return Config(
        username="user",
        password="pass",
        notebook_post_start=post_start,
    )


def test_resolve_notebook_post_start_spec_uses_keepalive_by_default() -> None:
    spec = resolve_notebook_post_start_spec(
        config=_config(),
        post_start=None,
        post_start_script=None,
        keepalive=None,
    )

    assert spec is not None
    assert spec.requires_gpu is True
    assert spec.log_path == KEEPALIVE_LOG
    assert spec.completion_marker == KEEPALIVE_STARTED_MARKER


def test_resolve_notebook_post_start_spec_can_disable_default() -> None:
    spec = resolve_notebook_post_start_spec(
        config=_config(post_start="none"),
        post_start=None,
        post_start_script=None,
        keepalive=None,
    )

    assert spec is None


def test_resolve_notebook_post_start_spec_prefers_explicit_command() -> None:
    spec = resolve_notebook_post_start_spec(
        config=_config(),
        post_start="echo hi",
        post_start_script=None,
        keepalive=True,
    )

    assert spec is not None
    assert spec.requires_gpu is False
    assert spec.log_path == POST_START_LOG
    assert POST_START_STARTED_MARKER in spec.command
    assert "nohup bash -lc " in spec.command


def test_resolve_notebook_post_start_spec_builds_script_from_file(tmp_path: Path) -> None:
    script_path = tmp_path / "bootstrap.sh"
    script_path.write_text("#!/usr/bin/env bash\necho hello\n", encoding="utf-8")

    spec = resolve_notebook_post_start_spec(
        config=_config(post_start="none"),
        post_start=None,
        post_start_script=script_path,
        keepalive=None,
    )

    assert spec is not None
    assert spec.requires_gpu is False
    assert spec.label.endswith(f"({script_path.name})")
    assert "base64 -d" in spec.command
    assert spec.log_path == POST_START_LOG


def test_resolve_notebook_post_start_spec_keepalive_flag_overrides_config_command() -> None:
    spec = resolve_notebook_post_start_spec(
        config=_config(post_start="echo hi"),
        post_start=None,
        post_start_script=None,
        keepalive=True,
    )

    assert spec is not None
    assert spec.requires_gpu is True
    assert spec.completion_marker == KEEPALIVE_STARTED_MARKER
