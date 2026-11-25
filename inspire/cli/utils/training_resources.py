"""Resolve training job resource requests into current platform price payloads."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Optional

from inspire.config import Config
from inspire.platform.openapi.resources import parse_resource_request
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api.priority import (
    allowed_priority_levels_for_offer,
    priority_matches_allowed,
)
from inspire.platform.web import session as web_session_module
from inspire.platform.web.session import WebSession


@dataclass(frozen=True)
class TrainingResourceSelection:
    logic_compute_group_id: str
    logic_compute_group_name: str
    resource_spec_price: dict[str, Any]
    quota_id: str
    cpu_count: int
    gpu_count: int
    memory_size_gib: int


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _matches_gpu_type(requested: str, candidate: str) -> bool:
    requested_upper = (requested or "").upper()
    candidate_upper = (candidate or "").upper()
    return bool(requested_upper and candidate_upper) and (
        requested_upper in candidate_upper or candidate_upper in requested_upper
    )


def _group_config_candidates(config: Config, group_id: str) -> list[str]:
    candidates: list[str] = []
    for item in config.compute_groups or []:
        configured_id = str(
            item.get("logic_compute_group_id")
            or item.get("compute_group_id")
            or item.get("id")
            or ""
        ).strip()
        if configured_id != group_id:
            continue
        for key in ("name", "location"):
            value = str(item.get(key) or "").strip()
            if value:
                candidates.append(value)
    return candidates


def _group_matches_location(config: Config, group: dict[str, Any], location: str) -> bool:
    needle = (location or "").strip().lower()
    if not needle:
        return True

    group_id = str(group.get("logic_compute_group_id") or "").strip()
    candidates = [group_id]
    for key in ("name", "compute_group_name", "location"):
        value = str(group.get(key) or "").strip()
        if value:
            candidates.append(value)
    candidates.extend(_group_config_candidates(config, group_id))

    return any(needle in candidate.lower() for candidate in candidates if candidate)


def _group_matches_exact(config: Config, group: dict[str, Any], location: str) -> bool:
    needle = (location or "").strip().lower()
    if not needle:
        return True
    group_id = str(group.get("logic_compute_group_id") or "").strip()
    candidates = [group_id, str(group.get("name") or "").strip()]
    candidates.extend(_group_config_candidates(config, group_id))
    return any(needle == candidate.lower() for candidate in candidates if candidate)


def _gpu_model(label: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", " ", label.upper()).strip()
    for pattern in (r"\b[HABLV]\d{2,3}[A-Z]?\b", r"\b\d{4}\b", r"\b\d{3}B\b"):
        match = re.search(pattern, normalized)
        if match:
            return match.group(0)
    return normalized.replace(" ", "")


def _price_gpu_type(price: dict[str, Any]) -> str:
    gpu_info = price.get("gpu_info") or {}
    if not isinstance(gpu_info, dict):
        gpu_info = {}
    labels = [
        str(value).strip()
        for value in (
            price.get("gpu_type"),
            gpu_info.get("gpu_type"),
            gpu_info.get("gpu_type_display"),
            gpu_info.get("gpu_product_simple"),
        )
        if value
    ]
    if not labels:
        return ""
    for label in labels[1:]:
        if _gpu_model(labels[0]) != _gpu_model(label):
            quota_id = str(price.get("quota_id") or "<unknown>")
            raise ValueError(
                f"Conflicting GPU types in Scheduling Offer {quota_id}: {', '.join(labels)}"
            )
    return labels[0]


_QUOTA_KEYS = ("GPU", "CPU", "MEM")


def _strip_quota_label(field: str, *, key: str) -> str:
    """Drop a `KEY=` prefix from one quota field, as in the `GPU=` of `GPU=4`.

    The ambiguity error prints candidates with their fields labelled, so the parser accepts the same
    spelling. Only the label belonging to this position is accepted: a reordered or unknown label is a
    different selector, and silently reading it as this one would pick the wrong offer.
    """
    label, sep, rest = field.partition("=")
    if not sep:
        return field
    if label.strip().upper() != key:
        raise ValueError("Quota values must be integers: GPU,CPU,MEM")
    return rest.strip()


def parse_quota_spec(value: str) -> tuple[int, int, int]:
    """Parse an exact GPU,CPU,MEM scheduling-offer selector.

    The bare `4,80,900` triple is canonical. A field may also carry its own `GPU=`/`CPU=`/`MEM=` label
    in either case, which is how `_describe_offer` prints a candidate, so quoting one back works.
    """
    parts = [part.strip() for part in str(value or "").split(",")]
    if len(parts) != 3:
        raise ValueError("Quota must be GPU,CPU,MEM, for example 4,80,900")
    fields = [
        _strip_quota_label(part, key=key) for part, key in zip(parts, _QUOTA_KEYS, strict=True)
    ]
    try:
        gpu, cpu, memory = (int(part) for part in fields)
    except ValueError as exc:
        raise ValueError("Quota values must be integers: GPU,CPU,MEM") from exc
    if gpu < 0 or cpu < 1 or memory < 1:
        raise ValueError("Quota requires GPU >= 0, CPU >= 1, and MEM >= 1")
    return gpu, cpu, memory


def group_supports_training(group: dict[str, Any]) -> bool:
    """Return whether a live group declares distributed-training support."""
    raw = group.get("support_job_type_list")
    if raw is None:
        return True  # Older platform payloads did not expose this declaration.
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return False
    if not isinstance(raw, list):
        return False
    supported = {str(value or "").strip().lower() for value in raw}
    return "distributed_training" in supported


def _matching_train_prices(
    *,
    prices: list[dict[str, Any]],
    requested_gpu_type: str,
    requested_gpu_count: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for price in prices:
        price_gpu_count = _safe_int(price.get("gpu_count"), 0)
        if price_gpu_count != requested_gpu_count:
            continue
        if not _matches_gpu_type(requested_gpu_type, _price_gpu_type(price)):
            continue
        candidates.append(price)
    return candidates


def _describe_offer(group: dict[str, Any], price: dict[str, Any]) -> str:
    group_id = str(group.get("logic_compute_group_id") or "").strip()
    group_name = str(group.get("name") or group_id).strip()
    return (
        f"{group_name} ({group_id}), quota_id={price.get('quota_id') or '<none>'}, "
        f"GPU={_safe_int(price.get('gpu_count'))}, CPU={_safe_int(price.get('cpu_count'))}, "
        f"MEM={_safe_int(price.get('memory_size_gib'))}"
    )


def _offer_selection_flags(group: dict[str, Any], price: dict[str, Any]) -> str:
    """The flags that select this offer, spelled so they can be pasted straight back."""
    group_id = str(group.get("logic_compute_group_id") or "").strip()
    quota = (
        f"{_safe_int(price.get('gpu_count'))},{_safe_int(price.get('cpu_count'))},"
        f"{_safe_int(price.get('memory_size_gib'))}"
    )
    return f"--group '{group_id}' --quota {quota}"


def _training_resource_selection_from_price(
    *,
    group_id: str,
    group_name: str,
    price: dict[str, Any],
) -> TrainingResourceSelection:
    """Build a selection from one already-resolved group/quota price row."""
    # Build the spec with only the fields the CreateJob proto accepts.
    # The raw price row carries extra keys (cpu_info, gpu_info, price ids,
    # ...) that the platform now rejects with "unknown field" errors.
    cpu_info = price.get("cpu_info")
    if not isinstance(cpu_info, dict):
        cpu_info = {}
    gpu_info = price.get("gpu_info")
    if not isinstance(gpu_info, dict):
        gpu_info = {}
    resource_spec_price = {
        "cpu_type": str(cpu_info.get("cpu_type") or ""),
        "cpu_count": _safe_int(price.get("cpu_count"), 1),
        "gpu_type": _price_gpu_type(price),
        "gpu_count": _safe_int(price.get("gpu_count"), 0),
        "memory_size_gib": _safe_int(price.get("memory_size_gib"), 1),
        "logic_compute_group_id": group_id,
        "quota_id": str(price.get("quota_id") or ""),
    }
    return TrainingResourceSelection(
        logic_compute_group_id=group_id,
        logic_compute_group_name=group_name,
        resource_spec_price=resource_spec_price,
        quota_id=str(resource_spec_price.get("quota_id") or ""),
        cpu_count=_safe_int(resource_spec_price.get("cpu_count"), 1),
        gpu_count=_safe_int(resource_spec_price.get("gpu_count"), 0),
        memory_size_gib=_safe_int(resource_spec_price.get("memory_size_gib"), 1),
    )


def training_resource_selection_from_offer(offer: object) -> TrainingResourceSelection:
    """Reuse the exact offer auto-selection picked, without re-querying prices."""
    group_id = str(getattr(offer, "logic_compute_group_id", "") or "").strip()
    group_name = str(getattr(offer, "logic_compute_group_name", "") or group_id)
    raw = getattr(offer, "raw", None)
    price = dict(raw) if isinstance(raw, dict) else {}
    price.setdefault("quota_id", str(getattr(offer, "quota_id", "") or ""))
    price.setdefault("cpu_count", _safe_int(getattr(offer, "cpu_count", 0), 1))
    price.setdefault("gpu_count", _safe_int(getattr(offer, "gpu_count", 0), 0))
    price.setdefault("memory_size_gib", _safe_int(getattr(offer, "memory_size_gib", 0), 1))
    return _training_resource_selection_from_price(
        group_id=group_id,
        group_name=group_name,
        price=price,
    )


def resolve_training_resource_selection(
    *,
    config: Config,
    workspace_id: str,
    resource: str,
    prefer_location: Optional[str],
    exact_quota: str | None = None,
    exact_group: bool = False,
    auto_select_offer: bool = False,
    priority: int | None = None,
    requested_priority: int | None = None,
    session: Optional[WebSession] = None,
) -> TrainingResourceSelection:
    """Resolve a train resource request into the current platform price shape.

    `priority` is the value used for matching; `requested_priority` is what the user asked for when the
    two differ - the project's priority ceiling can lower it - and it only changes how a failed match is
    explained. Without it, a user who asked for 6 and was capped to 4 is told no spec exists
    "compatible with priority 4", which is true of our clamped value and useless about their request.
    """
    if session is None:
        session = web_session_module.get_web_session()

    requested_gpu_type, requested_gpu_count = parse_resource_request(resource)
    schedule: dict[str, Any] = {}
    if priority is not None:
        try:
            schedule = browser_api_module.get_notebook_schedule(
                workspace_id=workspace_id,
                session=session,
            )
        except Exception:
            # Keep compatibility with deployments that expose offers but not
            # the optional schedule metadata endpoint.
            schedule = {}
    groups = browser_api_module.list_compute_groups(workspace_id=workspace_id, session=session)
    if not groups:
        raise ValueError(f"No compute groups found for workspace {workspace_id}")

    groups = [group for group in groups if group_supports_training(group)]

    if prefer_location:
        groups = [
            group
            for group in groups
            if (
                _group_matches_exact(config, group, prefer_location)
                if exact_group
                else _group_matches_location(config, group, prefer_location)
            )
        ]
        if not groups:
            raise ValueError(
                f"Location '{prefer_location}' not found for {requested_gpu_type.value}"
            )

    quota_tuple = parse_quota_spec(exact_quota) if exact_quota else None
    errors: list[str] = []
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    # Spec rows that matched the GPU but were rejected purely because of the priority band, kept only to
    # explain the failure: "nothing matches" and "these exist but not at your priority" need different
    # fixes (change the pool vs. change --priority).
    priority_blocked: list[str] = []
    for group in groups:
        group_id = str(group.get("logic_compute_group_id") or "").strip()
        if not group_id:
            continue
        try:
            prices = browser_api_module.get_train_resource_prices(
                workspace_id=workspace_id,
                logic_compute_group_id=group_id,
                session=session,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{group_id}: {exc}")
            continue

        candidates = _matching_train_prices(
            prices=prices,
            requested_gpu_type=requested_gpu_type.value,
            requested_gpu_count=requested_gpu_count,
        )
        if quota_tuple is not None:
            quota_gpu, quota_cpu, quota_memory = quota_tuple
            candidates = [
                item
                for item in candidates
                if _safe_int(item.get("gpu_count"), 0) == quota_gpu
                and _safe_int(item.get("cpu_count"), 0) == quota_cpu
                and _safe_int(item.get("memory_size_gib"), 0) == quota_memory
            ]
        if priority is not None:
            kept: list[dict[str, Any]] = []
            for item in candidates:
                levels = allowed_priority_levels_for_offer(
                    schedule,
                    group_id=group_id,
                    quota_id=str(item.get("quota_id") or ""),
                )
                if priority_matches_allowed(priority, levels):
                    kept.append(item)
                else:
                    priority_blocked.extend(sorted(levels))
            candidates = kept
        matches.extend((group, price) for price in candidates)

    if len(matches) > 1 and auto_select_offer:
        group_ids = [
            str(group.get("logic_compute_group_id") or "").strip() for group, _price in matches
        ]
        # Auto mode may choose between equivalent per-group offers. Multiple
        # matching quota rows inside one group remain ambiguous because they
        # can differ materially in CPU, memory, and price.
        if len(set(group_ids)) == len(matches):
            matches.sort(
                key=lambda item: (
                    str(item[0].get("name") or "").casefold(),
                    str(item[0].get("logic_compute_group_id") or ""),
                )
            )
            matches = matches[:1]

    if len(matches) > 1:
        details = "; ".join(
            f"{_describe_offer(group, price)} → {_offer_selection_flags(group, price)}"
            for group, price in matches
        )
        raise ValueError(
            "Ambiguous train Scheduling Offer; pass an exact --group and --quota. "
            f"Candidates: {details}"
        )

    if matches:
        group, price = matches[0]
        group_id = str(group.get("logic_compute_group_id") or "").strip()
        return _training_resource_selection_from_price(
            group_id=group_id,
            group_name=str(group.get("name") or group_id),
            price=price,
        )

    suffix = f" Resource price errors: {'; '.join(errors)}" if errors else ""
    exact_hint = f" with exact quota {exact_quota}" if exact_quota else ""
    blocked_hint = ""
    if priority_blocked:
        levels_text = ", ".join(sorted(set(priority_blocked)))
        blocked_hint = (
            f" {len(priority_blocked)} matching spec(s) do exist in this workspace but accept only "
            f"priority level(s): {levels_text}. Run `inspire job offers` to see which offers accept "
            f"which levels, or lower --priority."
        )
    priority_hint = ""
    if priority is not None:
        priority_hint = f" compatible with priority {priority}"
        asked = requested_priority if requested_priority is not None else priority
        if asked != priority:
            priority_hint += (
                f" - note that you asked for priority {asked}, and the selected offer's priority "
                f"ceiling replaced it with {priority} before matching"
            )
    raise ValueError(
        f"No train resource spec found for {requested_gpu_count}x{requested_gpu_type.value}"
        f"{exact_hint}{priority_hint}{blocked_hint}"
        f" in workspace {workspace_id}.{suffix}"
    )


__all__ = [
    "TrainingResourceSelection",
    "group_supports_training",
    "parse_quota_spec",
    "resolve_training_resource_selection",
    "training_resource_selection_from_offer",
]
