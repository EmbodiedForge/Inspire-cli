#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Inspire API Control Script - Smart Resource Matching Version

This script provides functionality to:
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

import os
import json
import logging
import requests
import argparse
import time
import re
from typing import Dict, Any, Optional, Tuple, List
from dataclasses import dataclass
from enum import Enum

from inspire.compute_groups import load_compute_groups_from_config

# Suppress SSL warnings when verification is disabled
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
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


class GPUType(Enum):
    """GPU type enumeration."""
    H100 = "H100"
    H200 = "H200"


@dataclass
class ResourceSpec:
    """Resource specification configuration."""
    gpu_type: GPUType
    gpu_count: int
    cpu_cores: int
    memory_gb: int
    gpu_memory_gb: int
    spec_id: str
    description: str


@dataclass
class ComputeGroup:
    """Compute group configuration."""
    name: str
    compute_group_id: str
    gpu_type: GPUType
    location: str = ""


@dataclass
class InspireConfig:
    """Inspire API configuration class."""
    base_url: str = "https://api.example.com"
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0
    verify_ssl: bool = True  # Can be disabled via INSPIRE_SKIP_SSL_VERIFY env var
    # API path prefixes (None = use code defaults)
    openapi_prefix: Optional[str] = None
    auth_endpoint: Optional[str] = None
    docker_registry: Optional[str] = None  # Docker registry hostname
    # Compute groups configuration
    compute_groups: Optional[list[dict]] = None  # List of compute group dicts from config


class ResourceManager:
    """Resource manager - handles resource spec and compute group matching."""

    def __init__(self, compute_groups_raw: Optional[list[dict]] = None):
        # Define available resource specs
        self.resource_specs = [
            ResourceSpec(
                gpu_type=GPUType.H200,
                gpu_count=1,
                cpu_cores=15,
                memory_gb=200,
                gpu_memory_gb=141,
                spec_id="4dd0e854-e2a4-4253-95e6-64c13f0b5117",
                description="1 × NVIDIA H200 (141GB) + 15 CPU cores + 200GB RAM"
            ),
            ResourceSpec(
                gpu_type=GPUType.H200,
                gpu_count=4,
                cpu_cores=60,
                memory_gb=800,
                gpu_memory_gb=141,
                spec_id="45ab2351-fc8a-4d50-a30b-b39a5306c906",
                description="4 × NVIDIA H200 (141GB) + 60 CPU cores + 800GB RAM"
            ),
            ResourceSpec(
                gpu_type=GPUType.H200,
                gpu_count=8,
                cpu_cores=120,
                memory_gb=1600,
                gpu_memory_gb=141,
                spec_id="b618f5cb-c119-4422-937e-f39131853076",
                description="8 × NVIDIA H200 (141GB) + 120 CPU cores + 1600GB RAM"
            )
        ]

        # Define available compute groups from config
        compute_groups_tuples = load_compute_groups_from_config(compute_groups_raw or [])
        self.compute_groups = [
            ComputeGroup(
                name=group.name,
                compute_group_id=group.compute_group_id,
                gpu_type=GPUType(group.gpu_type),
                location=group.location,
            )
            for group in compute_groups_tuples
        ]
    
    def parse_resource_request(self, resource_str: str) -> Tuple[GPUType, int]:
        """
        Parse natural language resource request.

        Args:
            resource_str: Resource description string, e.g., "H200", "4xH200", "8 H100"

        Returns:
            (GPU type, GPU count) tuple

        Raises:
            ValueError: When resource request cannot be parsed
        """
        if not resource_str:
            raise ValueError("Resource description cannot be empty")

        # Clean up and convert to uppercase
        resource_str = resource_str.upper().strip()

        # Match patterns: number + x/X + GPU type, or number + space + GPU type, or just GPU type
        patterns = [
            r'^(\d+)[xX]?(H100|H200)$',  # "4xH200", "4H200", "4 H200"
            r'^(H100|H200)[xX]?(\d+)?$',  # "H200", "H200x4", "H200 4"
            r'^(\d+)\s+(H100|H200)$',     # "4 H200"
        ]

        gpu_count = 1  # Default count
        gpu_type_str = None
        
        for pattern in patterns:
            match = re.match(pattern, resource_str.replace(' ', ''))
            if match:
                groups = match.groups()
                if len(groups) == 2:
                    # 可能是 (数字, GPU类型) 或 (GPU类型, 数字)
                    if groups[0].isdigit():
                        gpu_count = int(groups[0])
                        gpu_type_str = groups[1]
                    elif groups[1] and groups[1].isdigit():
                        gpu_type_str = groups[0]
                        gpu_count = int(groups[1])
                    else:
                        gpu_type_str = groups[0] if not groups[0].isdigit() else groups[1]
                break
        
        # If no number+GPU pattern matched, try to match GPU type directly
        if not gpu_type_str:
            if 'H200' in resource_str:
                gpu_type_str = 'H200'
            elif 'H100' in resource_str:
                gpu_type_str = 'H100'

        if not gpu_type_str:
            raise ValueError(f"Unrecognized GPU type: {resource_str}")

        try:
            gpu_type = GPUType(gpu_type_str)
        except ValueError:
            raise ValueError(f"Unsupported GPU type: {gpu_type_str}, supported types: H100, H200")

        if gpu_count <= 0:
            raise ValueError(f"GPU count must be positive: {gpu_count}")
        
        return gpu_type, gpu_count
    
    def find_matching_specs(self, gpu_type: GPUType, gpu_count: int) -> List[ResourceSpec]:
        """
        Find matching resource specs.

        Args:
            gpu_type: GPU type
            gpu_count: Required GPU count

        Returns:
            List of matching resource specs
        """
        matching_specs = []

        for spec in self.resource_specs:
            # For H100, since spec_id is the same, H200 specs can be used
            if (spec.gpu_type == gpu_type or
                (gpu_type == GPUType.H100 and spec.gpu_type == GPUType.H200)):
                if spec.gpu_count >= gpu_count:
                    matching_specs.append(spec)

        # Sort by GPU count, prefer configurations closest to requirements
        matching_specs.sort(key=lambda x: x.gpu_count)
        return matching_specs

    def find_compute_groups(self, gpu_type: GPUType) -> List[ComputeGroup]:
        """
        Find matching compute groups.

        Args:
            gpu_type: GPU type

        Returns:
            List of matching compute groups
        """
        return [group for group in self.compute_groups if group.gpu_type == gpu_type]

    def get_recommended_config(self, resource_str: str, prefer_location: Optional[str] = None) -> Tuple[str, str]:
        """
        Get recommended configuration.

        Args:
            resource_str: Resource description string
            prefer_location: Preferred datacenter location

        Returns:
            (spec_id, compute_group_id) tuple

        Raises:
            ValueError: When no matching configuration is found
        """
        gpu_type, gpu_count = self.parse_resource_request(resource_str)

        # Find matching specs
        matching_specs = self.find_matching_specs(gpu_type, gpu_count)
        if not matching_specs:
            available_configs = [f"{spec.gpu_count}x{spec.gpu_type.value}"
                               for spec in self.resource_specs]
            raise ValueError(
                f"No configuration found matching {gpu_count}x{gpu_type.value}. "
                f"Available configurations: {', '.join(available_configs)}"
            )

        # Select the most suitable spec (smallest that meets requirements)
        selected_spec = matching_specs[0]

        # Find matching compute groups
        matching_groups = self.find_compute_groups(gpu_type)
        if not matching_groups:
            raise ValueError(f"No compute group found supporting {gpu_type.value}")

        # Select compute group (consider location preference)
        selected_group = matching_groups[0]  # Default to first one

        if prefer_location:
            matched = False

            # Step 1: Try substring match
            for group in matching_groups:
                if prefer_location.lower() in group.location.lower():
                    selected_group = group
                    matched = True
                    break

            # Step 2: Try number-based semantic match
            if not matched:
                numbers = re.findall(r'\d+', prefer_location)
                if numbers:
                    for num in numbers:
                        for group in matching_groups:
                            if num in group.location:
                                selected_group = group
                                matched = True
                                break
                        if matched:
                            break

            # Step 3: Error if nothing matched
            if not matched:
                available_locations = [g.location for g in matching_groups]
                raise ValueError(
                    f"Location '{prefer_location}' not found for {gpu_type.value}. "
                    f"Available locations: {', '.join(available_locations)}"
                )

        return selected_spec.spec_id, selected_group.compute_group_id
    
    def display_available_resources(self) -> None:
        """Display all available resource configurations."""
        print("\n📊 Available Resource Configurations:")
        print("=" * 60)

        print("\n🖥️  GPU Spec Configurations:")
        for spec in self.resource_specs:
            print(f"  • {spec.description}")
            print(f"    Spec ID: {spec.spec_id}")

        print("\n🏢 Compute Groups:")
        for group in self.compute_groups:
            print(f"  • {group.name} ({group.location})")
            print(f"    Compute Group ID: {group.compute_group_id}")

        print("\n💡 Usage Examples:")
        print("  • --resource 'H200'     -> 1x H200 GPU")
        print("  • --resource '4xH200'   -> 4x H200 GPU")
        print("  • --resource '8 H200'   -> 8x H200 GPU")
        print("  • --resource 'H100'     -> 1x H100 GPU")
        print("=" * 60)


class APIEndpoints:
    """API endpoint paths with configurable prefixes.

    Uses configured prefixes if provided, otherwise falls back to
    hardcoded defaults for backward compatibility.
    """

    # Default fallback values
    DEFAULT_AUTH_ENDPOINT = "/auth/token"
    DEFAULT_OPENAPI_PREFIX = "/openapi/v1"

    def __init__(self, auth_endpoint: Optional[str] = None, openapi_prefix: Optional[str] = None):
        """Initialize API endpoints with optional configurable prefixes.

        Args:
            auth_endpoint: Custom auth endpoint path (e.g., "/custom/auth")
            openapi_prefix: Custom OpenAPI prefix (e.g., "/custom/api/v1")
        """
        self._auth_endpoint = auth_endpoint or self.DEFAULT_AUTH_ENDPOINT
        self._openapi_prefix = openapi_prefix or self.DEFAULT_OPENAPI_PREFIX

    @property
    def AUTH_TOKEN(self) -> str:
        return self._auth_endpoint

    @property
    def TRAIN_JOB_CREATE(self) -> str:
        return f"{self._openapi_prefix}/train_job/create"

    @property
    def TRAIN_JOB_DETAIL(self) -> str:
        return f"{self._openapi_prefix}/train_job/detail"

    @property
    def TRAIN_JOB_STOP(self) -> str:
        return f"{self._openapi_prefix}/train_job/stop"

    @property
    def CLUSTER_NODES_LIST(self) -> str:
        return f"{self._openapi_prefix}/cluster_nodes/list"


class InspireAPIError(Exception):
    """Inspire API base exception."""
    pass


class AuthenticationError(InspireAPIError):
    """Authentication failed exception."""
    pass


class JobCreationError(InspireAPIError):
    """Job creation failed exception."""
    pass


class ValidationError(InspireAPIError):
    """Input validation failed exception."""
    pass


class JobNotFoundError(InspireAPIError):
    """Job not found or invalid job ID"""
    pass


# Known API error codes from Inspire platform
API_ERROR_CODES = {
    100002: "Parameter error - the job ID may be invalid, truncated, or the job no longer exists",
    100001: "Authentication error",
    100003: "Permission denied",
    100004: "Resource not found",
}


def _translate_api_error(code: int, message: str) -> str:
    """Translate API error code to a helpful message."""
    hint = API_ERROR_CODES.get(code)
    if hint:
        return f"{message} ({hint})"
    return message


# Job ID format: job-<uuid> where uuid is 8-4-4-4-12 hex chars
JOB_ID_PATTERN = re.compile(r'^job-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)
JOB_ID_EXPECTED_LENGTH = 40  # "job-" (4) + UUID with hyphens (36)


def _validate_job_id_format(job_id: str) -> Optional[str]:
    """Validate job ID format and return a helpful message if invalid.

    Returns None if valid, or an error message if invalid.
    """
    if not job_id:
        return "Job ID cannot be empty"

    if not job_id.startswith("job-"):
        return f"Job ID should start with 'job-', got: {job_id[:20]}..."

    if JOB_ID_PATTERN.match(job_id):
        return None  # Valid

    # Try to give a helpful hint
    actual_len = len(job_id)
    if actual_len < JOB_ID_EXPECTED_LENGTH:
        missing = JOB_ID_EXPECTED_LENGTH - actual_len
        return (f"Job ID appears to be truncated (got {actual_len} chars, expected {JOB_ID_EXPECTED_LENGTH}). "
                f"Missing {missing} character(s). Did you copy the full ID?")
    elif actual_len > JOB_ID_EXPECTED_LENGTH:
        return f"Job ID is too long (got {actual_len} chars, expected {JOB_ID_EXPECTED_LENGTH})"
    else:
        return f"Job ID format is invalid. Expected format: job-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"


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
        'INSPIRE_PROJECT_ID',
        "project-00000000-0000-0000-0000-000000000000" # Placeholder - set INSPIRE_PROJECT_ID env var
    )
    DEFAULT_WORKSPACE_ID = os.getenv(
        'INSPIRE_WORKSPACE_ID',
        "ws-00000000-0000-0000-0000-000000000000" # Placeholder - set INSPIRE_WORKSPACE_ID env var
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
        if os.getenv('INSPIRE_SKIP_SSL_VERIFY', '').lower() in ('1', 'true', 'yes'):
            self.config.verify_ssl = False

        self.base_url = self.config.base_url.rstrip('/')
        self.token = None
        self.headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

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
        if os.getenv('INSPIRE_FORCE_PROXY', '').lower() in ('1', 'true', 'yes'):
            http_proxy = os.environ.get('http_proxy') or os.environ.get('HTTP_PROXY')
            https_proxy = os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY')
            if http_proxy or https_proxy:
                self.session.proxies = {
                    'http': http_proxy or https_proxy,
                    'https': https_proxy or http_proxy,
                }
                logger.debug(f"INSPIRE_FORCE_PROXY enabled, using explicit proxy configuration: {self.session.proxies}")
    
    def _validate_required_params(self, **kwargs) -> None:
        """Validate required parameters."""
        for param_name, param_value in kwargs.items():
            if param_value is None or (isinstance(param_value, str) and not param_value.strip()):
                raise ValidationError(f"Required parameter '{param_name}' cannot be empty")
    
    def _make_request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        """Request method with retry mechanism."""
        last_exception = None
        # Add SSL verification setting to kwargs if not already present
        if 'verify' not in kwargs:
            kwargs['verify'] = self.config.verify_ssl

        for attempt in range(self.config.max_retries + 1):
            try:
                if method.upper() == 'POST':
                    response = self.session.post(url, timeout=self.config.timeout, **kwargs)
                else:
                    response = self.session.get(url, timeout=self.config.timeout, **kwargs)

                if response.status_code < 500:
                    return response
                else:
                    # Check if server returned an API error in JSON body (don't retry these)
                    try:
                        error_body = response.json()
                        error_code = error_body.get('code')
                        error_msg = error_body.get('message', '')
                        if error_code is not None and error_code != 0:
                            # This is an API-level error, not a transient server error
                            # Don't retry - return immediately so caller can handle it
                            logger.warning(f"API error {error_code}: {error_msg} (HTTP {response.status_code})")
                            return response
                    except (ValueError, KeyError):
                        pass  # Not JSON or missing fields, treat as normal 500

                    if attempt < self.config.max_retries:
                        logger.warning(f"Server error {response.status_code}, retrying in {self.config.retry_delay}s...")
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
                    raise InspireAPIError(f"Request timeout after {self.config.max_retries} retries")

            except requests.exceptions.ConnectionError as e:
                last_exception = e
                if attempt < self.config.max_retries:
                    logger.warning(f"Connection error, retrying in {self.config.retry_delay}s...")
                    time.sleep(self.config.retry_delay * (attempt + 1))
                    continue
                else:
                    raise InspireAPIError(f"Connection error after {self.config.max_retries} retries: {str(e)}")
            
            except requests.exceptions.SSLError as e:
                last_exception = e
                if not self.config.verify_ssl:
                    logger.warning(f"SSL error detected, but SSL verification is disabled. This may be normal with corporate proxies.")
                if attempt < self.config.max_retries:
                    logger.warning(f"SSL error, retrying in {self.config.retry_delay}s...")
                    time.sleep(self.config.retry_delay * (attempt + 1))
                    continue
                else:
                    error_msg = str(e)
                    if not self.config.verify_ssl:
                        error_msg += "\n💡 Hint: SSL verification is disabled (INSPIRE_SKIP_SSL_VERIFY=1). If this persists, check your proxy settings or firewall."
                    raise InspireAPIError(f"SSL error after {self.config.max_retries} retries: {error_msg}")

            except requests.exceptions.RequestException as e:
                raise InspireAPIError(f"Request failed: {str(e)}")

        if last_exception:
            raise InspireAPIError(f"All retry attempts failed. Last error: {str(last_exception)}")
        else:
            raise InspireAPIError("All retry attempts failed")

    def _summarize_response_error(self, response: Optional[requests.Response]) -> str:
        """Format HTTP error response with status, URL, headers, and truncated body."""
        if response is None:
            return "No HTTP response available."
        headers = {k: v for k, v in response.headers.items()}
        body_preview = response.text or ""
        truncated = False
        if len(body_preview) > self.ERROR_BODY_PREVIEW_LIMIT:
            body_preview = body_preview[:self.ERROR_BODY_PREVIEW_LIMIT]
            truncated = True
        summary_lines = [
            f"Status: {response.status_code} {response.reason}",
            f"URL: {response.url}",
            f"Headers: {json.dumps(headers, ensure_ascii=False)}",
            "Body:",
            (body_preview.strip() or "<empty>")
        ]
        if truncated:
            summary_lines.append(f"... (truncated to {self.ERROR_BODY_PREVIEW_LIMIT} characters)")
        return "\n".join(summary_lines)

    def _make_request(self, method: str, endpoint: str, payload: Optional[Dict] = None) -> Dict[str, Any]:
        """Generic method for sending HTTP requests."""
        url = f"{self.base_url}{endpoint}"
        response: Optional[requests.Response] = None

        try:
            kwargs = {'headers': self.headers}
            if payload is not None:
                kwargs['json'] = payload

            response = self._make_request_with_retry(method, url, **kwargs)

            logger.debug(f"Request: {method} {url}")
            logger.debug(f"Response status: {response.status_code}")

            response.raise_for_status()
            result = response.json()

            if not isinstance(result, dict) or 'code' not in result:
                raise InspireAPIError("Invalid API response format")

            return result

        except requests.exceptions.HTTPError as http_err:
            error_summary = self._summarize_response_error(http_err.response or response)
            logger.error("❌ Inspire API returned non-success response.\n%s", error_summary)
            raise InspireAPIError(f"HTTP error while requesting {endpoint}: {error_summary}") from http_err
        except json.JSONDecodeError:
            body_preview = (response.text[:self.ERROR_BODY_PREVIEW_LIMIT] + "..."
                            if response and len(response.text) > self.ERROR_BODY_PREVIEW_LIMIT
                            else (response.text if response else "<no response>"))
            raise InspireAPIError(f"Invalid JSON response from API. Body preview: {body_preview}")
        except requests.exceptions.RequestException as e:
            raise InspireAPIError(f"Request failed: {str(e)}")
    
    def authenticate(self, username: str, password: str) -> bool:
        """Authenticate with username and password to obtain access token."""
        self._validate_required_params(username=username, password=password)
        
        payload = {
            "username": username,
            "password": password
        }
        
        try:
            result = self._make_request('POST', self.endpoints.AUTH_TOKEN, payload)
            
            if result.get('code') == 0:
                self.token = result['data']['access_token']
                self.headers['Authorization'] = f"Bearer {self.token}"
                expires_in = result['data'].get('expires_in', 'unknown')
                logger.info(f"🔐 Authentication successful. Token expires in {expires_in} seconds.")
                return True
            else:
                error_msg = result.get('message', 'Unknown authentication error')
                raise AuthenticationError(f"Authentication failed: {error_msg}")
                
        except InspireAPIError as e:
            if "Authentication failed" in str(e):
                raise
            raise AuthenticationError(f"Authentication request failed: {str(e)}")
    
    def _check_authentication(self) -> None:
        """Check if authenticated."""
        if not self.token:
            raise AuthenticationError("Not authenticated. Please authenticate first.")
    
    def create_training_job_smart(self,
                                name: str,
                                command: str,
                                resource: str,
                                framework: str = "pytorch",
                                prefer_location: Optional[str] = None,
                                project_id: Optional[str] = None,
                                workspace_id: Optional[str] = None,
                                image: Optional[str] = None,
                                task_priority: int = DEFAULT_TASK_PRIORITY,
                                instance_count: int = DEFAULT_INSTANCE_COUNT,
                                shm_gi: int = DEFAULT_SHM_SIZE,
                                max_running_time_ms: str = DEFAULT_MAX_RUNNING_TIME,
                                auto_fault_tolerance: bool = False,
                                enable_notification: bool = False,
                                enable_troubleshoot: bool = False,
                                **kwargs) -> Dict[str, Any]:
        """
        Smart distributed training job creation.

        Args:
            name: Training job name
            command: Start command
            resource: Resource description (e.g., "H200", "4xH200", "8 H200")
            framework: Training framework (default: pytorch)
            prefer_location: Preferred datacenter location (e.g., "Room1", "Room2")
            project_id: Project ID (optional, uses default)
            workspace_id: Workspace ID (optional, uses default)
            image: Image name (optional, uses default)
            task_priority: Task priority (default: 8)
            instance_count: Instance count (default: 1)
            shm_gi: Shared memory size (default: env var INSPIRE_SHM_SIZE or 200)
            max_running_time_ms: Max running time in ms (default: 360000000ms=100h)
            auto_fault_tolerance: Enable fault tolerance (default: False)
            enable_notification: Enable notifications (default: False)
            enable_troubleshoot: Enable troubleshooting (default: False)

        Returns:
            API response data

        Raises:
            ValidationError: When parameter validation fails
            JobCreationError: When job creation fails
            AuthenticationError: When not authenticated
        """
        self._check_authentication()

        # Validate required parameters
        self._validate_required_params(name=name, command=command, resource=resource)

        # Smart resource matching
        try:
            spec_id, compute_group_id = self.resource_manager.get_recommended_config(
                resource, prefer_location
            )
            logger.info(f"🎯 Smart resource matching:")
            logger.info(f"   Resource: {resource}")
            logger.info(f"   Spec ID: {spec_id}")
            logger.info(f"   Compute Group ID: {compute_group_id}")
        except ValueError as e:
            raise ValidationError(f"Resource matching failed: {str(e)}")

        # Fill optional parameters with defaults
        project_id = project_id or self.DEFAULT_PROJECT_ID
        workspace_id = workspace_id or self.DEFAULT_WORKSPACE_ID
        image = image or self._get_default_image()

        # Call the original create method
        return self.create_training_job(
            name=name,
            logic_compute_group_id=compute_group_id,
            project_id=project_id,
            workspace_id=workspace_id,
            framework=framework,
            command=command,
            spec_id=spec_id,
            task_priority=task_priority,
            auto_fault_tolerance=auto_fault_tolerance,
            enable_notification=enable_notification,
            enable_troubleshoot=enable_troubleshoot,
            image=image,
            instance_count=instance_count,
            shm_gi=shm_gi,
            max_running_time_ms=max_running_time_ms,
            **kwargs
        )
    
    def create_training_job(self, 
                           name: str, 
                           logic_compute_group_id: str, 
                           project_id: str,
                           workspace_id: str,
                           framework: str,
                           command: str,
                           spec_id: str,
                           task_priority: int = DEFAULT_TASK_PRIORITY,
                           auto_fault_tolerance: bool = False,
                           enable_notification: bool = False,
                           enable_troubleshoot: bool = False,
                           image: str = "",
                           image_type: str = DEFAULT_IMAGE_TYPE,
                           instance_count: int = DEFAULT_INSTANCE_COUNT,
                           shm_gi: int = DEFAULT_SHM_SIZE,
                           max_running_time_ms: str = DEFAULT_MAX_RUNNING_TIME,
                           reserve_on_fail_ms: str = "0",
                           reserve_on_success_ms: str = "0",
                           tb_summary_path: str = "",
                           dataset_info: Optional[list] = None,
                           envs: Optional[list] = None) -> Dict[str, Any]:
        """Create distributed training job (original method)."""
        self._check_authentication()

        # Validate required parameters
        self._validate_required_params(
            name=name,
            logic_compute_group_id=logic_compute_group_id,
            project_id=project_id,
            workspace_id=workspace_id,
            framework=framework,
            command=command,
            spec_id=spec_id
        )
        
        # Validate numeric parameters
        if instance_count < 1:
            raise ValidationError("Instance count must be at least 1")
        if shm_gi < 1:
            raise ValidationError("Shared memory size must be at least 1")
        if task_priority < 1 or task_priority > 10:
            raise ValidationError("Task priority must be between 1 and 10")

        # Use default image if not provided
        if not image:
            image = self._get_default_image()

        # Build request payload
        payload = {
            "name": name,
            "logic_compute_group_id": logic_compute_group_id,
            "project_id": project_id,
            "workspace_id": workspace_id,
            "framework": framework,
            "command": command,
            "task_priority": task_priority,
            "auto_fault_tolerance": auto_fault_tolerance,
            "enable_notification": enable_notification,
            "enable_troubleshoot": enable_troubleshoot,
            "max_running_time_ms": max_running_time_ms,
            "reserve_on_fail_ms": reserve_on_fail_ms,
            "reserve_on_success_ms": reserve_on_success_ms,
            "tb_summary_path": tb_summary_path,
            "framework_config": [{
                "image": image,
                "image_type": image_type,
                "instance_count": instance_count,
                "shm_gi": shm_gi,
                "spec_id": spec_id
            }],
            "dataset_info": dataset_info or [],
            "envs": envs or []
        }
        
        logger.debug("Creating training job with payload structure defined")
        
        try:
            result = self._make_request('POST', self.endpoints.TRAIN_JOB_CREATE, payload)
            
            if result.get('code') == 0:
                logger.info(f"✅ Training job '{name}' created successfully.")
                if 'data' in result and 'job_id' in result['data']:
                    logger.info(f"🆔 Job ID: {result['data']['job_id']}")
                return result
            else:
                error_msg = result.get('message', 'Unknown error')
                raise JobCreationError(f"Failed to create training job: {error_msg}")
                
        except InspireAPIError as e:
            if "Failed to create training job" in str(e):
                raise
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

        result = self._make_request('POST', self.endpoints.TRAIN_JOB_DETAIL, payload)

        if result.get('code') == 0:
            logger.info(f"📋 Retrieved details for job {job_id}")
            return result
        else:
            error_code = result.get('code')
            error_msg = result.get('message', 'Unknown error')
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

        result = self._make_request('POST', self.endpoints.TRAIN_JOB_STOP, payload)

        if result.get('code') == 0:
            logger.info(f"🛑 Training job {job_id} stopped successfully.")
            return True
        else:
            error_code = result.get('code')
            error_msg = result.get('message', 'Unknown error')
            friendly_msg = _translate_api_error(error_code, error_msg)
            if error_code == 100002:
                raise JobNotFoundError(f"Failed to stop job '{job_id}': {friendly_msg}")
            raise InspireAPIError(f"Failed to stop training job: {friendly_msg}")

    def list_cluster_nodes(self,
                          page_num: int = 1,
                          page_size: int = 10,
                          resource_pool: Optional[str] = None) -> Dict[str, Any]:
        """Get cluster node list."""
        self._check_authentication()
        
        if page_num < 1:
            raise ValidationError("Page number must be at least 1")
        if page_size < 1 or page_size > 1000:
            raise ValidationError("Page size must be between 1 and 1000")
        
        valid_pools = ['online', 'backup', 'fault', 'unknown']
        if resource_pool and resource_pool not in valid_pools:
            raise ValidationError(f"Resource pool must be one of: {valid_pools}")
        
        payload = {
            "page_num": page_num,
            "page_size": page_size
        }
        
        if resource_pool:
            payload["filter"] = {"resource_pool": resource_pool}
        
        result = self._make_request('POST', self.endpoints.CLUSTER_NODES_LIST, payload)
        
        if result.get('code') == 0:
            node_count = len(result['data'].get('nodes', []))
            logger.info(f"🖥️  Retrieved {node_count} nodes successfully.")
            return result
        else:
            error_msg = result.get('message', 'Unknown error')
            raise InspireAPIError(f"Failed to get node list: {error_msg}")


def get_credentials() -> tuple[str, str]:
    """Get credentials from environment variables."""
    username = os.getenv('INSPIRE_USERNAME')
    password = os.getenv('INSPIRE_PASSWORD')
    
    if not username:
        raise ValidationError(
            "❌ Username not found. Please set INSPIRE_USERNAME environment variable.\n"
            "   Example: export INSPIRE_USERNAME='your_username'"
        )
    
    if not password:
        raise ValidationError(
            "❌ Password not found. Please set INSPIRE_PASSWORD environment variable.\n"
            "   Example: export INSPIRE_PASSWORD='your_password'"
        )
    
    return username, password


def main():
    """Main function - provides command line interface."""
    parser = argparse.ArgumentParser(
        description='🚀 Inspire Platform API Smart Control Tool',
        epilog='Credentials provided via environment variables: INSPIRE_USERNAME and INSPIRE_PASSWORD\n'
               'Use --show-resources to view all available resource configurations',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # Global options
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    parser.add_argument('--base-url', type=str, default="https://api.example.com",
                       help='API base URL (default: https://api.example.com)')
    parser.add_argument('--show-resources', action='store_true',
                       help='Show all available resource configurations and exit')

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Smart training job creation
    create_parser = subparsers.add_parser('create', help='🎯 Smart distributed training job creation')
    create_parser.add_argument('--name', required=True, type=str, help='Training job name')
    create_parser.add_argument('--start-command', required=True, type=str, help='Start command')
    create_parser.add_argument('--resource', required=True, type=str,
                              help='Resource configuration (e.g., "H200", "4xH200", "8 H200", "H100")')
    create_parser.add_argument('--framework', type=str, default="pytorch",
                              help='Training framework (default: pytorch)')
    create_parser.add_argument('--location', type=str,
                              help='Preferred datacenter location (e.g., "Room1", "Room2")')
    create_parser.add_argument('--priority', type=int, default=8,
                              help='Task priority 1-10 (default: 8)')
    create_parser.add_argument('--image', type=str,
                              help='Custom image name (optional)')
    create_parser.add_argument('--instances', type=int, default=1,
                              help='Instance count (default: 1)')
    default_shm_size = InspireAPI.DEFAULT_SHM_SIZE
    create_parser.add_argument('--shm-size', type=int, default=default_shm_size,
                              help=f'Shared memory size (Gi) (default: {default_shm_size}, overridable via {DEFAULT_SHM_ENV_VAR})')
    create_parser.add_argument('--max-time-hours', type=float, default=100.0,
                              help='Max running time (hours) (default: 100)')
    create_parser.add_argument('--project-id', type=str,
                              help='Project ID (optional, uses default)')
    create_parser.add_argument('--workspace-id', type=str,
                              help='Workspace ID (optional, uses default)')
    create_parser.add_argument('--auto-fault-tolerance', action='store_true',
                              help='Enable auto fault tolerance')
    create_parser.add_argument('--enable-notification', action='store_true',
                              help='Enable notifications')
    create_parser.add_argument('--enable-troubleshoot', action='store_true',
                              help='Enable troubleshooting')

    # Query job details
    detail_parser = subparsers.add_parser('detail', help='📋 Query training job details')
    detail_parser.add_argument('--job-id', required=True, type=str, help='Job ID')

    # Stop training job
    stop_parser = subparsers.add_parser('stop', help='🛑 Stop training job')
    stop_parser.add_argument('--job-id', required=True, type=str, help='Job ID')

    # List cluster nodes
    list_parser = subparsers.add_parser('list-nodes', help='🖥️  List cluster nodes')
    list_parser.add_argument('--page', type=int, default=1, help='Page number (default: 1)')
    list_parser.add_argument('--size', type=int, default=10, help='Page size (default: 10)')
    list_parser.add_argument('--pool', type=str, choices=['online', 'backup', 'fault', 'unknown'],
                            help='Resource pool filter')
    
    args = parser.parse_args()

    # Show resource configuration and exit
    if args.show_resources:
        resource_manager = ResourceManager()
        resource_manager.display_available_resources()
        return 0

    # Set log level
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("🐛 Debug mode enabled")

    try:
        # Get credentials from environment variables
        username, password = get_credentials()

        # Create API client
        config = InspireConfig(base_url=args.base_url)
        api = InspireAPI(config)

        # Authenticate
        logger.info("🔐 Authenticating with Inspire API...")
        api.authenticate(username, password)

        # Execute corresponding operation based on command
        if args.command == 'create':
            # Convert hours to milliseconds
            max_time_ms = str(int(args.max_time_hours * 3600 * 1000))
            
            result = api.create_training_job_smart(
                name=args.name,
                command=args.start_command,
                resource=args.resource,
                framework=args.framework,
                prefer_location=args.location,
                project_id=args.project_id,
                workspace_id=args.workspace_id,
                image=args.image,
                task_priority=args.priority,
                instance_count=args.instances,
                shm_gi=args.shm_size,
                max_running_time_ms=max_time_ms,
                auto_fault_tolerance=args.auto_fault_tolerance,
                enable_notification=args.enable_notification,
                enable_troubleshoot=args.enable_troubleshoot
            )
            
            print("\n✅ Creation result:")
            print(json.dumps(result, indent=2, ensure_ascii=False))

        elif args.command == 'detail':
            result = api.get_job_detail(args.job_id)
            print("\n📋 Job details:")
            print(json.dumps(result, indent=2, ensure_ascii=False))

        elif args.command == 'stop':
            api.stop_training_job(args.job_id)
            print("🛑 Job stopped")

        elif args.command == 'list-nodes':
            result = api.list_cluster_nodes(
                page_num=args.page,
                page_size=args.size,
                resource_pool=args.pool
            )
            print("\n🖥️  Node list:")
            print(json.dumps(result, indent=2, ensure_ascii=False))

        else:
            parser.print_help()
            print("\n💡 Tip: Use --show-resources to view all available resource configurations")
            return 1
        
        return 0
        
    except (ValidationError, AuthenticationError, JobCreationError, InspireAPIError) as e:
        logger.error(f"❌ Error: {str(e)}")
        return 1
    except KeyboardInterrupt:
        logger.info("⏹️  Operation cancelled by user")
        return 1
    except Exception as e:
        logger.error(f"💥 Unexpected error: {str(e)}")
        if args.debug:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit(main())
