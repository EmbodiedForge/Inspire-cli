"""Inspire OpenAPI client façade.

The implementation is split across smaller modules. This module re-exports the public API to keep
import paths stable for `inspire.api`.
"""

from inspire.api.openapi_client import DEFAULT_SHM_ENV_VAR, InspireAPI
from inspire.api.openapi_endpoints import APIEndpoints
from inspire.api.openapi_errors import (
    API_ERROR_CODES,
    AuthenticationError,
    InspireAPIError,
    JobCreationError,
    JobNotFoundError,
    ValidationError,
    _translate_api_error,
    _validate_job_id_format,
)
from inspire.api.openapi_models import ComputeGroup, GPUType, InspireConfig, ResourceSpec
from inspire.api.openapi_resources import ResourceManager

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
