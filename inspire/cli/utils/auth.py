"""Authentication management for Inspire CLI.

Provides authenticated API client with token caching.
"""

import json
import time
from typing import Optional, Tuple

from inspire.platform.openapi import AuthenticationError, InspireAPI, InspireConfig
from inspire.config import Config


class AuthManager:
    """Manages authentication and provides API client instances.

    Caches tokens for reuse within a session (tokens expire after ~1 hour).

    CACHE INVALIDATION: The cache key includes workspace_id because resource
    specs are workspace-scoped. When users switch between projects with
    different workspaces, the cache must invalidate to ensure fresh specs
    are probed from the browser API.
    """

    _token: Optional[str] = None
    _expires_at: float = 0
    _api: Optional[InspireAPI] = None
    _cache_key: Optional[Tuple] = None

    @classmethod
    def _make_cache_key(cls, config: Config) -> Tuple:
        """Create cache key that includes workspace for spec isolation.

        Since specs are workspace-dependent, the cached API must be invalidated
        when the target workspace changes to ensure correct specs are probed.

        Args:
            config: Configuration object

        Returns:
            Tuple of hashable values representing the cache key
        """
        workspace_hint = config.job_workspace_id or config.default_workspace_id

        return (
            config.base_url,
            config.username,
            config.password,
            config.timeout,
            config.max_retries,
            config.retry_delay,
            config.skip_ssl_verify,
            config.force_proxy,
            config.openapi_prefix,
            config.auth_endpoint,
            config.docker_registry,
            json.dumps(config.compute_groups, sort_keys=True),
            workspace_hint,
        )

    @classmethod
    def get_api(cls, config: Optional[Config] = None) -> InspireAPI:
        """Get an authenticated API client.

        Args:
            config: Configuration to use. If None, reads from environment.

        Returns:
            Authenticated InspireAPI instance

        Raises:
            ConfigError: If required environment variables are missing
            AuthenticationError: If authentication fails
        """
        if config is None:
            config = Config.from_env()

        cache_key = cls._make_cache_key(config)

        # Check if we have a valid cached token AND matching config
        if (
            cls._api is not None
            and cls._token
            and time.time() < cls._expires_at
            and cls._cache_key == cache_key
        ):
            return cls._api

        # Create new API client
        api_config = InspireConfig(
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=config.max_retries,
            retry_delay=config.retry_delay,
            verify_ssl=not config.skip_ssl_verify,
            force_proxy=config.force_proxy,
            openapi_prefix=config.openapi_prefix,
            auth_endpoint=config.auth_endpoint,
            docker_registry=config.docker_registry,
            compute_groups=config.compute_groups,
            workspace_specs=config.workspace_specs,
        )
        api = InspireAPI(api_config)

        # Authenticate
        try:
            api.authenticate(config.username, config.password)
        except AuthenticationError as e:
            raise AuthenticationError(f"Authentication failed: {e}") from e
        except Exception as e:
            raise AuthenticationError(f"Authentication request failed: {e}") from e

        # Cache the token (expire 10 minutes early for safety)
        cls._token = api.token
        cls._expires_at = time.time() + 3000  # ~50 minutes
        cls._api = api
        cls._cache_key = cache_key

        return api

    @classmethod
    def clear_cache(cls) -> None:
        """Clear cached authentication."""
        cls._token = None
        cls._expires_at = 0
        cls._api = None
        cls._cache_key = None
