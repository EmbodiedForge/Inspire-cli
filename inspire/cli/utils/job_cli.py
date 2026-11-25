"""Job command helpers."""

from __future__ import annotations

import logging

from inspire.platform.openapi import _validate_job_id_format
from inspire.cli.context import Context, EXIT_JOB_NOT_FOUND
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.id_resolver import (
    is_full_uuid,
    is_partial_id,
    normalize_partial,
    resolve_partial_id,
)

logger = logging.getLogger(__name__)


def resolve_job_id(ctx: Context, job_id: str) -> str:
    """Resolve a full or partial job ID to a complete ``job-<uuid>`` string.

    Resolution order:
    1. Full UUID (with or without ``job-`` prefix) -> return directly.
    2. Partial hex -> search the platform's job listing.
    3. Not hex -> error with format hint.
    """
    job_id = job_id.strip()
    if not job_id:
        exit_with_error(ctx, "InvalidJobID", "Job ID cannot be empty", EXIT_JOB_NOT_FOUND)

    # Full UUID with prefix
    if is_full_uuid(job_id, prefix="job-"):
        # Ensure the job- prefix is present
        if not job_id.lower().startswith("job-"):
            job_id = f"job-{job_id}"
        return job_id

    # Partial hex
    if is_partial_id(job_id, prefix="job-"):
        partial = normalize_partial(job_id, prefix="job-")
        matches = _search_job_api(partial)

        return resolve_partial_id(
            ctx,
            partial,
            "job",
            matches,
            ctx.json_output,
        )

    # Not a hex string at all — give the original validation error
    format_error = _validate_job_id_format(job_id)
    msg = format_error or f"Invalid job ID format: {job_id}"
    exit_with_error(
        ctx,
        "InvalidJobID",
        msg,
        EXIT_JOB_NOT_FOUND,
        hint="Expected a full job ID (job-xxxxxxxx-...) or a partial hex prefix (4+ chars).",
    )
    return ""  # unreachable, exit_with_error calls sys.exit


# The jobs list documents no ceiling; 1000 is the only page_size limit documented for this platform
# (API_ENDPOINTS.md, cluster nodes), so a lookup page stops there instead of being refused.
_SEARCH_PAGE_SIZE = 1000


def _search_job_api(partial: str) -> list[tuple[str, str]]:
    """Search the platform's job listing for *partial*; return [] on any failure.

    A job matches on an exact name or on a prefix of the hex part of its ID, so ``resolve_partial_id``
    still decides between "nothing matched" and "several did".
    """
    try:
        from inspire.platform.web.browser_api.jobs import list_jobs as web_list_jobs

        items, _ = web_list_jobs(page_size=_SEARCH_PAGE_SIZE)
        matches: list[tuple[str, str]] = []
        for job in items:
            jid = job.job_id
            uuid_part = jid[4:] if jid.lower().startswith("job-") else jid
            label = job.name or job.status or ""
            id_match = uuid_part.lower().startswith(partial)
            name_match = bool(job.name) and job.name.lower() == partial
            if id_match or name_match:
                matches.append((jid, label))
        return matches
    except Exception as error:  # noqa: BLE001 - graceful fallback by design for resolver UX
        logger.debug("Web job lookup fallback for partial %s failed: %s", partial, error)
        return []
