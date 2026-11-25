"""Dataset catalogue endpoints on the SII data-square portal (aip.sii.edu.cn).

Three read-only endpoints back `inspire dataset`: the paged catalogue, a code-to-versions
lookup, and the full detail record. Field names are returned in the portal's own camelCase and
are kept verbatim here - the snake_case contract belongs to the CLI layer, and translating in
both layers would mean two places to keep honest about one backend.
"""

from __future__ import annotations

from typing import Any, Optional

from inspire.platform.web.session import WebSession

from .client import datasquare_request_json

DATASETS_LIST_PATH = "/api/datasets/getDatasetsList"
DATASETS_WITH_VERSIONS_PATH = "/api/datasets/getDatasetsWithVersions"
FIND_DATASETS_PATH = "/api/datasets/findDatasets"


def list_datasets(
    session: WebSession,
    keyword: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """List catalogue datasets; ``keyword`` searches dataset code and description."""
    params: dict[str, Any] = {"page": page, "pageSize": page_size}
    if keyword:
        params["keyword"] = keyword
    data = datasquare_request_json(session, "GET", DATASETS_LIST_PATH, params=params)
    return data if isinstance(data, dict) else {}


def get_datasets_with_versions(session: WebSession, keyword: str) -> list[Any]:
    """Match datasets by code and return each match with its version list.

    The portal's ``keyword`` here matches the dataset code only, and it matches loosely, so the
    caller must expect more than one record for an exact-looking code.
    """
    data = datasquare_request_json(
        session,
        "GET",
        DATASETS_WITH_VERSIONS_PATH,
        params={"keyword": keyword},
    )
    return data if isinstance(data, list) else []


def find_dataset(session: WebSession, dataset_id: int) -> dict[str, Any]:
    """Fetch one dataset record with its per-version storage and replication detail."""
    data = datasquare_request_json(
        session, "POST", FIND_DATASETS_PATH, body={"datasetId": dataset_id}
    )
    return data if isinstance(data, dict) else {}


__all__ = [
    "DATASETS_LIST_PATH",
    "DATASETS_WITH_VERSIONS_PATH",
    "FIND_DATASETS_PATH",
    "find_dataset",
    "get_datasets_with_versions",
    "list_datasets",
]
