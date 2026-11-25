"""The one owner of "can this compute group run a notebook".

Two platform facts used to be conflated in ``notebook create``. A group can have
live DSW quota rows and still be closed to interactive instances, because the
platform gates the workload on the group's ``support_job_type_list``. Its own offer
data separates them - ``offers`` are capability intersected with quota rows, and
``quota_rows_without_capability`` is the residue that looks launchable and is not -
so that data, and nothing derived from specs or free nodes, decides this question.

Everything that places a notebook asks here: ``--auto``, ``--no-auto`` and the
``--dry-run`` preview read the same object built once, which is what keeps a
preview from resolving a group the create then refuses (#113).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api.workload_contracts import (
    WorkloadOffer,
    WorkloadOfferDiscovery,
)
from inspire.platform.web.session import WebSession

OFFERS_COMMAND = "inspire notebook offers"


@dataclass(frozen=True)
class NotebookGroupCapability:
    """What the platform says about one workspace's groups, held once per command."""

    workspace_id: str
    discovery: Optional[WorkloadOfferDiscovery]
    error: Optional[str] = None

    @property
    def known(self) -> bool:
        """False only when the offer data could not be read at all.

        A read that succeeded and found nothing capable is a real answer, so the
        caller must not fall back to guessing from specs: that is the path #113
        came down.
        """
        return self.discovery is not None

    @property
    def offers(self) -> list[WorkloadOffer]:
        return list(self.discovery.offers) if self.discovery else []

    @property
    def groups_with_quota_but_no_capability(self) -> list[dict[str, Any]]:
        if not self.discovery:
            return []
        return list(self.discovery.quota_rows_without_capability)

    def runnable_group_ids(self) -> Optional[set[str]]:
        """Group ids that may host the workload, or ``None`` when unknown.

        ``None`` is the only outcome that lets a caller fall back to its own
        ranking; an empty set is a platform "no" and must be honoured.
        """
        if not self.discovery:
            return None
        return {offer.logic_compute_group_id for offer in self.discovery.offers}

    def offers_for_size(self, requested_cpu_count: Optional[int]) -> list[WorkloadOffer]:
        """CPU offers that answer the requested size, across every capable group."""
        cpu_offers = [offer for offer in self.offers if offer.gpu_count == 0]
        if requested_cpu_count is None:
            return cpu_offers
        return [offer for offer in cpu_offers if offer.cpu_count == requested_cpu_count]

    def offered_cpu_sizes(self) -> list[int]:
        return sorted({offer.cpu_count for offer in self.offers if offer.gpu_count == 0})

    def quota_id_for_size(self, group_id: str, requested_cpu_count: Optional[int]) -> str:
        """The quota id the platform pairs with this group at this size.

        The offer row is the only place group and quota are bound together, so a
        quota taken from anywhere else can belong to a different group and be
        rejected by the spec lookup that follows.
        """
        for offer in self.offers:
            if offer.logic_compute_group_id != group_id:
                continue
            if requested_cpu_count is not None and offer.cpu_count != requested_cpu_count:
                continue
            if offer.quota_id:
                return offer.quota_id
        return ""

    def resource_price_rows(self, group_id: str) -> list[dict[str, Any]]:
        """The group's own DSW quota rows, as the platform returned them."""
        return [
            dict(offer.raw) for offer in self.offers if offer.logic_compute_group_id == group_id
        ]

    def groups_without_quota_rows(self) -> list[dict[str, str]]:
        if not self.discovery:
            return []
        return list(self.discovery.capability_groups_without_quota_rows)

    def warnings(self, *, resolved_group_id: str = "") -> list[str]:
        """The placement facts a resolved group does not by itself convey.

        The preview must not look healthy because the group it picked is fine: a
        quota-bearing group elsewhere in the workspace is a placement fact for
        whoever reads the preview, and it is exactly what the create would have
        tripped over a moment ago (#113).
        """
        warnings: list[str] = []
        if not self.known:
            warnings.append(
                f"Compute-group capability could not be verified ({self.error or 'no offer data'}); "
                f"{OFFERS_COMMAND} reports which groups can run a notebook."
            )
            return warnings

        for row in self.groups_with_quota_but_no_capability:
            group_id = str(row.get("logic_compute_group_id") or "")
            name = str(row.get("logic_compute_group_name") or group_id)
            row_count = row.get("row_count")
            counted = f"{row_count} quota rows" if row_count else "quota rows"
            warnings.append(
                f"{name} ({group_id}) has {counted} but cannot run an interactive notebook; "
                f"it was excluded. {OFFERS_COMMAND} --workspace-id {self.workspace_id} lists "
                "the groups that can."
            )

        if resolved_group_id and resolved_group_id not in (self.runnable_group_ids() or set()):
            warnings.append(
                f"The resolved group {resolved_group_id} is not among the workload-capable "
                f"offers; {OFFERS_COMMAND} --workspace-id {self.workspace_id} lists the ones that are."
            )

        return warnings


def fetch_notebook_group_capability(
    *,
    workspace_id: str,
    session: WebSession,
) -> NotebookGroupCapability:
    """Read the platform's own verdict on which groups can run a notebook.

    A failure is recorded, not raised: an older deployment without the offer
    surface should still be able to create a notebook, and the preview then says
    the capability is unverified rather than staying silent.
    """
    try:
        discovery = browser_api_module.list_notebook_offers(
            workspace_id=workspace_id, session=session
        )
    except Exception as exc:  # noqa: BLE001 - the CLI reports it, the platform does not
        return NotebookGroupCapability(workspace_id=workspace_id, discovery=None, error=str(exc))
    return NotebookGroupCapability(workspace_id=workspace_id, discovery=discovery)


__all__ = [
    "OFFERS_COMMAND",
    "NotebookGroupCapability",
    "fetch_notebook_group_capability",
]
