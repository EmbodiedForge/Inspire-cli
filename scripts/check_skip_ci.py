#!/usr/bin/env python3
"""Reject skip-CI markers outside the automated release commit.

Forgejo/Gitea skip a whole push when *any* pushed commit message contains a
skip-ci pattern, and the match runs over the entire message body, not just the
subject line. So a commit that merely *talks about* the mechanism - "the release
commit carries [skip ci]" - silently disables CI for that push, along with every
other commit in it. This happened twice on main and looked exactly like a dead
runner.

Only the release commit may carry the marker (python-semantic-release writes it
via [tool.semantic_release] commit_message, and without it the release commit
would re-trigger the bump job).

Fix for a rejected message: hyphenate the word ([skip-ci], "skip-ci marker") -
prose about the feature never needs the literal marker - or, if this commit is
genuinely meant to skip CI and is not a release, use `git commit --no-verify`
and say why in the message.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# The only subject allowed to carry a skip marker; PSR's commit_message is
# "chore(release): bump version {version} [skip ci]".
RELEASE_SUBJECT = re.compile(r"^chore\(release\):")

# The patterns Forgejo/Gitea treat as skip markers. Bracketed forms plus the bare
# phrases, deliberately excluding bare "no ci", which is too common in English
# prose to block blindly.
MARKER = re.compile(
    "|".join(
        [
            r"\[\s*skip ci\s*\]",
            r"\[\s*ci skip\s*\]",
            r"\[\s*skip actions\s*\]",
            r"\[\s*actions skip\s*\]",
            r"\[\s*skip workflows\s*\]",
            r"\[\s*workflows skip\s*\]",
            r"\[\s*no ci\s*\]",
            r"\bskip ci\b",
            r"\bci skip\b",
            r"\bskip actions\b",
            r"\bskip workflows\b",
        ]
    ),
    re.IGNORECASE,
)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("check_skip_ci: expected exactly one commit-msg file path", file=sys.stderr)
        return 2

    path = Path(argv[1])
    try:
        message = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"check_skip_ci: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    # Git puts comments (#) and the diff in the same file depending on flags.
    lines = [line for line in message.splitlines() if not line.startswith("#")]
    if not lines:
        return 0

    if RELEASE_SUBJECT.match(lines[0].strip()):
        return 0

    offenders = [
        (number, line)
        for number, line in enumerate(message.splitlines(), 1)
        if line.startswith("#") is False and MARKER.search(line)
    ]
    if not offenders:
        return 0

    print(
        "check_skip_ci: this commit carries a skip-CI marker, so Forgejo would skip\n"
        "CI for the entire push - including every other commit in it. The match also\n"
        "runs over the body, so writing about the mechanism trips it too.\n",
        file=sys.stderr,
    )
    for number, line in offenders:
        print(f"  line {number}: {line.strip()}", file=sys.stderr)
    print(
        "\nOnly a 'chore(release):' subject (the automated release commit) may carry\n"
        "a skip marker. Otherwise write [skip-ci] or 'skip-ci marker' in prose, or\n"
        "use 'git commit --no-verify' if skipping CI really is intended.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
