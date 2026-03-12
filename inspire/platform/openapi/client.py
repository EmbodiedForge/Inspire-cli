"""Inspire OpenAPI client (extracted from legacy script).

Provides functionality to:
- Authenticate with the Inspire API
- Create distributed training jobs with smart resource matching
- Query training job details
- Stop training jobs
- List cluster nodes

New Features:
- Natural language resource specification (e.g., "H200", "H100", "4xH200")
- Automatic spec-id and compute-group-id matching
- Interactive resource selection
- Enhanced user experience

API Documentation: https://api.example.com/openapi/
"""

import logging
import os
from typing import Any, Dict, Optional

import requests
import urllib3

from inspire.platform.openapi.auth import authenticate as _authenticate
from inspire.platform.openapi.auth import check_authentication as _check_authentication
from inspire.platform.openapi.http import make_request as _make_request
from inspire.platform.openapi.http import make_request_with_retry as _make_request_with_retry
from inspire.platform.openapi.jobs import create_training_job_smart as _create_training_job_smart
from inspire.platform.openapi.jobs import get_job_detail as _get_job_detail
from inspire.platform.openapi.jobs import stop_training_job as _stop_training_job
from inspire.platform.openapi.nodes import list_cluster_nodes as _list_cluster_nodes
from inspire.platform.openapi.endpoints import APIEndpoints
from inspire.platform.openapi.errors import (
    ValidationError,
)
from inspire.platform.openapi.models import InspireConfig
from inspire.platform.openapi.resources import ResourceManager

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

DEFAULT_SHM_ENV_VAR = "INSPIRE_SHM_SIZE"


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def _get_default_shm_size(fallback: int = 200) -> int:
    """Read default shared memory size from env, falling back to a sane default."""
    env_value = os.getenv(DEFAULT_SHM_ENV_VAR)
    if env_value:
        try:
            value = int(env_value)
            if value >= 1:
                return value
            logger.warning(
                "Environment variable %s must be >= 1 (got %s). Falling back to %s Gi.",
                DEFAULT_SHM_ENV_VAR,
                env_value,
                fallback,
            )
        except ValueError:
            logger.warning(
                "Environment variable %s must be an integer (got %s). Falling back to %s Gi.",
                DEFAULT_SHM_ENV_VAR,
                env_value,
                fallback,
            )
    return fallback


class InspireAPI:
    """
    Inspire API Client - Smart Resource Matching Version
    """

    # Default value constants
    DEFAULT_TASK_PRIORITY = 8
    DEFAULT_INSTANCE_COUNT = 1
    DEFAULT_SHM_SIZE = _get_default_shm_size()
    DEFAULT_MAX_RUNNING_TIME = "360000000"  # 100 hours
    DEFAULT_IMAGE_TYPE = "SOURCE_PRIVATE"
    DEFAULT_PROJECT_ID = os.getenv(
        "INSPIRE_PROJECT_ID",
        "project-00000000-0000-0000-0000-000000000000",  # Placeholder - set INSPIRE_PROJECT_ID env var
    )
    DEFAULT_WORKSPACE_ID = os.getenv(
        "INSPIRE_WORKSPACE_ID",
        "ws-00000000-0000-0000-0000-000000000000",  # Placeholder - set INSPIRE_WORKSPACE_ID env var
    )
    DEFAULT_IMAGE = "docker.example.com/inspire-studio/ngc-cuda12.8-base:1.0"
    DEFAULT_IMAGE_PATH = "inspire-studio/ngc-cuda12.8-base:1.0"
    ERROR_BODY_PREVIEW_LIMIT = 4000

    def _get_default_image(self) -> str:
        """Get the default Docker image, using configurable registry if set."""
        if self.config.docker_registry:
            return f"{self.config.docker_registry}/{self.DEFAULT_IMAGE_PATH}"
        return self.DEFAULT_IMAGE

    def __init__(self, config: Optional[InspireConfig] = None):
        """
        Initialize API client.

        Args:
            config: API configuration object, uses default config if None
        """
        self.config = config or InspireConfig()

        # Check for SSL verification override via environment variable
        if _env_enabled("INSPIRE_SKIP_SSL_VERIFY"):
            self.config.verify_ssl = False

        self.base_url = self.config.base_url.rstrip("/")
        self.token = None
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}

        # Initialize API endpoints with configurable prefixes
        self.endpoints = APIEndpoints(
            auth_endpoint=self.config.auth_endpoint,
            openapi_prefix=self.config.openapi_prefix,
        )

        # Initialize resource manager
        self.resource_manager = ResourceManager(self.config.compute_groups)

        # Use simple requests session
        self.session = requests.Session()
        # Enable proxy and no_proxy support from environment by default
        self.session.trust_env = True

        # Optional override: force using proxy even if no_proxy would normally bypass it.
        # This preserves the previous WSL corporate-proxy workaround when needed.
        if self.config.force_proxy or _env_enabled("INSPIRE_FORCE_PROXY"):
            http_proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
            https_proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
            if http_proxy or https_proxy:
                self.session.proxies = {
                    "http": http_proxy or https_proxy,
                    "https": https_proxy or http_proxy,
                }
                logger.debug(
                    "Force-proxy enabled, using explicit proxy configuration: %s",
                    self.session.proxies,
                )

    def _validate_required_params(self, **kwargs) -> None:
        """Validate required parameters."""
        for param_name, param_value in kwargs.items():
            if param_value is None or (isinstance(param_value, str) and not param_value.strip()):
                raise ValidationError(f"Required parameter '{param_name}' cannot be empty")

    def _make_request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        return _make_request_with_retry(self, method, url, **kwargs)

    def _make_request(self, method: str, endpoint: str, payload: Optional[Dict] = None) -> Dict:
        return _make_request(self, method, endpoint, payload)

    def authenticate(self, username: str, password: str) -> bool:
        return _authenticate(self, username, password)

    def _check_authentication(self) -> None:
        _check_authentication(self)

    def create_training_job_smart(
        self,
        name: str,
        command: str,
        resource: str,
        framework: str = "pytorch",
        prefer_location: Optional[str] = None,
        project_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        image: Optional[str] = None,
        task_priority: Optional[int] = None,
        instance_count: Optional[int] = None,
        max_running_time_ms: Optional[str] = None,
        shm_gi: Optional[int] = None,
        auto_fault_tolerance: bool = False,
    ) -> Dict[str, Any]:
        return _create_training_job_smart(
            self,
            name=name,
            command=command,
            resource=resource,
            framework=framework,
            prefer_location=prefer_location,
            project_id=project_id,
            workspace_id=workspace_id,
            image=image,
            task_priority=task_priority,
            instance_count=instance_count,
            max_running_time_ms=max_running_time_ms,
            shm_gi=shm_gi,
            auto_fault_tolerance=auto_fault_tolerance,
        )

    def get_job_detail(self, job_id: str) -> Dict[str, Any]:
        return _get_job_detail(self, job_id)

    def stop_training_job(self, job_id: str) -> bool:
        return _stop_training_job(self, job_id)

    def list_cluster_nodes(
        self, page_num: int = 1, page_size: int = 10, resource_pool: Optional[str] = None
    ) -> Dict[str, Any]:
        return _list_cluster_nodes(
            self,
            page_num=page_num,
            page_size=page_size,
            resource_pool=resource_pool,
        )
