"""Forge configuration helpers.

Resolves the active Git platform (Gitea vs GitHub) and extracts the relevant settings from
`inspire.cli.utils.config.Config`.
"""

from __future__ import annotations

import os

from inspire.config import Config

from .models import ForgeAuthError, GitPlatform


def _sanitize_token(token: str) -> str:
    """Sanitize a token by removing common prefixes."""
    token = token.strip()
    lower = token.lower()
    if lower.startswith("bearer "):
        token = token[7:].strip()
    elif lower.startswith("token "):
        token = token[6:].strip()
    return token


def _resolve_platform(config: Config) -> GitPlatform:
    """Resolve which Git platform to use from config.

    Priority:
    1. INSP_GIT_PLATFORM / git.platform setting (if explicitly set)
    2. Auto-detect from GitHub vars if set
    3. Default to GITEA for backward compatibility
    """
    # Check environment variable first
    platform_env = os.getenv("INSP_GIT_PLATFORM", "").strip().lower()

    # Check config setting
    platform_config = (getattr(config, "git_platform", None) or "").strip().lower()

    # Use env var if set, otherwise use config
    platform_str = platform_env if platform_env else platform_config

    if platform_str == "github":
        return GitPlatform.GITHUB
    elif platform_str == "gitea":
        return GitPlatform.GITEA

    # Auto-detect: if GitHub vars are set, use GitHub
    if config.github_repo or config.github_token:
        return GitPlatform.GITHUB

    # Default to Gitea for backward compatibility
    return GitPlatform.GITEA


def _get_active_repo(config: Config) -> str:
    """Get the repository from the active platform config."""
    platform = _resolve_platform(config)

    if platform == GitPlatform.GITHUB:
        repo = (getattr(config, "github_repo", None) or "").strip()
        if not repo:
            raise ForgeAuthError(
                "GitHub operations require INSP_GITHUB_REPO to be set.\n"
                "Use 'owner/repo' format.\n"
                "Example: export INSP_GITHUB_REPO='my-org/my-repo'"
            )
        if "/" not in repo:
            raise ForgeAuthError(
                f"Invalid INSP_GITHUB_REPO format '{repo}'. Expected 'owner/repo'."
            )
        return repo
    else:
        repo = (config.gitea_repo or "").strip()
        if not repo:
            raise ForgeAuthError(
                "Gitea operations require INSP_GITEA_REPO to be set.\n"
                "Use 'owner/repo' format.\n"
                "Example: export INSP_GITEA_REPO='my-org/my-repo'"
            )
        if "/" not in repo:
            raise ForgeAuthError(f"Invalid INSP_GITEA_REPO format '{repo}'. Expected 'owner/repo'.")
        return repo


def _get_active_token(config: Config) -> str:
    """Get the token from the active platform config."""
    platform = _resolve_platform(config)

    if platform == GitPlatform.GITHUB:
        token = (getattr(config, "github_token", None) or "").strip()
        if not token:
            raise ForgeAuthError("GitHub operations require INSP_GITHUB_TOKEN to be set.")
        return _sanitize_token(token)
    else:
        token = (config.gitea_token or "").strip()
        if not token:
            raise ForgeAuthError("Gitea operations require INSP_GITEA_TOKEN to be set.")
        return _sanitize_token(token)


def _get_active_server(config: Config) -> str:
    """Get the server URL from the active platform config."""
    platform = _resolve_platform(config)

    if platform == GitPlatform.GITHUB:
        return (getattr(config, "github_server", None) or "https://github.com").rstrip("/")
    else:
        return (config.gitea_server or "https://codeberg.org").rstrip("/")


def _get_active_workflow_file(config: Config, workflow_type: str) -> str:
    """Get the workflow filename from the active platform config.

    Args:
        config: CLI configuration
        workflow_type: One of 'log', 'sync', 'bridge'
    """
    platform = _resolve_platform(config)

    if platform == GitPlatform.GITHUB:
        if workflow_type == "log":
            return getattr(config, "github_log_workflow", "retrieve_job_log.yml")
        elif workflow_type == "sync":
            return getattr(config, "github_sync_workflow", "sync_code.yml")
        elif workflow_type == "bridge":
            return getattr(config, "github_bridge_workflow", "run_bridge_action.yml")
    else:
        if workflow_type == "log":
            return config.gitea_log_workflow
        elif workflow_type == "sync":
            return config.gitea_sync_workflow
        elif workflow_type == "bridge":
            return config.gitea_bridge_workflow

    # Default fallback
    return "workflow.yml"
