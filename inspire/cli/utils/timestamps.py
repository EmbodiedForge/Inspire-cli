"""One place that turns platform timestamps into UTC ISO 8601 strings.

The same instant reaches the CLI in different spellings depending on the endpoint: the job list
API sends epoch milliseconds as a string, other payloads send epoch seconds or ISO 8601, and .NET
serialises an unset ``DateTime`` as ``0001-01-01T00:00:00`` (or its epoch-milliseconds form, a
large negative number). Every reader that formatted one of these had to guess which spelling it
held, so the guessing lives here once.

``format_timestamp_utc`` answers a single UTC ISO 8601 shape, or ``None`` when the value is not a
moment anyone acted on: the unset sentinel, a non-positive epoch, an empty value or garbage.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

#: Epoch values at or above this are milliseconds, below it seconds - the same threshold the
#: list-ordering code in ``browser_api.jobs`` already uses. Current epoch seconds (~1.8e9) sit far
#: below it and current epoch milliseconds (~1.8e12) far above, so the boundary only matters for
#: far-future seconds or 1970-era milliseconds, neither of which the platform sends.
_EPOCH_MILLISECONDS_THRESHOLD = 10_000_000_000

#: .NET's round-trip ("o") format emits 7 fractional digits; ``fromisoformat`` accepts at most 6.
_EXCESS_FRACTION = re.compile(r"\.(\d{7,})")

__all__ = ["format_timestamp_utc"]


def format_timestamp_utc(value: object) -> str | None:
    """Format a timestamp as a UTC ISO 8601 string (``YYYY-MM-DDTHH:MM:SSZ``), or None.

    Accepts the spellings the platform actually sends: epoch seconds and epoch milliseconds (as
    numbers or numeric strings, the list API's string milliseconds included) and ISO 8601 strings
    (a naive string is read as UTC, an offset is converted). Non-positive epochs - zero and .NET's
    negative ``DateTime.MinValue`` milliseconds - the ``0001-01-01`` empty-date sentinel and
    unparseable values all answer None: an unset timestamp is absent, not "1970-01-01".
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _format_epoch(float(value))
    text = str(value).strip()
    if not text:
        return None
    try:
        return _format_epoch(float(text))
    except ValueError:
        return _format_iso(text)


def _format_epoch(epoch: float) -> str | None:
    if not math.isfinite(epoch) or epoch <= 0:
        return None
    seconds = epoch / 1000 if epoch >= _EPOCH_MILLISECONDS_THRESHOLD else epoch
    try:
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - absurd magnitudes only
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_iso(text: str) -> str | None:
    normalized = _EXCESS_FRACTION.sub(lambda match: "." + match.group(1)[:6], text)
    if normalized.endswith("Z"):
        # fromisoformat only learned the Z suffix in Python 3.11; this CLI runs on 3.10.
        normalized = normalized[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    if moment.timestamp() <= 0:
        # .NET's DateTime.MinValue (0001-01-01T00:00:00) is "never set", not a moment in time.
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
