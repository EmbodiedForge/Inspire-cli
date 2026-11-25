"""Dataset references shared by every command that attaches a dataset version.

Three facts keep being needed in the same shape: how a ``DATASET_CODE@VERSION`` reference is
spelled, where a mounted version appears inside a container, and whether the platform will let that
version be attached in a given workspace at all. ``inspire dataset`` reports the third; the commands
that create workloads act on it, in the order the console uses - validate everything, then create.
Both read them here so that a preview, a check, and the resulting instance cannot disagree about
where a dataset lands.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from inspire.platform.web.browser_api import datasets as compute_datasets
from inspire.platform.web.session import WebSession

#: Where a mounted dataset version appears inside a container. A naming convention the platform
#: applies to ``dataset_info`` entries, not something the caller chooses.
MOUNT_PATH_PREFIX = "/inspire/dataset"

DATASET_REFERENCE_EXAMPLE = "PointWorld@v1"

#: The remedy every command offers when a reference cannot be read.
DATASET_REFERENCE_HINT = (
    f"Use DATASET_CODE@VERSION, for example {DATASET_REFERENCE_EXAMPLE}. "
    "List versions with: inspire dataset versions DATASET_CODE"
)

#: What a missing verdict means. The platform validates each pair independently and has never
#: dropped one, so silence is its own finding - never a pass.
NO_VERDICT_MESSAGE = "the platform returned no verdict for this pair"

#: The verdict shapes the platform's ValidateDataset has answered with: 2001 版本不存在
#: (version not mountable right now), 2000 数据集不存在 (unknown dataset code) and
#: 2005 无访问权限 (no access grant). The code never travels without the message.
_BACKEND_ERROR_CODE_RE = re.compile(r"code:\s*(\d+)")
_BACKEND_ERROR_PREFIX_RE = re.compile(r"^\s*code:\s*\d+\s*,\s*message:\s*")

__all__ = [
    "DATASET_REFERENCE_EXAMPLE",
    "DATASET_REFERENCE_HINT",
    "MOUNT_PATH_PREFIX",
    "NO_VERDICT_MESSAGE",
    "DatasetValidationError",
    "dataset_mount_path",
    "index_validation_results",
    "parse_backend_error",
    "parse_dataset_target",
    "validate_dataset_targets",
    "validation_pair_key",
]


class DatasetValidationError(Exception):
    """The platform refused to attach one dataset version.

    ``ValidateDataset`` answers per item, so exactly one pair is named here and it carries the
    platform's own ``error_message`` - the same text the console shows when its 「校验数据」 step
    blocks a submission.
    """

    def __init__(
        self,
        dataset_code: str,
        version_code: str,
        error_message: str,
        *,
        error_code: int | None = None,
    ) -> None:
        self.dataset_code = dataset_code
        self.version_code = version_code
        self.error_code = error_code
        self.error_message = error_message or NO_VERDICT_MESSAGE
        code_part = f" (code {error_code})" if error_code is not None else ""
        super().__init__(
            f"Dataset {dataset_code}@{version_code} cannot be mounted: "
            f"{self.error_message}{code_part}"
        )


def parse_dataset_target(text: str) -> tuple[str, str]:
    """Split ``DATASET_CODE@VERSION`` into its two halves.

    ``rsplit`` with one split because a code may itself contain ``@``; the version never does, so
    the last separator is the one the platform means.

    An all-digit half is the data square's numeric ``datasetId``/``versionId``, which the mount
    API does not accept - it answers ``2000 数据集不存在`` for the numbers even though the square's
    own UI keys everything off them. Refuse the shape here so the failure names the real remedy.

    Raises:
        ValueError: when either half is missing or all-numeric, carrying the syntax example.
    """
    parts = text.rsplit("@", 1)
    dataset_code = parts[0].strip() if len(parts) == 2 else ""
    version_code = parts[1].strip() if len(parts) == 2 else ""
    if not dataset_code or not version_code:
        raise ValueError(
            f"Invalid dataset reference: {text!r} "
            f"(expected DATASET_CODE@VERSION, for example {DATASET_REFERENCE_EXAMPLE})"
        )
    if dataset_code.isdigit() or version_code.isdigit():
        raise ValueError(
            f"Invalid dataset reference: {text!r}. The data square's numeric "
            "datasetId/versionId are not the mount reference; use the datasetCode and "
            "versionCode strings instead (see: inspire dataset list)"
        )
    return dataset_code, version_code


def dataset_mount_path(dataset_code: str, version_code: str) -> str:
    """The path a mounted dataset version appears at inside a job container."""
    return f"{MOUNT_PATH_PREFIX}/{dataset_code}/{version_code}"


def parse_backend_error(message: Any) -> tuple[int | None, str]:
    """Pull ``(code, message)`` out of ``code: 2001, message: 版本不存在``."""
    text = str(message or "").strip()
    if not text:
        return None, ""
    match = _BACKEND_ERROR_CODE_RE.search(text)
    code = int(match.group(1)) if match else None
    return code, _BACKEND_ERROR_PREFIX_RE.sub("", text).strip()


def validation_pair_key(dataset_code: Any, version_code: Any) -> tuple[str, str]:
    """The key a verdict is filed under.

    The platform is case-insensitive about a dataset code and exact about a version, so a lookup
    that treated both halves the same way would lose verdicts for code that was answered.
    """
    return (
        str(dataset_code or "").strip().lower(),
        str(version_code or "").strip(),
    )


def index_validation_results(
    results: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Verdicts keyed by pair, first answer winning when the platform repeats itself."""
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for item in results:
        indexed.setdefault(
            validation_pair_key(item.get("dataset_id"), item.get("version_id")), item
        )
    return indexed


def validate_dataset_targets(
    pairs: list[tuple[str, str]],
    workspace_id: str,
    session: WebSession,
) -> list[dict[str, str]]:
    """Turn ``DATASET_CODE@VERSION`` pairs into the ``dataset_info`` a create body needs, or refuse.

    Mirrors the console's order: one ``ValidateDataset`` call over the whole set, and no submission
    at all when any single version is rejected - an instance that starts with half of its data
    attached is worse than one that never started. Each answered entry contributes the backend
    ``path`` the platform resolved for it, which is what makes the mount real.

    Returns the entries in the order requested, shaped as the create bodies want them:
    ``{"dataset_id": code, "version_id": version, "path": backend_path}``. An empty request yields
    an empty list, which callers must not turn into a ``dataset_info`` field (the create protos
    reject what they do not expect).

    Raises:
        DatasetValidationError: the platform rejected a version, or never answered for it.

    Transport and session failures from the platform layer pass through untouched; each command
    already maps those onto its own exit codes.
    """
    if not pairs:
        return []

    verdicts = index_validation_results(
        compute_datasets.validate_datasets(
            [
                {"dataset_id": dataset_code, "version_id": version_code}
                for dataset_code, version_code in pairs
            ],
            workspace_id,
            session=session,
        )
    )

    dataset_info: list[dict[str, str]] = []
    for dataset_code, version_code in pairs:
        result = verdicts.get(validation_pair_key(dataset_code, version_code))
        if result is None:
            raise DatasetValidationError(dataset_code, version_code, NO_VERDICT_MESSAGE)
        if not result.get("success"):
            error_code, error_message = parse_backend_error(result.get("error_message"))
            raise DatasetValidationError(
                dataset_code, version_code, error_message, error_code=error_code
            )
        dataset_info.append(
            {
                "dataset_id": dataset_code,
                "version_id": version_code,
                "path": str(result.get("path") or ""),
            }
        )
    return dataset_info
