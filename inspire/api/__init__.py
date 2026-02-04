"""Deprecated API package.

The OpenAPI client now lives in `inspire.platform.openapi`. This module re-exports the public
API for internal compatibility (legacy script and older imports).
"""

from inspire.platform.openapi import (  # noqa: F401
    APIEndpoints,
    API_ERROR_CODES,
    AuthenticationError,
    ComputeGroup,
    DEFAULT_SHM_ENV_VAR,
    GPUType,
    InspireAPI,
    InspireAPIError,
    InspireConfig,
    JobCreationError,
    JobNotFoundError,
    ResourceManager,
    ResourceSpec,
    ValidationError,
    _translate_api_error,
    _validate_job_id_format,
)

__all__ = [
    "APIEndpoints",
    "API_ERROR_CODES",
    "AuthenticationError",
    "ComputeGroup",
    "DEFAULT_SHM_ENV_VAR",
    "GPUType",
    "InspireAPI",
    "InspireAPIError",
    "InspireConfig",
    "JobCreationError",
    "JobNotFoundError",
    "ResourceManager",
    "ResourceSpec",
    "ValidationError",
    "_translate_api_error",
    "_validate_job_id_format",
]
