"""Browser (web-session) dataset APIs on the compute platform.

One endpoint lives here: ``ValidateDataset``, the platform's own answer to "can this
dataset/version actually be mounted in this workspace?". Whether a dataset *exists* is the data
square's business (see :mod:`inspire.platform.web.datasquare`); whether a training job can start
with it attached is decided here, per item, and the two answers are not interchangeable.
"""

from __future__ import annotations

from typing import Any, Optional

from inspire.platform.web.browser_api.core import _get_base_url, _request_json
from inspire.platform.web.browser_api.errors import get_browser_api_action_result
from inspire.platform.web.session import WebSession, get_web_session

VALIDATE_DATASET_PATH = "/api/v2/dataset?Action=ValidateDataset"

__all__ = ["VALIDATE_DATASET_PATH", "validate_datasets"]


def _datasets_referer() -> str:
    """The console page that normally issues this call.

    Validation is what the distributed-training form does before it lets a dataset be attached,
    so that is the referer the endpoint expects to see.
    """
    return f"{_get_base_url()}/jobs/distributedTraining"


def validate_datasets(
    pairs: list[dict[str, Any]],
    workspace_id: str,
    session: Optional[WebSession] = None,
) -> list[dict[str, Any]]:
    """Ask the platform whether each ``dataset_id``/``version_id`` pair is mountable.

    Every pair is validated independently and the answer comes back one entry per pair, so a
    rejected version is data in ``datasets_result`` rather than an exception. Only a transport
    or platform-level failure raises: this function keeps the ``ResponseMetadata.Error`` check
    the escape-hatch command applies, and refuses an answer it cannot read.
    """
    if session is None:
        session = get_web_session()

    payload = _request_json(
        session,
        "POST",
        VALIDATE_DATASET_PATH,
        referer=_datasets_referer(),
        body={"datasets": pairs, "workspace_id": workspace_id},
        timeout=30,
    )
    result = get_browser_api_action_result(payload, action="ValidateDataset")
    datasets_result = result.get("datasets_result")
    if not isinstance(datasets_result, list):
        raise ValueError("ValidateDataset failed: platform Result.datasets_result is not a list")
    return [item for item in datasets_result if isinstance(item, dict)]
