#!/usr/bin/env python3
"""Lightweight pre-commit check for hardcoded API keys and tokens."""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path


ALLOWLIST_MARKERS = (
    "pragma: allowlist secret",
    "pragma: allowlist-secret",
    "nosec",
)

# Unquoted values (`.env`, YAML, TOML) count as well as quoted ones, but only inside an
# opaque-token alphabet that carries a digit. In source code the same shape is an expression -
# `token = _extract_jupyter_token(url)`, `api_key = os.environ.get(ENV)` - and letting an
# unquoted value run to whitespace turned every one of those into a finding (41 in this tree).
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"""
    (?P<name>\b[A-Za-z0-9_.-]*
        (?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|
           token|secret|password|passwd|pwd|authorization|cookie)
        [A-Za-z0-9_.-]*\b)
    \s*[:=]\s*
    (?P<value>
        (?P<quote>["'])[^"']{4,}(?P=quote)
        |
        (?=[A-Za-z0-9_+/=-]*[0-9])[A-Za-z0-9_+/=-]{8,}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

URL_TOKEN_RE = re.compile(
    r"\b(?P<name>access_token|refresh_token|id_token|token)=(?P<value>[^&\s\"')]{8,})",
    re.IGNORECASE,
)

BEARER_LITERAL_RE = re.compile(
    r"\bBearer\s+(?P<value>[A-Za-z0-9._~+/=-]{12,})",
    re.IGNORECASE,
)

# A provider key begins a token, so a word character, hyphen or underscore before it means the
# match started inside an ordinary word: prose like `task-queue-max-retries-exhausted` otherwise
# reads as `sk-` plus 20+ characters of `-` and lowercase, i.e. a leaked key.
PROVIDER_SECRET_RE = re.compile(
    r"""
    (?<![A-Za-z0-9_-])
    (?:
        gh[pousr]_[A-Za-z0-9_]{20,} |
        sk-[A-Za-z0-9_-]{20,} |
        xox[baprs]-[A-Za-z0-9-]{20,} |
        hf_[A-Za-z0-9]{20,} |
        AKIA[0-9A-Z]{16} |
        AIza[0-9A-Za-z_-]{20,} |
        Sii[A-Za-z0-9_@#$%^&*+=!?.-]{4,}
    )
    """,
    re.VERBOSE,
)

PRIVATE_KEY_MARKER_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

# Applied with fullmatch(), never search(): these are whole-value placeholders. An unanchored
# search whitelisted any real credential that merely contained "test" or "valid".
PLACEHOLDER_RE = re.compile(
    r"""
    (?:
        ^$ | your | example | sample | dummy | mock | fake | test |
        token-?123 | abc123 | xxx | placeholder | redacted |
        <[^>]+> | \.\.\. | changeme | change-me | secret |
        password | passwd | valid | invalid | none | null | empty |
        demo | session_id
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# `token = "CONSTANT_NAME"` is a fixture naming a constant, and only a quoted value can be one:
# those same characters unquoted in a `.env` line are what a leaked uppercase hex key looks like.
UPPER_SNAKE_CONSTANT_RE = re.compile(r"[A-Z][A-Z0-9_]+")

# An unquoted value that is a plain lowercase identifier is code naming something
# (`token = load_token_from_vault_v2`), not a literal, however secret its entropy reads.
LOWER_SNAKE_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_]*")


def entropy(value: str) -> float:
    if not value:
        return 0.0
    return -sum(
        (value.count(ch) / len(value)) * math.log2(value.count(ch) / len(value))
        for ch in set(value)
    )


def character_classes(value: str) -> int:
    return sum(
        bool(re.search(pattern, value))
        for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]")
    )


def is_placeholder(value: str, *, quoted: bool = False) -> bool:
    stripped = value.strip()
    if len(stripped) < 8:
        return True
    if PLACEHOLDER_RE.fullmatch(stripped):
        return True
    if stripped.startswith(("$", "${", "<", "{")):
        return True
    if quoted and UPPER_SNAKE_CONSTANT_RE.fullmatch(stripped):
        return True
    if not quoted and "_" in stripped and LOWER_SNAKE_IDENTIFIER_RE.fullmatch(stripped):
        return True
    return False


def looks_secret(value: str, *, quoted: bool = False) -> bool:
    # A provider-format key is never a placeholder, so this runs first: a real `sk-...`
    # that happens to contain "test" or "valid" must not be whitelisted by the checks below.
    if PROVIDER_SECRET_RE.search(value):
        return True
    if is_placeholder(value, quoted=quoted):
        return False
    return len(value) >= 20 and entropy(value) >= 3.5 and character_classes(value) >= 2


def issue(path: Path, line_no: int, label: str, value: str) -> str:
    return (
        f"{path}:{line_no}: possible hardcoded {label} "
        f"(length={len(value)}, entropy={entropy(value):.2f}). "
        "Use an environment variable or add 'pragma: allowlist secret' if this is a safe fixture."
    )


def scan_file(path: Path) -> list[str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [f"{path}:0: cannot read file: {exc}"]

    if b"\x00" in raw[:8192]:
        return []

    text = raw.decode("utf-8", errors="replace")
    findings: list[str] = []

    for line_no, line in enumerate(text.splitlines(), start=1):
        if any(marker in line for marker in ALLOWLIST_MARKERS):
            continue

        if PRIVATE_KEY_MARKER_RE.search(line):
            findings.append(issue(path, line_no, "private key", "PRIVATE_KEY"))

        for match in SENSITIVE_ASSIGNMENT_RE.finditer(line):
            # The quoted alternative captures its delimiters together with the value, so
            # they are dropped here; the unquoted alternative cannot contain a quote.
            value = match.group("value").strip("\"'")
            if looks_secret(value, quoted=match.group("quote") is not None):
                findings.append(issue(path, line_no, match.group("name"), value))

        # None of the three shapes below is a quoted string literal, so all are unquoted values.
        for match in URL_TOKEN_RE.finditer(line):
            value = match.group("value")
            if looks_secret(value):
                findings.append(issue(path, line_no, match.group("name"), value))

        for match in BEARER_LITERAL_RE.finditer(line):
            value = match.group("value")
            if looks_secret(value):
                findings.append(issue(path, line_no, "bearer token literal", value))

        for match in PROVIDER_SECRET_RE.finditer(line):
            value = match.group(0)
            if looks_secret(value):
                findings.append(issue(path, line_no, "provider secret", value))

    return findings


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv if Path(arg).is_file()]
    if not paths:
        # A scan over nothing is not a clean scan. Arguments that are not readable files - a
        # deleted path, the wrong working directory, an emptied file list - would otherwise exit 0
        # and certify a tree that was never opened.
        print(
            "Secret literal check: no files to scan "
            "(usage: check_secrets.py FILE...). Nothing was read, so nothing is certified clean.",
            file=sys.stderr,
        )
        return 2

    findings: list[str] = []
    for path in paths:
        findings.extend(scan_file(path))

    if findings:
        print("Secret literal check failed:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
