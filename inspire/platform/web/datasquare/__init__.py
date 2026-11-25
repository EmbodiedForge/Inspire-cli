"""SII data-square portal (aip.sii.edu.cn) client.

Separate from :mod:`inspire.platform.web.browser_api`, which speaks to the compute platform:
different host, different session cookie, different response envelope. Only the on-disk
``WebSession`` shape and the requests-session builder are shared.
"""

from __future__ import annotations

from .client import (
    DATASQUARE_CODE_UNAUTHORIZED,
    DatasquareAPIError,
    DatasquareSessionExpired,
    datasquare_request_json,
)
from .datasets import (
    find_dataset,
    get_datasets_with_versions,
    list_datasets,
)
from .session import (
    DATASQUARE_BASE_URL_ENV,
    DATASQUARE_SESSION_TTL,
    DEFAULT_DATASQUARE_BASE_URL,
    get_datasquare_base_url,
    get_datasquare_session,
    get_datasquare_session_cache_file,
    is_datasquare_session_valid,
    save_datasquare_session,
)

__all__ = [
    "DATASQUARE_BASE_URL_ENV",
    "DATASQUARE_CODE_UNAUTHORIZED",
    "DATASQUARE_SESSION_TTL",
    "DEFAULT_DATASQUARE_BASE_URL",
    "DatasquareAPIError",
    "DatasquareSessionExpired",
    "datasquare_request_json",
    "find_dataset",
    "get_datasets_with_versions",
    "get_datasquare_base_url",
    "get_datasquare_session",
    "get_datasquare_session_cache_file",
    "is_datasquare_session_valid",
    "list_datasets",
    "save_datasquare_session",
]
