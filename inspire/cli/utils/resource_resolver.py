"""Unified resource resolution for top-level shortcuts."""

from __future__ import annotations

import builtins
import difflib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, NoReturn

from inspire.cli.context import Context, EXIT_API_ERROR, EXIT_VALIDATION_ERROR
from inspire.cli.utils.id_resolver import is_full_uuid
from inspire.cli.utils.output import emit_error, emit_warning

RESOURCE_TYPES = ("job", "notebook", "serving")
RESOURCE_TYPE_ALIASES = {
    "job": "job",
    "jobs": "job",
    "notebook": "notebook",
    "notebooks": "notebook",
    "nb": "notebook",
    "serving": "serving",
    "servings": "serving",
    "service": "serving",
    "svc": "serving",
    "model": "serving",
    "models": "serving",
}
RESOURCE_CACHE_ENV = "INSPIRE_RESOURCE_CACHE"
RESOURCE_CACHE_FILENAME = "resource_cache.json"

_NOTEBOOK_PREFIX = "notebook-"
_JOB_PREFIX = "job-"
# The jobs list documents no ceiling; 1000 is the only page_size limit documented for this platform
# (API_ENDPOINTS.md, cluster nodes), so a search page stops there instead of being refused.
_JOB_SEARCH_PAGE_SIZE = 1000
_HEX_RE = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)


@dataclass(frozen=True)
class ResourceCandidate:
    """A possible resource match."""

    resource_type: str
    resource_id: str
    name: str = ""
    status: str = ""
    source: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.resource_type}:{self.resource_id}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "name": self.name,
            "status": self.status,
            "source": self.source,
            "updated_at": self.updated_at,
        }
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


def normalize_resource_type(value: str | None) -> str | None:
    if value is None:
        return None
    return RESOURCE_TYPE_ALIASES.get(value.strip().lower())


def default_resource_cache_path() -> Path:
    override = str(os.getenv(RESOURCE_CACHE_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "inspire-cli" / RESOURCE_CACHE_FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidate_from_payload(payload: dict[str, Any]) -> ResourceCandidate | None:
    resource_type = normalize_resource_type(str(payload.get("resource_type") or ""))
    resource_id = str(payload.get("resource_id") or "").strip()
    if resource_type not in RESOURCE_TYPES or not resource_id:
        return None
    metadata = payload.get("metadata")
    return ResourceCandidate(
        resource_type=resource_type,
        resource_id=resource_id,
        name=str(payload.get("name") or ""),
        status=str(payload.get("status") or ""),
        source=str(payload.get("source") or "resource-cache"),
        updated_at=str(payload.get("updated_at") or ""),
        metadata=metadata if isinstance(metadata, dict) else {},
    )


class ResourceCache:
    """Small JSON cache for cross-resource lookup."""

    def __init__(self, cache_path: str | Path | None = None) -> None:
        self.cache_path = (
            Path(cache_path).expanduser() if cache_path else default_resource_cache_path()
        )
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, ResourceCandidate]:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

        raw_resources = payload.get("resources") if isinstance(payload, dict) else None
        if isinstance(raw_resources, list):
            items = raw_resources
        elif isinstance(raw_resources, dict):
            items = list(raw_resources.values())
        else:
            items = []

        resources: dict[str, ResourceCandidate] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            candidate = _candidate_from_payload(item)
            if candidate is not None:
                resources[candidate.key] = candidate
        return resources

    def _save(self, resources: dict[str, ResourceCandidate]) -> None:
        payload = {
            "version": 1,
            "resources": {key: candidate.to_dict() for key, candidate in sorted(resources.items())},
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def list(self, resource_type: str | None = None) -> builtins.list[ResourceCandidate]:
        canonical_type = normalize_resource_type(resource_type)
        resources = list(self._load().values())
        if canonical_type:
            resources = [
                candidate for candidate in resources if candidate.resource_type == canonical_type
            ]
        resources.sort(key=lambda item: item.updated_at or "", reverse=True)
        return resources

    def upsert(self, candidate: ResourceCandidate) -> ResourceCandidate:
        resources = self._load()
        existing = resources.get(candidate.key)
        merged = ResourceCandidate(
            resource_type=candidate.resource_type,
            resource_id=candidate.resource_id,
            name=candidate.name or (existing.name if existing else ""),
            status=candidate.status or (existing.status if existing else ""),
            source=candidate.source or (existing.source if existing else "resource-cache"),
            updated_at=_now_iso(),
            metadata=candidate.metadata or (existing.metadata if existing else {}),
        )
        resources[merged.key] = merged
        self._save(resources)
        return merged

    def remove(self, resource_type: str, resource_id: str) -> bool:
        resources = self._load()
        key = f"{normalize_resource_type(resource_type)}:{resource_id}"
        if key not in resources:
            return False
        del resources[key]
        self._save(resources)
        return True

    def clear(self) -> int:
        count = len(self._load())
        self._save({})
        return count

    def stats(self) -> dict[str, Any]:
        resources = self.list()
        by_type = {resource_type: 0 for resource_type in RESOURCE_TYPES}
        for candidate in resources:
            by_type[candidate.resource_type] = by_type.get(candidate.resource_type, 0) + 1
        return {
            "path": str(self.cache_path),
            "total": len(resources),
            "by_type": by_type,
        }

    def find(
        self, reference: str, allowed_types: Iterable[str] | None = None
    ) -> builtins.list[ResourceCandidate]:
        allowed = _allowed_type_set(allowed_types)
        matches: list[ResourceCandidate] = []
        for candidate in self.list():
            if allowed is not None and candidate.resource_type not in allowed:
                continue
            if _candidate_matches(candidate, reference):
                matches.append(
                    ResourceCandidate(
                        resource_type=candidate.resource_type,
                        resource_id=candidate.resource_id,
                        name=candidate.name,
                        status=candidate.status,
                        source="resource-cache",
                        updated_at=candidate.updated_at,
                        metadata=candidate.metadata,
                    )
                )
        return matches

    def suggestions(
        self,
        reference: str,
        allowed_types: Iterable[str] | None = None,
        *,
        limit: int = 5,
    ) -> builtins.list[ResourceCandidate]:
        allowed = _allowed_type_set(allowed_types)
        candidates = [
            candidate
            for candidate in self.list()
            if allowed is None or candidate.resource_type in allowed
        ]
        labels: dict[str, ResourceCandidate] = {}
        for candidate in candidates:
            labels[candidate.resource_id] = candidate
            if candidate.name:
                labels[candidate.name] = candidate
            labels[f"{candidate.resource_type}:{candidate.resource_id}"] = candidate
            if candidate.name:
                labels[f"{candidate.resource_type}:{candidate.name}"] = candidate

        close = difflib.get_close_matches(reference, list(labels), n=limit, cutoff=0.5)
        seen: set[str] = set()
        suggestions: list[ResourceCandidate] = []
        for label in close:
            candidate = labels[label]
            if candidate.key in seen:
                continue
            seen.add(candidate.key)
            suggestions.append(candidate)
        return suggestions


def _allowed_type_set(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    allowed = {normalize_resource_type(value) for value in values}
    allowed.discard(None)
    return {value for value in allowed if value in RESOURCE_TYPES}


def _split_qualified_reference(reference: str) -> tuple[str | None, str]:
    raw = reference.strip()
    if ":" not in raw:
        return None, raw
    prefix, value = raw.split(":", 1)
    resource_type = normalize_resource_type(prefix)
    if resource_type is None:
        return None, raw
    return resource_type, value.strip()


def _display_id_tokens(candidate: ResourceCandidate) -> set[str]:
    resource_id = candidate.resource_id
    tokens = {resource_id, f"{candidate.resource_type}:{resource_id}"}
    if candidate.resource_type == "notebook" and not resource_id.lower().startswith(
        _NOTEBOOK_PREFIX
    ):
        tokens.add(f"{_NOTEBOOK_PREFIX}{resource_id}")
        tokens.add(f"notebook:{_NOTEBOOK_PREFIX}{resource_id}")
    if candidate.resource_type == "job" and resource_id.lower().startswith(_JOB_PREFIX):
        tokens.add(resource_id[len(_JOB_PREFIX) :])
    if candidate.resource_type == "serving":
        tokens.add(f"model:{resource_id}")
        tokens.add(f"serving:{resource_id}")
    return tokens


def _id_prefix_matches(candidate: ResourceCandidate, reference: str) -> bool:
    if len(reference) < 4:
        return False
    reference_lower = reference.lower()
    for token in _display_id_tokens(candidate):
        token_lower = token.lower()
        if token_lower.startswith(reference_lower):
            return True
        if ":" in token_lower:
            _prefix, value = token_lower.split(":", 1)
            if value.startswith(reference_lower):
                return True
    return False


def _candidate_matches(candidate: ResourceCandidate, reference: str) -> bool:
    reference = reference.strip()
    if not reference:
        return False
    reference_lower = reference.lower()
    for token in _display_id_tokens(candidate):
        if token.lower() == reference_lower:
            return True
    if candidate.name and candidate.name.lower() == reference_lower:
        return True
    if candidate.name and f"{candidate.resource_type}:{candidate.name}".lower() == reference_lower:
        return True
    return _id_prefix_matches(candidate, reference)


def _deterministic_id_candidates(
    reference: str,
    allowed_types: Iterable[str] | None,
    *,
    forced_type: str | None,
) -> list[ResourceCandidate]:
    allowed = _allowed_type_set(allowed_types)

    def type_allowed(resource_type: str) -> bool:
        return (allowed is None or resource_type in allowed) and (
            forced_type is None or forced_type == resource_type
        )

    raw = reference.strip()
    raw_lower = raw.lower()
    candidates: list[ResourceCandidate] = []

    if (
        type_allowed("job")
        and raw_lower.startswith(_JOB_PREFIX)
        and is_full_uuid(raw, prefix=_JOB_PREFIX)
    ):
        candidates.append(ResourceCandidate("job", raw, source="id"))
    elif forced_type == "job" and type_allowed("job") and is_full_uuid(raw, prefix=_JOB_PREFIX):
        job_id = raw if raw_lower.startswith(_JOB_PREFIX) else f"{_JOB_PREFIX}{raw}"
        candidates.append(ResourceCandidate("job", job_id, source="id"))

    if type_allowed("notebook"):
        if raw_lower.startswith(_NOTEBOOK_PREFIX) and is_full_uuid(raw, prefix=_NOTEBOOK_PREFIX):
            candidates.append(
                ResourceCandidate("notebook", raw[len(_NOTEBOOK_PREFIX) :], source="id")
            )
        elif is_full_uuid(raw) and (forced_type == "notebook" or forced_type is None):
            candidates.append(ResourceCandidate("notebook", raw, source="id"))

    if type_allowed("serving") and (
        raw_lower.startswith("sv-") or raw_lower.startswith("serving-")
    ):
        candidates.append(ResourceCandidate("serving", raw, source="id"))

    return candidates


def _search_live_jobs(reference: str) -> list[ResourceCandidate]:
    """Live job listing matches for *reference*, in the same candidate shape as every other source."""
    try:
        from inspire.platform.web.browser_api.jobs import list_jobs

        items, _total = list_jobs(page_size=_JOB_SEARCH_PAGE_SIZE)
    except Exception:
        return []

    matches: list[ResourceCandidate] = []
    for item in items:
        candidate = ResourceCandidate(
            "job",
            str(getattr(item, "job_id", "") or ""),
            name=str(getattr(item, "name", "") or ""),
            status=str(getattr(item, "status", "") or ""),
            source="live",
        )
        if candidate.resource_id and _candidate_matches(candidate, reference):
            matches.append(candidate)
    return matches


def _search_live_notebooks(ctx: Context, reference: str) -> list[ResourceCandidate]:
    try:
        from inspire.cli.commands.notebook.notebook_lookup import (
            _collect_workspace_ids_for_lookup,
            _list_notebooks_for_workspace,
            _notebook_id_from_item,
            _try_get_current_user_ids,
        )
        from inspire.cli.utils.notebook_cli import get_base_url, load_config, require_web_session

        session = require_web_session(ctx, hint="Resolving notebooks requires web authentication.")
        config = load_config(ctx)
        base_url = get_base_url()
        workspace_ids = _collect_workspace_ids_for_lookup(session, config)
        user_ids = _try_get_current_user_ids(session, base_url=base_url)
    except Exception:
        return []

    matches: list[ResourceCandidate] = []
    seen: set[str] = set()
    for workspace_id in workspace_ids:
        try:
            items = _list_notebooks_for_workspace(
                session,
                base_url=base_url,
                workspace_id=workspace_id,
                user_ids=user_ids,
                keyword=reference,
                page_size=100,
            )
        except Exception:
            continue
        for item in items:
            notebook_id = _notebook_id_from_item(item)
            if not notebook_id or notebook_id in seen:
                continue
            seen.add(notebook_id)
            candidate = ResourceCandidate(
                "notebook",
                (
                    notebook_id[len(_NOTEBOOK_PREFIX) :]
                    if notebook_id.startswith(_NOTEBOOK_PREFIX)
                    else notebook_id
                ),
                name=str(item.get("name") or ""),
                status=str(item.get("status") or ""),
                source="live",
                metadata={"workspace_id": str(item.get("workspace_id") or workspace_id)},
            )
            if _candidate_matches(candidate, reference):
                matches.append(candidate)
    return matches


def _search_live_servings(ctx: Context, reference: str) -> list[ResourceCandidate]:
    try:
        from inspire.cli.utils.notebook_cli import require_web_session
        from inspire.platform.web.browser_api.inference_servings import list_inference_servings

        session = require_web_session(
            ctx, hint="Resolving model servings requires web authentication."
        )
        servings = list_inference_servings(session=session, limit=100)
    except Exception:
        return []

    matches: list[ResourceCandidate] = []
    for serving in servings:
        candidate = ResourceCandidate(
            "serving",
            str(getattr(serving, "serving_id", "") or ""),
            name=str(getattr(serving, "name", "") or ""),
            status=str(getattr(serving, "status", "") or ""),
            source="live",
            metadata={
                "workspace_id": str(getattr(serving, "workspace_id", "") or ""),
                "project_id": str(getattr(serving, "project_id", "") or ""),
            },
        )
        if candidate.resource_id and _candidate_matches(candidate, reference):
            matches.append(candidate)
    return matches


def _search_sources(
    ctx: Context, reference: str, allowed_types: Iterable[str] | None
) -> list[ResourceCandidate]:
    allowed = _allowed_type_set(allowed_types) or set(RESOURCE_TYPES)
    candidates: list[ResourceCandidate] = []
    if "job" in allowed:
        candidates.extend(_search_live_jobs(reference))
    if "notebook" in allowed:
        candidates.extend(_search_live_notebooks(ctx, reference))
    if "serving" in allowed:
        candidates.extend(_search_live_servings(ctx, reference))
    return candidates


def _dedupe_candidates(candidates: Iterable[ResourceCandidate]) -> list[ResourceCandidate]:
    by_key: dict[str, ResourceCandidate] = {}
    source_rank = {"id": 0, "live": 1, "resource-cache": 2}
    for candidate in candidates:
        if normalize_resource_type(candidate.resource_type) not in RESOURCE_TYPES:
            continue
        if not candidate.resource_id:
            continue
        existing = by_key.get(candidate.key)
        if existing is None:
            by_key[candidate.key] = candidate
            continue
        existing_rank = source_rank.get(existing.source, 99)
        candidate_rank = source_rank.get(candidate.source, 99)
        if candidate_rank < existing_rank:
            by_key[candidate.key] = ResourceCandidate(
                candidate.resource_type,
                candidate.resource_id,
                name=candidate.name or existing.name,
                status=candidate.status or existing.status,
                source=candidate.source,
                updated_at=candidate.updated_at or existing.updated_at,
                metadata=candidate.metadata or existing.metadata,
            )
        elif not existing.name or not existing.status:
            by_key[candidate.key] = ResourceCandidate(
                existing.resource_type,
                existing.resource_id,
                name=existing.name or candidate.name,
                status=existing.status or candidate.status,
                source=existing.source,
                updated_at=existing.updated_at,
                metadata=existing.metadata or candidate.metadata,
            )
    return list(by_key.values())


def _verify_job(candidate: ResourceCandidate) -> ResourceCandidate | None:
    try:
        from inspire.platform.web import browser_api as browser_api_module

        payload = browser_api_module.get_job_detail(candidate.resource_id)
    except Exception:
        return None
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    return ResourceCandidate(
        "job",
        candidate.resource_id,
        name=str(data.get("name") or candidate.name),
        status=str(data.get("status") or candidate.status),
        source="live",
        metadata=candidate.metadata,
    )


def _verify_notebook(ctx: Context, candidate: ResourceCandidate) -> ResourceCandidate | None:
    try:
        from inspire.cli.utils.notebook_cli import require_web_session
        from inspire.platform.web import browser_api as browser_api_module

        session = require_web_session(ctx, hint="Verifying notebooks requires web authentication.")
        data = browser_api_module.get_notebook_detail(candidate.resource_id, session=session)
    except Exception:
        return None
    detail = data if isinstance(data, dict) else {}
    return ResourceCandidate(
        "notebook",
        candidate.resource_id,
        name=str(detail.get("name") or candidate.name),
        status=str(detail.get("status") or candidate.status),
        source="live",
        metadata=candidate.metadata,
    )


def _verify_serving(ctx: Context, candidate: ResourceCandidate) -> ResourceCandidate | None:
    try:
        from inspire.cli.utils.notebook_cli import require_web_session
        from inspire.platform.web.browser_api.inference_servings import get_inference_serving_detail

        session = require_web_session(
            ctx, hint="Verifying model servings requires web authentication."
        )
        serving = get_inference_serving_detail(candidate.resource_id, session=session)
    except Exception:
        return None
    return ResourceCandidate(
        "serving",
        candidate.resource_id,
        name=str(getattr(serving, "name", "") or candidate.name),
        status=str(getattr(serving, "status", "") or candidate.status),
        source="live",
        metadata=candidate.metadata,
    )


def verify_resource(ctx: Context, candidate: ResourceCandidate) -> ResourceCandidate | None:
    if candidate.resource_type == "job":
        return _verify_job(candidate)
    if candidate.resource_type == "notebook":
        return _verify_notebook(ctx, candidate)
    if candidate.resource_type == "serving":
        return _verify_serving(ctx, candidate)
    return None


def _format_candidate(candidate: ResourceCandidate) -> str:
    label_parts = [candidate.resource_type, candidate.resource_id]
    if candidate.name:
        label_parts.append(candidate.name)
    if candidate.status:
        label_parts.append(candidate.status)
    return "  - " + "  ".join(label_parts)


def _fail_ambiguous(ctx: Context, reference: str, candidates: list[ResourceCandidate]) -> NoReturn:
    lines = [_format_candidate(candidate) for candidate in candidates]
    message = f"Resource reference '{reference}' is ambiguous."
    if ctx.json_output:
        message += " Candidates: " + "; ".join(
            f"{candidate.resource_type}:{candidate.resource_id}" for candidate in candidates
        )
    emit_error(
        ctx,
        error_type="AmbiguousResource",
        message=message,
        exit_code=EXIT_VALIDATION_ERROR,
        hint="Use a type prefix such as job:<ref>, notebook:<ref>, or serving:<ref>.",
        human_lines=lines,
    )
    sys.exit(EXIT_VALIDATION_ERROR)


def _fail_not_found(
    ctx: Context,
    reference: str,
    *,
    cache: ResourceCache,
    allowed_types: Iterable[str] | None,
    stale: ResourceCandidate | None = None,
) -> NoReturn:
    suggestions = cache.suggestions(reference, allowed_types)
    hint = "Run 'inspire resolve <ref>' after listing resources, or use a type prefix."
    if suggestions:
        hint = (
            "Did you mean "
            + ", ".join(
                f"{candidate.resource_type}:{candidate.name or candidate.resource_id}"
                for candidate in suggestions
            )
            + "?"
        )
    if stale is not None:
        message = (
            f"Cached {stale.resource_type} '{reference}' resolved to {stale.resource_id}, "
            "but live verification failed and the stale entry was evicted."
        )
    else:
        message = f"No resource matching '{reference}'."
    emit_error(
        ctx,
        error_type="ResourceNotFound",
        message=message,
        exit_code=EXIT_VALIDATION_ERROR,
        hint=hint,
    )
    sys.exit(EXIT_VALIDATION_ERROR)


def resolve_resource(
    ctx: Context,
    reference: str,
    *,
    allowed_types: Iterable[str] | None = None,
    verify: bool = False,
    cache: ResourceCache | None = None,
) -> ResourceCandidate:
    """Resolve a resource reference to a single candidate or exit with an error."""

    cache = cache or ResourceCache()
    forced_type, raw_reference = _split_qualified_reference(reference)
    requested_allowed = _allowed_type_set(allowed_types)
    if forced_type is not None:
        if requested_allowed is not None and forced_type not in requested_allowed:
            emit_error(
                ctx,
                error_type="ValidationError",
                message=f"Resource type '{forced_type}' is not valid for this command.",
                exit_code=EXIT_VALIDATION_ERROR,
            )
            sys.exit(EXIT_VALIDATION_ERROR)
        allowed_types = [forced_type]

    if not raw_reference:
        emit_error(
            ctx,
            error_type="ValidationError",
            message="Resource reference cannot be empty.",
            exit_code=EXIT_VALIDATION_ERROR,
        )
        sys.exit(EXIT_VALIDATION_ERROR)

    deterministic = _deterministic_id_candidates(
        raw_reference,
        allowed_types,
        forced_type=forced_type,
    )
    candidates = deterministic
    if not deterministic:
        candidates = _dedupe_candidates(cache.find(raw_reference, allowed_types))
        if not candidates:
            if cache.suggestions(raw_reference, allowed_types):
                _fail_not_found(ctx, raw_reference, cache=cache, allowed_types=allowed_types)
            candidates = _search_sources(ctx, raw_reference, allowed_types)
    candidates = _dedupe_candidates(candidates)

    if not candidates:
        _fail_not_found(ctx, raw_reference, cache=cache, allowed_types=allowed_types)

    if len(candidates) > 1:
        _fail_ambiguous(ctx, raw_reference, candidates)

    candidate = candidates[0]
    if verify and candidate.source == "resource-cache":
        verified = verify_resource(ctx, candidate)
        if verified is None:
            cache.remove(candidate.resource_type, candidate.resource_id)
            _fail_not_found(
                ctx, raw_reference, cache=cache, allowed_types=allowed_types, stale=candidate
            )
        candidate = verified

    if verify and candidate.source == "id":
        verified = verify_resource(ctx, candidate)
        if verified is None:
            emit_error(
                ctx,
                error_type="ResourceNotFound",
                message=(
                    f"Live verification failed for {candidate.resource_type} "
                    f"{candidate.resource_id}."
                ),
                exit_code=EXIT_API_ERROR,
            )
            sys.exit(EXIT_API_ERROR)
        candidate = verified

    try:
        candidate = cache.upsert(candidate)
    except OSError as exc:
        emit_warning(ctx, f"Failed to update resource cache: {exc}")

    return candidate


def resource_to_json(candidate: ResourceCandidate) -> dict[str, Any]:
    return candidate.to_dict()
