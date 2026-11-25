"""Internal console-source scanner used by the platform drift snapshot.

:func:`discover_source` fetches the console entry HTML fresh, follows its JavaScript references in
a bounded breadth-first walk, and reads ``/api/v2/<service>?Action=<Action>`` references together
with nearby request-construction evidence. It is keyed to no particular Action, so console-side API
additions need no scanner change.

This is not an agent CLI. Agents use ``inspire api`` for known requests and ``inspire api eval`` for
page-driven exploration. The snapshot invokes this module in a subprocess to retain per-Action
timeouts. Cookies stay inside the HTTP transport, and emitted strings are redacted by key name and
credential-shaped text.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit

from inspire.platform.web.session import WebSession, build_requests_session, get_web_session

from .core import _get_base_url, _get_browser_api_prefix, _origin

# --- Source-mode budgets ---------------------------------------------------------------
#
# Measured on the live console: the entry script plus everything it references at depth <= 2 is
# ~241 assets / 11.4 MB, so the defaults are what one full sweep costs and the ceilings are the
# point past which a caller is asking for more than the current build contains. The report always
# says which limit stopped the walk.
DEFAULT_MAX_ASSETS = 256
DEFAULT_MAX_MATCHES = 8
DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
MAX_ROOT_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 8 * 1024 * 1024
EXCERPT_RADIUS = 220
MAX_EXCERPT_CHARS = 360
MAX_HITS_PER_ASSET = 3
SOURCE_TIMEOUT_SECONDS = 30

# Reasons that mean "the asset walk stopped early" as opposed to "the evidence list was trimmed".
# The distinction is what tells a caller whether a missing Action might still be out there.
SWEEP_REASONS = frozenset({"max_assets", "max_bytes", "max_depth"})

# The snapshot may probe these query verbs with an empty body as a second oracle.
READ_ONLY_ACTION_PREFIXES: tuple[str, ...] = (
    "List",
    "Get",
    "Search",
    "Describe",
    "Check",
    "Validate",
)

JS_MEDIA_TYPES = frozenset(
    {
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "text/ecmascript",
        "application/ecmascript",
        "application/node",
    }
)

_SCRIPT_SRC_RE = re.compile(
    r"""<script\b[^>]*?\bsrc\s*=\s*(?P<q>["']?)(?P<src>[^"'>\s]+)(?P=q)""", re.I
)
# Quoted module references: ``"./chunk.js"``, ``"/assets/x.js"``, ``"assets/y.js"``.
_MODULE_REF_RE = re.compile(r"""["'`](?P<ref>(?:\.\.?/|/)?[\w@./[\]-]+\.m?js(?:\?[^"'`]*)?)["'`]""")
# Either a spelled-out ``/api/v2`` prefix or the template placeholder that stands for it, as the
# console's generated endpoint tables spell it:
# ``wa="/api/v2"; xa={GET_TRAIN_JOB_EVENT_LIST:`${wa}/train?Action=ListJobEvents`}``.
_ACTION_RE = re.compile(
    r"""(?P<prefix>/api/v(?P<version>\d+)|\$\{(?P<var>[A-Za-z_$][A-Za-z0-9_$]*)\})"""
    r"""(?P<service>[^"'`\\?\s]*?)\?Action=(?P<action>[A-Za-z][A-Za-z0-9_.-]{2,})"""
)
_PREFIX_CONSTANT_RE = re.compile(
    r"""(?P<var>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*["'`](?P<prefix>/api/v\d+(?:/[\w./-]+)?)["'`]"""
)
# The table key an endpoint is reached through, which is the token the call site actually uses.
# The tail may cross a quote or a backtick but never a newline, and stays lazy at 120 characters:
# minified bundles are one line, so line-plus-length is the only bound that keeps this from
# matching a key at the head of the file against an Action at the other end of it.
_ACTION_KEY_RE = re.compile(
    r"""(?<![\w$])["'`]?(?P<key>[A-Z][A-Z0-9_]{3,})["'`]?\s*:\s*(?P<tail>[^\n]{0,120}?)"""
    r"""\?Action=(?P<action>[A-Za-z][A-Za-z0-9_.-]{2,})"""
)

# Redaction lives here rather than in the CLI layer because this module is also the library
# entry point and must not emit a secret on its own account. The patterns mirror
# ``inspire.cli.logging_setup.redact_text`` deliberately: two redactors that disagree is worse
# than one that is duplicated.
_SENSITIVE_TEXT_RES = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer|token)\s+[^\s,]+"),
    re.compile(
        r"""(?ix)
        (
            [\"']?(?:password|passwd|token|access[_-]?token|refresh[_-]?token|
                   secret|api[_-]?key|apikey|authorization|cookie|set-cookie)[\"']?
            \s*[:=]\s*
        )
        (\"[^\"]*\" | '[^']*' | [^\s,}\]]+)
        """
    ),
    re.compile(r"(?i)([?&](?:token|access_token|refresh_token|api_key|signature)=)([^&\s]+)"),
)
_SENSITIVE_KEYS = frozenset({"authorization", "cookie", "set-cookie"})
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "auth",
    "credential",
)
_REDACTED = "<redacted>"

__all__ = [
    "DEFAULT_MAX_ASSETS",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_MATCHES",
    "READ_ONLY_ACTION_PREFIXES",
    "discover_source",
]


# --------------------------------------------------------------------------------------
# Source scanning
# --------------------------------------------------------------------------------------


def discover_source(
    query: str,
    *,
    max_assets: int = DEFAULT_MAX_ASSETS,
    max_matches: int = DEFAULT_MAX_MATCHES,
    session: Optional[WebSession] = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Read the live console's JavaScript and return the endpoint references for ``query``.

    Args:
        query: An Action name, an endpoint fragment or any source term; matched
            case-insensitively against the Action, its service segment and the full path.
        max_assets: How many JavaScript assets may be downloaded and scanned.
        max_matches: How many actions and how many evidence excerpts are returned.
        session: An authenticated web session; a fresh one is taken when omitted.
        max_depth: How deep the reference walk goes below the entry script (0 = entry only).
        max_bytes: Total downloaded JavaScript the walk may hold.

    Returns:
        A report with ``query``, ``provenance``, ``actions``, ``evidence`` and ``budget``. Every
        limit the walk hit is reported, so "not in the scanned slice" stays distinguishable from
        "the console has no such Action".
    """
    needle = str(query or "").strip().lower()
    if not needle:
        raise ValueError("Discovery query must not be empty")
    if max_assets < 1 or max_matches < 1 or max_bytes < 1 or max_depth < 0:
        raise ValueError("max_assets, max_matches and max_bytes must be positive")

    base_url = _get_base_url().rstrip("/")
    root_url = f"{base_url}/"
    origin = _origin(root_url)
    web_session = session if session is not None else get_web_session()
    http = build_requests_session(web_session, base_url)

    root = _read_text(http, root_url, budget_bytes=MAX_ROOT_BYTES)
    if root.status != 200:
        raise RuntimeError(f"Console root {root_url} returned HTTP {root.status}")
    if _origin(root.final_url) != origin:
        raise RuntimeError(
            f"Console root {root_url} redirected off-origin to {_origin(root.final_url)}; the web "
            "session is not valid for this console (run `inspire login`)"
        )
    if "html" not in root.media_type:
        raise RuntimeError(
            f"Console root {root_url} returned {root.media_type or 'unknown'} content, not HTML; "
            "check the configured base URL"
        )

    entry_assets: list[str] = []
    external: list[str] = []
    for match in _SCRIPT_SRC_RE.finditer(root.text):
        absolute = _absolute(root.final_url, match.group("src"))
        if absolute is None:
            continue
        if _origin(absolute) != origin:
            if absolute not in external:
                external.append(absolute)
            continue
        if absolute not in entry_assets:
            entry_assets.append(absolute)
    if not entry_assets:
        raise RuntimeError(
            f"Console root {root_url} referenced no same-origin JavaScript; the console build or "
            "the configured base URL is not what this command expects"
        )

    budget = _Budget(
        max_assets=max_assets,
        max_matches=max_matches,
        max_depth=max_depth,
        max_bytes=max_bytes,
    )
    scanned: list[_Asset] = []
    skipped: list[dict[str, str]] = []
    _crawl(
        http,
        budget=budget,
        origin=origin,
        seeds=entry_assets,
        scanned=scanned,
        skipped=skipped,
        external=external,
    )

    index = _build_index(scanned)
    actions = _matching_actions(index, needle, budget)
    terms = _search_terms(needle, index)
    candidates = _collect_candidates(scanned, terms, budget)

    warnings: list[str] = []
    if budget.deferred:
        warnings.append(
            f"{budget.deferred} referenced asset(s) were not fetched inside the budget; raise "
            "--max-assets (and --max-bytes if the byte ceiling was the limit) to widen the sweep"
        )
    if not actions and budget.sweep_truncated:
        warnings.append(
            "no reference matched the query in the scanned slice, and the budget truncated the "
            "sweep: this is not evidence that the Action does not exist"
        )
    elif not actions:
        warnings.append(
            f"no endpoint reference in the scanned JavaScript mentions {query!r}; check the "
            "spelling, or inspect the calling console page with `inspire api eval`"
        )
    if external:
        warnings.append(
            f"{len(external)} cross-origin script reference(s) were not fetched; session cookies "
            "are never sent off-origin"
        )

    return _redact(
        {
            "mode": "source",
            "query": str(query),
            "provenance": {
                "console_url": root_url,
                "origin": origin,
                "entry_status": root.status,
                "entry_content_type": root.media_type,
                "entry_assets": entry_assets[:20],
                "assets_scanned": [asset.url for asset in scanned][:40],
                "assets_skipped": skipped[:20],
                "cross_origin_skipped": external[:10],
            },
            "actions": actions,
            "evidence": [candidate.to_dict() for candidate in candidates],
            "budget": budget.to_dict(),
            "warnings": warnings,
        },
    )


# --------------------------------------------------------------------------------------
# Source mode
# --------------------------------------------------------------------------------------


@dataclass
class _Budget:
    """The counters a bounded sweep is judged against after it ran."""

    max_assets: int
    max_matches: int
    max_depth: int
    max_bytes: int
    assets_fetched: int = 0
    assets_failed: int = 0
    bytes_read: int = 0
    matches_seen: int = 0
    matches_dropped: int = 0
    deferred: int = 0
    reasons: list[str] = field(default_factory=list)

    def mark(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    @property
    def sweep_truncated(self) -> bool:
        """True when the asset walk itself stopped early, as opposed to the evidence ranking."""
        return self.deferred > 0 or any(reason in SWEEP_REASONS for reason in self.reasons)

    @property
    def stopped(self) -> bool:
        """True when any ceiling bound what the call returned."""
        return bool(self.reasons)

    @property
    def out_of_budget(self) -> bool:
        return self.assets_fetched >= self.max_assets or self.bytes_read >= self.max_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_assets": self.max_assets,
            "max_matches": self.max_matches,
            "max_depth": self.max_depth,
            "max_bytes": self.max_bytes,
            "assets_fetched": self.assets_fetched,
            "assets_failed": self.assets_failed,
            "bytes_read": self.bytes_read,
            "matches_seen": self.matches_seen,
            "matches_dropped": self.matches_dropped,
            "deferred_references": self.deferred,
            "truncated": self.stopped,
            "sweep_truncated": self.sweep_truncated,
            "truncation_reasons": list(self.reasons),
            "excerpt_chars": MAX_EXCERPT_CHARS,
        }


@dataclass
class _Asset:
    url: str
    depth: int
    text: str


@dataclass
class _Endpoint:
    action: str
    service: str
    path: str
    method: str = "POST"
    constants: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "service": self.service,
            "method": self.method,
            "path": self.path,
            "constants": self.constants[:5],
            "assets": self.assets[:3],
        }


@dataclass
class _Candidate:
    asset: str
    offset: int
    text: str
    terms: list[str]
    relevance: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "offset": self.offset,
            "text": self.text,
            "terms": self.terms,
            "relevance": self.relevance,
        }


@dataclass
class _Fetched:
    status: int
    media_type: str
    text: str
    bytes_read: int
    final_url: str
    truncated: bool


def _absolute(importer_url: str, ref: str) -> Optional[str]:
    """Resolve one script/module reference to an absolute URL, or None if it cannot be resolved.

    ``./x.js`` and ``../x.js`` resolve against the importing file, ``/x.js`` against the console
    root, and a bare path against the root when it already carries a directory (the Vite preload
    manifest spells chunks ``assets/x.js``) or against the importing file's directory when it does
    not. Template-literal and wildcard references cannot be resolved without running the bundle.
    Cross-origin URLs come back unchanged: the caller drops them, and says so in the report.
    """
    candidate = str(ref or "").strip()
    if not candidate or "${" in candidate or "*" in candidate:
        return None
    if candidate.startswith(("http://", "https://", "//")):
        return urljoin(importer_url, candidate)
    if candidate.startswith("/"):
        return f"{_origin(importer_url)}{candidate}"
    if candidate.startswith(("./", "../")):
        return urljoin(importer_url, candidate)
    if "/" in candidate:
        return f"{_origin(importer_url)}/{candidate}"
    return urljoin(importer_url, candidate)


def _read_text(http: Any, url: str, *, budget_bytes: int) -> _Fetched:
    """Download a URL as text, classifying it by content type before reading the body."""
    response = http.get(
        url,
        headers={"Accept": "*/*", "Cache-Control": "no-cache", "Pragma": "no-cache"},
        timeout=SOURCE_TIMEOUT_SECONDS,
        stream=True,
    )
    try:
        headers = getattr(response, "headers", None) or {}
        content_type = str(headers.get("Content-Type") or headers.get("content-type") or "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        raw, truncated = _read_body(response, budget_bytes)
        charset = _charset(content_type) or "utf-8"
        return _Fetched(
            status=int(getattr(response, "status_code", 0) or 0),
            media_type=media_type,
            text=raw.decode(charset, errors="replace"),
            bytes_read=len(raw),
            final_url=str(getattr(response, "url", "") or url),
            truncated=truncated,
        )
    finally:
        closer = getattr(response, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - a socket that will not close is not a failure
                pass


def _read_body(response: Any, budget_bytes: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        for chunk in iterator(65536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= budget_bytes:
                truncated = True
                break
    else:  # pragma: no cover - only a transport with no streaming support lands here
        content = bytes(getattr(response, "content", b"") or b"")
        truncated = len(content) > budget_bytes
        chunks = [content[:budget_bytes]]
    return b"".join(chunks), truncated


def _charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset":
            return value.strip("'\"")
    return ""


def _crawl(
    http: Any,
    *,
    budget: _Budget,
    origin: str,
    seeds: list[str],
    scanned: list[_Asset],
    skipped: list[dict[str, str]],
    external: list[str],
) -> None:
    """Walk the console's JavaScript breadth-first, bounded by ``budget``.

    Relative references resolve against the file that carries them, which is what a browser does
    with an ``import()`` specifier - the entry page URL is therefore only used for the HTML's own
    ``<script src>`` attributes.
    """
    queue: deque[tuple[str, int]] = deque((url, 0) for url in seeds)
    seen: set[str] = set(seeds)
    while queue:
        url, depth = queue.popleft()
        if depth > budget.max_depth:
            budget.mark("max_depth")
            budget.deferred += 1
            continue
        if budget.out_of_budget:
            budget.mark("max_assets" if budget.assets_fetched >= budget.max_assets else "max_bytes")
            budget.deferred += 1
            continue
        try:
            fetched = _read_text(http, url, budget_bytes=min(MAX_ASSET_BYTES, budget.max_bytes))
        except Exception as exc:  # noqa: BLE001 - one dead chunk must not end the sweep
            budget.assets_failed += 1
            skipped.append({"asset": url, "reason": f"fetch failed: {exc}"[:160]})
            continue
        if fetched.truncated:
            budget.mark("max_bytes")
        if fetched.status != 200:
            budget.assets_failed += 1
            skipped.append({"asset": url, "reason": f"HTTP {fetched.status}"})
            continue
        reason = _reject_reason(fetched)
        if reason is not None:
            budget.assets_failed += 1
            skipped.append({"asset": url, "reason": reason})
            continue
        budget.assets_fetched += 1
        budget.bytes_read += fetched.bytes_read
        scanned.append(_Asset(url=url, depth=depth, text=fetched.text))
        for ref in _MODULE_REF_RE.findall(fetched.text):
            resolved = _absolute(url, ref)
            if resolved is None:
                budget.mark("unresolvable-dynamic-references")
                continue
            if _origin(resolved) != origin:
                if resolved not in external:
                    external.append(resolved)
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            queue.append((resolved, depth + 1))


def _reject_reason(fetched: _Fetched) -> Optional[str]:
    """Why this 200 response is not JavaScript, or None when it is."""
    if fetched.media_type in JS_MEDIA_TYPES:
        return None
    body_head = fetched.text.lstrip()[:15].lower()
    if "html" in fetched.media_type or body_head.startswith(("<!doctype html", "<html")):
        # The console answers an unknown asset path with its SPA shell at HTTP 200. Scanned as
        # JavaScript it would hand back the same entry references for every missing chunk, so
        # the walk would never stop and every asset would look like the entry.
        return "html-fallback (HTTP 200 with an HTML body, not JavaScript)"
    if fetched.media_type:
        return f"not JavaScript: {fetched.media_type}"
    # A 200 with no usable content type and a non-HTML body is judged on its extension.
    return None if urlsplit(fetched.final_url).path.endswith((".js", ".mjs")) else "not JavaScript"


def _resolve_action(match: re.Match[str], prefixes: dict[str, str]) -> tuple[str, str, str]:
    version = match.group("version")
    prefix = (
        f"/api/v{version}"
        if version
        else prefixes.get(match.group("var") or "") or _get_browser_api_prefix()
    )
    service = (match.group("service") or "").strip("/\\")
    action = match.group("action")
    path = f"{prefix}/{service}?Action={action}" if service else f"{prefix}?Action={action}"
    return path, service, action


def _build_index(scanned: list[_Asset]) -> dict[str, _Endpoint]:
    """Every ``<prefix>/<service>?Action=<Action>`` reference, with the table keys it uses."""
    prefixes: dict[str, str] = {}
    for asset in scanned:
        for match in _PREFIX_CONSTANT_RE.finditer(asset.text):
            prefixes.setdefault(match.group("var"), match.group("prefix"))

    index: dict[str, _Endpoint] = {}
    for asset in scanned:
        for match in _ACTION_RE.finditer(asset.text):
            path, service, action = _resolve_action(match, prefixes)
            if not service and path.startswith("/api/v2?"):
                continue
            entry = index.get(path)
            if entry is None:
                entry = _Endpoint(action=action, service=service, path=path)
                index[path] = entry
            if asset.url not in entry.assets:
                entry.assets.append(asset.url)
    _attach_constant_keys(index, scanned, prefixes)
    return index


def _attach_constant_keys(
    index: dict[str, _Endpoint], scanned: list[_Asset], prefixes: dict[str, str]
) -> None:
    """Record each endpoint's own table key, not every key sharing its Action name."""
    for asset in scanned:
        for match in _ACTION_KEY_RE.finditer(asset.text):
            endpoint_match = _ACTION_RE.search(match.group(0))
            if endpoint_match is None:
                continue
            path, _service, _action = _resolve_action(endpoint_match, prefixes)
            entry = index.get(path)
            if entry is None:
                continue
            key = match.group("key")
            if key not in entry.constants:
                entry.constants.append(key)


def _matching_actions(
    index: dict[str, _Endpoint], needle: str, budget: _Budget
) -> list[dict[str, Any]]:
    """Actions whose Action name, service segment or path mentions ``needle``; exact matches first."""
    ranked: list[tuple[int, _Endpoint]] = []
    for entry in index.values():
        action = entry.action.lower()
        if action == needle:
            rank = 0
        elif needle in action:
            rank = 1
        elif needle in entry.service.lower():
            rank = 2
        elif needle in entry.path.lower():
            rank = 3
        else:
            continue
        ranked.append((rank, entry))
    ranked.sort(key=lambda item: (item[0], item[1].action, item[1].path))
    selected = [entry for _rank, entry in ranked[: budget.max_matches]]
    if len(ranked) > len(selected):
        budget.matches_dropped += len(ranked) - len(selected)
        budget.mark("max_matches")
    return [entry.to_dict() for entry in selected]


def _search_terms(needle: str, index: dict[str, _Endpoint]) -> set[str]:
    """Tokens whose excerpts explain the request: the query, the Action, and its table keys."""
    terms = {needle}
    for path, entry in index.items():
        if needle in entry.action.lower() or needle in path.lower():
            terms.add(entry.action.lower())
            terms.update(key.lower() for key in entry.constants)
    return {term for term in terms if len(term) >= 3}


def _collect_candidates(
    scanned: list[_Asset], terms: set[str], budget: _Budget
) -> list[_Candidate]:
    """Bounded, relevance-ranked code excerpts around each term hit."""
    candidates: list[_Candidate] = []
    ordered_terms = sorted(terms)
    for asset in scanned:
        lowered = asset.text.lower()
        hits = 0
        for term in ordered_terms:
            if hits >= MAX_HITS_PER_ASSET:
                budget.matches_dropped += lowered.count(term)
                budget.mark("max_matches")
                continue
            start = lowered.find(term)
            while start != -1:
                budget.matches_seen += 1
                if hits >= MAX_HITS_PER_ASSET:
                    budget.matches_dropped += 1
                    budget.mark("max_matches")
                    start = lowered.find(term, start + len(term))
                    continue
                begin = max(0, start - EXCERPT_RADIUS)
                end = min(len(asset.text), start + len(term) + EXCERPT_RADIUS)
                excerpt = " ".join(asset.text[begin:end].split())
                if len(excerpt) > MAX_EXCERPT_CHARS:
                    excerpt = excerpt[: MAX_EXCERPT_CHARS - 1] + "…"
                    budget.mark("excerpt-truncated")
                others = sorted(other for other in terms if other != term and other in excerpt)
                lowered_excerpt = excerpt.lower()
                request_clues = sum(
                    marker in lowered_excerpt for marker in ("filter:", "params:", "body:", "page")
                )
                candidates.append(
                    _Candidate(
                        asset=asset.url,
                        offset=start,
                        text=excerpt,
                        terms=[term, *others],
                        relevance=1 + len(others) + (1 if asset.depth else 0) + request_clues * 3,
                    )
                )
                hits += 1
                start = lowered.find(term, start + len(term))
    candidates.sort(key=lambda item: (-item.relevance, item.asset, item.offset))
    if len(candidates) > budget.max_matches:
        budget.matches_dropped += len(candidates) - budget.max_matches
        budget.mark("max_matches")
    return candidates[: budget.max_matches]


# --------------------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------------------


def _redact(value: Any, known_secrets: tuple[str, ...] = ()) -> Any:
    """Mask credential-shaped text anywhere in a report, without changing its structure."""
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _is_sensitive_key(key) else _redact(item, known_secrets))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, known_secrets) for item in value]
    if isinstance(value, str):
        for secret in known_secrets:
            if secret:
                value = value.replace(secret, _REDACTED)
        for pattern in _SENSITIVE_TEXT_RES:
            value = pattern.sub(_REDACTED, value)
        return value
    return value


def _is_sensitive_key(key: object) -> bool:
    lowered = str(key).strip().lower().replace("-", "_")
    return lowered in _SENSITIVE_KEYS or any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def main(argv: Optional[list[str]] = None) -> int:
    """Write one source report for the platform snapshot's bounded subprocess."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("query", help="Action name, endpoint fragment, or console source term")
    parser.add_argument("--max-assets", type=int, default=DEFAULT_MAX_ASSETS)
    parser.add_argument("--max-matches", type=int, default=DEFAULT_MAX_MATCHES)
    args = parser.parse_args(argv)
    report = discover_source(
        args.query,
        max_assets=args.max_assets,
        max_matches=args.max_matches,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
