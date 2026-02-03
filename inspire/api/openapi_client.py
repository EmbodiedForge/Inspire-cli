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

import json
import logging
import os
import time
from typing import Any, Dict, Optional

import requests
import urllib3

from inspire.api.openapi_endpoints import APIEndpoints
from inspire.api.openapi_errors import (
    AuthenticationError,
    InspireAPIError,
    JobCreationError,
    JobNotFoundError,
    ValidationError,
    _translate_api_error,
    _validate_job_id_format,
)
from inspire.api.openapi_models import InspireConfig
from inspire.api.openapi_resources import ResourceManager

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

DEFAULT_SHM_ENV_VAR = "INSPIRE_SHM_SIZE"


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
    DEFAULT_DOCKER_REGISTRY = "docker.example.com"
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
        if os.getenv("INSPIRE_SKIP_SSL_VERIFY", "").lower() in ("1", "true", "yes"):
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
        if os.getenv("INSPIRE_FORCE_PROXY", "").lower() in ("1", "true", "yes"):
            http_proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
            https_proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
            if http_proxy or https_proxy:
                self.session.proxies = {
                    "http": http_proxy or https_proxy,
                    "https": https_proxy or http_proxy,
                }
                logger.debug(
                    f"INSPIRE_FORCE_PROXY enabled, using explicit proxy configuration: {self.session.proxies}"
                )

    def _validate_required_params(self, **kwargs) -> None:
        """Validate required parameters."""
        for param_name, param_value in kwargs.items():
            if param_value is None or (isinstance(param_value, str) and not param_value.strip()):
                raise ValidationError(f"Required parameter '{param_name}' cannot be empty")

    def _make_request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """Request method with retry mechanism."""
        last_exception = None
        # Add SSL verification setting to kwargs if not already present
        if "verify" not in kwargs:
            kwargs["verify"] = self.config.verify_ssl

        for attempt in range(self.config.max_retries + 1):
            try:
                if method.upper() == "POST":
                    response = self.session.post(url, timeout=self.config.timeout, **kwargs)
                else:
                    response = self.session.get(url, timeout=self.config.timeout, **kwargs)

                if response.status_code < 500:
                    return response
                else:
                    # Check if server returned an API error in JSON body (don't retry these)
                    try:
                        error_body = response.json()
                        error_code = error_body.get("code")
                        error_msg = error_body.get("message", "")
                        if error_code is not None and error_code != 0:
                            # This is an API-level error, not a transient server error
                            # Don't retry - return immediately so caller can handle it
                            logger.warning(
                                f"API error {error_code}: {error_msg} (HTTP {response.status_code})"
                            )
                            return response
                    except (ValueError, KeyError):
                        pass  # Not JSON or missing fields, treat as normal 500

                    if attempt < self.config.max_retries:
                        logger.warning(
                            f"Server error {response.status_code}, retrying in {self.config.retry_delay}s..."
                        )
                        time.sleep(self.config.retry_delay * (attempt + 1))
                        continue
                    else:
                        response.raise_for_status()

            except requests.exceptions.Timeout as e:
                last_exception = e
                if attempt < self.config.max_retries:
                    logger.warning(f"Request timeout, retrying in {self.config.retry_delay}s...")
                    time.sleep(self.config.retry_delay * (attempt + 1))
                    continue
                else:
                    raise InspireAPIError(
                        f"Request timeout after {self.config.max_retries} retries"
                    )

            except requests.exceptions.ConnectionError as e:
                last_exception = e
                if attempt < self.config.max_retries:
                    logger.warning(f"Connection error, retrying in {self.config.retry_delay}s...")
                    time.sleep(self.config.retry_delay * (attempt + 1))
                    continue
                else:
                    raise InspireAPIError(
                        f"Connection error after {self.config.max_retries} retries"
                    )

        # Should not reach here
        raise InspireAPIError(f"Request failed: {str(last_exception)}")

    def _make_request(self, method: str, endpoint: str, payload: Optional[Dict] = None) -> Dict:
        """Make authenticated request to API."""
        url = f"{self.base_url}{endpoint}"

        # Add auth header if token exists
        headers = self.headers.copy()
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        # Log request details
        logger.debug(f"API Request: {method} {url}")
        logger.debug(f"Headers: {json.dumps(headers, ensure_ascii=False)}")
        if payload:
            logger.debug(f"Payload: {json.dumps(payload, ensure_ascii=False)}")

        try:
            if method.upper() == "POST":
                response = self._make_request_with_retry(
                    method,
                    url,
                    headers=headers,
                    json=payload,
                )
            else:
                response = self._make_request_with_retry(method, url, headers=headers)

            # Try to parse JSON response
            try:
                result = response.json()
            except json.JSONDecodeError:
                body_preview = (response.text or "")[: self.ERROR_BODY_PREVIEW_LIMIT]
                raise InspireAPIError(
                    "Failed to parse API response as JSON.\n"
                    f"HTTP {response.status_code} from {url}\n"
                    f"Body (first {self.ERROR_BODY_PREVIEW_LIMIT} chars): {body_preview}"
                )

            return result

        except requests.exceptions.RequestException as e:
            raise InspireAPIError(f"API request failed: {str(e)}")

    def authenticate(self, username: str, password: str) -> bool:
        """Authenticate and get access token."""
        self._validate_required_params(username=username, password=password)

        payload = {"username": username, "password": password}

        result = self._make_request("POST", self.endpoints.AUTH_TOKEN, payload)

        if result.get("code") == 0:
            token = result.get("data", {}).get("access_token")
            if token:
                self.token = token
                logger.info("✅ Authentication successful!")
                return True
            else:
                raise AuthenticationError("Authentication succeeded but no token returned")
        else:
            error_msg = result.get("message", "Authentication failed")
            raise AuthenticationError(f"❌ Authentication failed: {error_msg}")

    def _check_authentication(self) -> None:
        """Check if authenticated."""
        if not self.token:
            raise AuthenticationError(
                "Not authenticated. Please call authenticate() first or provide valid credentials."
            )

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
    ) -> Dict[str, Any]:
        """
        Create training job with smart resource matching.
        """
        self._check_authentication()

        # Validate required parameters
        self._validate_required_params(name=name, command=command, resource=resource)

        # Get recommended configuration
        try:
            spec_id, compute_group_id = self.resource_manager.get_recommended_config(
                resource, prefer_location
            )
        except ValueError as e:
            raise ValidationError(f"Resource configuration error: {str(e)}")

        # Use defaults for optional parameters
        project_id = project_id or self.DEFAULT_PROJECT_ID
        workspace_id = workspace_id or self.DEFAULT_WORKSPACE_ID
        task_priority = task_priority or self.DEFAULT_TASK_PRIORITY
        instance_count = instance_count or self.DEFAULT_INSTANCE_COUNT
        max_running_time_ms = max_running_time_ms or self.DEFAULT_MAX_RUNNING_TIME

        # Set default shared memory size
        if shm_gi is None:
            shm_gi = self.DEFAULT_SHM_SIZE

        # Image configuration
        final_image = image or self._get_default_image()
        # Determine registry
        if self.config.docker_registry:
            docker_registry = self.config.docker_registry
        else:
            # Extract registry from image
            docker_registry = (
                final_image.split("/")[0] if "/" in final_image else self.DEFAULT_DOCKER_REGISTRY
            )

        payload = {
            "name": name,
            "start_cmd": command,
            "framework": framework,
            "spec_id": spec_id,
            "logic_compute_group_id": compute_group_id,
            "project_id": project_id,
            "workspace_id": workspace_id,
            "task_priority": task_priority,
            "instance_count": instance_count,
            "max_running_time_ms": max_running_time_ms,
            "shm_gi": shm_gi,
            "image": {
                "image_type": self.DEFAULT_IMAGE_TYPE,
                "image_url": final_image,
                "docker_registry": docker_registry,
            },
        }

        try:
            result = self._make_request("POST", self.endpoints.TRAIN_JOB_CREATE, payload)

            if result.get("code") == 0:
                job_id = result["data"].get("job_id")
                logger.info(f"🚀 Training job created successfully! Job ID: {job_id}")
                return result
            else:
                error_code = result.get("code")
                error_msg = result.get("message", "Unknown error")
                friendly_msg = _translate_api_error(error_code, error_msg)
                raise JobCreationError(f"Failed to create training job: {friendly_msg}")

        except requests.exceptions.RequestException as e:
            raise JobCreationError(f"Training job creation request failed: {str(e)}")

    def get_job_detail(self, job_id: str) -> Dict[str, Any]:
        """Get training job details."""
        self._check_authentication()
        self._validate_required_params(job_id=job_id)

        # Validate job ID format before making API call
        format_error = _validate_job_id_format(job_id)
        if format_error:
            raise JobNotFoundError(f"Invalid job ID '{job_id}': {format_error}")

        payload = {"job_id": job_id}

        result = self._make_request("POST", self.endpoints.TRAIN_JOB_DETAIL, payload)

        if result.get("code") == 0:
            logger.info(f"📋 Retrieved details for job {job_id}")
            return result
        else:
            error_code = result.get("code")
            error_msg = result.get("message", "Unknown error")
            friendly_msg = _translate_api_error(error_code, error_msg)
            # Use specific exception for parameter errors (likely invalid job ID)
            if error_code == 100002:
                raise JobNotFoundError(f"Failed to get job details for '{job_id}': {friendly_msg}")
            raise InspireAPIError(f"Failed to get job details: {friendly_msg}")

    def stop_training_job(self, job_id: str) -> bool:
        """Stop training job."""
        self._check_authentication()
        self._validate_required_params(job_id=job_id)

        # Validate job ID format before making API call
        format_error = _validate_job_id_format(job_id)
        if format_error:
            raise JobNotFoundError(f"Invalid job ID '{job_id}': {format_error}")

        payload = {"job_id": job_id}

        result = self._make_request("POST", self.endpoints.TRAIN_JOB_STOP, payload)

        if result.get("code") == 0:
            logger.info(f"🛑 Training job {job_id} stopped successfully.")
            return True
        else:
            error_code = result.get("code")
            error_msg = result.get("message", "Unknown error")
            friendly_msg = _translate_api_error(error_code, error_msg)
            if error_code == 100002:
                raise JobNotFoundError(f"Failed to stop job '{job_id}': {friendly_msg}")
            raise InspireAPIError(f"Failed to stop training job: {friendly_msg}")

    def list_cluster_nodes(
        self, page_num: int = 1, page_size: int = 10, resource_pool: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get cluster node list."""
        self._check_authentication()

        if page_num < 1:
            raise ValidationError("Page number must be at least 1")
        if page_size < 1 or page_size > 1000:
            raise ValidationError("Page size must be between 1 and 1000")

        valid_pools = ["online", "backup", "fault", "unknown"]
        if resource_pool and resource_pool not in valid_pools:
            raise ValidationError(f"Resource pool must be one of: {valid_pools}")

        payload = {"page_num": page_num, "page_size": page_size}

        if resource_pool:
            payload["filter"] = {"resource_pool": resource_pool}

        result = self._make_request("POST", self.endpoints.CLUSTER_NODES_LIST, payload)

        if result.get("code") == 0:
            node_count = len(result["data"].get("nodes", []))
            logger.info(f"🖥️  Retrieved {node_count} nodes successfully.")
            return result
        else:
            error_msg = result.get("message", "Unknown error")
            raise InspireAPIError(f"Failed to get node list: {error_msg}")
