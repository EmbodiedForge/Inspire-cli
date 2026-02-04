"""Forge client implementations for GitHub Actions and Gitea Actions."""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from inspire.config import Config

from .config import _get_active_server, _get_active_token, _resolve_platform
from .models import ForgeError, GitPlatform


@dataclass
class ForgeClient(ABC):
    """Abstract base class for Git forge clients."""

    token: str
    server_url: str

    @abstractmethod
    def get_auth_header(self) -> str:
        """Return the Authorization header value."""

    @abstractmethod
    def get_api_base(self, repo: str) -> str:
        """Return the API base URL for the given repo."""

    @abstractmethod
    def get_raw_file_url(self, repo: str, branch: str, filepath: str) -> str:
        """Return the URL to fetch a raw file."""

    @abstractmethod
    def get_pagination_params(self, limit: int, page: int) -> str:
        """Return query string for pagination (platform-specific)."""

    def _build_request(
        self,
        method: str,
        url: str,
        data: Optional[dict] = None,
        accept: str = "application/json",
    ) -> urlrequest.Request:
        headers = {
            "Authorization": self.get_auth_header(),
            "Accept": accept,
            "User-Agent": "inspire-cli",
        }
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            body = None

        req = urlrequest.Request(url, data=body, headers=headers)
        req.get_method = lambda: method  # type: ignore[assignment]
        return req

    def request_json(self, method: str, url: str, data: Optional[dict] = None) -> dict:
        """Make a JSON request with retry."""
        max_retries = 3
        retry_delay = 2.0

        for attempt in range(max_retries + 1):
            try:
                req = self._build_request(method, url, data)
                with urlrequest.urlopen(req, timeout=60) as resp:
                    charset = resp.headers.get_content_charset("utf-8")
                    payload = resp.read().decode(charset)
                    if not payload:
                        return {}
                    return json.loads(payload)
            except urlerror.HTTPError as e:
                detail = None
                try:
                    raw = e.read().decode("utf-8")
                    parsed = json.loads(raw)
                    detail = parsed.get("message") or parsed.get("error")
                except Exception:
                    pass
                msg = f"API error {e.code} for {url}"
                if detail:
                    msg += f": {detail}"

                if e.code >= 500 and attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ForgeError(msg)
            except urlerror.URLError as e:
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ForgeError(f"API request failed for {url}: {e}")

        return {}

    def request_bytes(self, method: str, url: str) -> bytes:
        """Make a binary request with retry."""
        max_retries = 3
        retry_delay = 2.0

        for attempt in range(max_retries + 1):
            try:
                logging.debug(
                    "Forge request_bytes %s %s (attempt %d)",
                    method,
                    url,
                    attempt + 1,
                )
                req = self._build_request(method, url, data=None, accept="application/octet-stream")
                with urlrequest.urlopen(req, timeout=120) as resp:
                    return resp.read()
            except urlerror.HTTPError as e:
                debug_body = ""
                try:
                    raw = e.read()
                    if raw:
                        debug_body = raw.decode("utf-8", "replace")[:500]
                except Exception:
                    pass
                logging.debug(
                    "Forge HTTPError %s for %s, body=%r",
                    e.code,
                    url,
                    debug_body,
                )
                msg = f"API error {e.code} for {url}"
                if e.code >= 500 and attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ForgeError(msg)
            except urlerror.URLError as e:
                logging.debug(
                    "Forge URLError for %s: %s (attempt %d)",
                    url,
                    e,
                    attempt + 1,
                )
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ForgeError(f"API request failed for {url}: {e}")

        return b""


@dataclass
class GiteaClient(ForgeClient):
    """Client for Gitea/Forgejo/Codeberg Actions API."""

    def get_auth_header(self) -> str:
        """Gitea uses 'token {token}' format."""
        return f"token {self.token}"

    def get_api_base(self, repo: str) -> str:
        """Gitea API base path."""
        return f"{self.server_url}/api/v1/repos/{repo}/actions"

    def get_raw_file_url(self, repo: str, branch: str, filepath: str) -> str:
        """Gitea raw file URL."""
        return f"{self.server_url}/api/v1/repos/{repo}/raw/{branch}/{filepath}"

    def get_pagination_params(self, limit: int, page: int) -> str:
        """Gitea uses limit instead of per_page."""
        return f"limit={limit}&page={page}"


@dataclass
class GitHubClient(ForgeClient):
    """Client for GitHub Actions API."""

    def get_auth_header(self) -> str:
        """GitHub uses 'Bearer {token}' format."""
        return f"Bearer {self.token}"

    def get_api_base(self, repo: str) -> str:
        """GitHub API base path.

        Note: GitHub API is at api.github.com, not github.com.
        For GitHub Enterprise, it's {host}/api/v3/repos/...
        """
        if self.server_url == "https://github.com":
            return f"https://api.github.com/repos/{repo}/actions"
        else:
            # GitHub Enterprise
            return f"{self.server_url}/api/v3/repos/{repo}/actions"

    def get_raw_file_url(self, repo: str, branch: str, filepath: str) -> str:
        """GitHub raw file URL (uses different domain)."""
        # Extract hostname for raw URL
        if self.server_url == "https://github.com":
            raw_base = "https://raw.githubusercontent.com"
        else:
            # GitHub Enterprise or custom
            raw_base = self.server_url.replace("https://", "https://raw.")

        return f"{raw_base}/{repo}/{branch}/{filepath}"

    def get_pagination_params(self, limit: int, page: int) -> str:
        """GitHub uses per_page instead of limit."""
        return f"per_page={limit}&page={page}"


def create_forge_client(config: Config) -> ForgeClient:
    """Factory function to create the appropriate forge client.

    Args:
        config: CLI configuration

    Returns:
        GiteaClient or GitHubClient based on configured platform
    """
    platform = _resolve_platform(config)
    token = _get_active_token(config)
    server_url = _get_active_server(config)

    if platform == GitPlatform.GITHUB:
        return GitHubClient(token=token, server_url=server_url)
    else:
        return GiteaClient(token=token, server_url=server_url)
