"""Forge model classes and errors."""

from __future__ import annotations

from enum import Enum

from inspire.config import ConfigError


class GitPlatform(Enum):
    """Supported Git platforms for Actions."""

    GITEA = "gitea"
    GITHUB = "github"


class ForgeAuthError(ConfigError):
    """Authentication/configuration error for forge access."""


class ForgeError(Exception):
    """Generic forge API or workflow error."""


class GiteaAuthError(ForgeAuthError):
    """Authentication error for Gitea (backward compatibility alias)."""


class GiteaError(ForgeError):
    """Generic Gitea error (backward compatibility alias)."""
