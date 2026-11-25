#!/usr/bin/env python3
"""Reject a built wheel whose packaged skill docs differ from the source tree.

The release gate used to prove only that the wheel reports the tag's version. A wheel
can pass that while ``package-data`` quietly stops matching ``inspire/skills``, leaving
an installed CLI whose ``inspire skills`` surface is empty. Both release paths build a
wheel (``release_bump.sh`` and ``release.yml``); both call this script, which needs only
the standard library.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

WHEEL_SKILLS_PREFIX = "inspire/skills/"


def _skills_in_tree(skills_dir: Path) -> set[str]:
    return {
        path.relative_to(skills_dir).as_posix() for path in skills_dir.rglob("*") if path.is_file()
    }


def _skills_in_wheel(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    return {
        name[len(WHEEL_SKILLS_PREFIX) :]
        for name in names
        if name.startswith(WHEEL_SKILLS_PREFIX) and not name.endswith("/")
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="built wheel to inspect")
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=Path("inspire/skills"),
        help="source skill tree to compare against (default: inspire/skills)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.wheel.is_file():
        print(f"check_wheel_skills: wheel not found: {args.wheel}", file=sys.stderr)
        return 2
    if not args.skills_dir.is_dir():
        print(f"check_wheel_skills: skill tree not found: {args.skills_dir}", file=sys.stderr)
        return 2

    expected = _skills_in_tree(args.skills_dir)
    if not expected:
        print(
            f"check_wheel_skills: {args.skills_dir} has no files; refusing to pass an empty check",
            file=sys.stderr,
        )
        return 2
    actual = _skills_in_wheel(args.wheel)

    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        if missing:
            print("check_wheel_skills: wheel is missing packaged files:", file=sys.stderr)
            for entry in missing:
                print(f"  - inspire/skills/{entry}", file=sys.stderr)
        if extra:
            print("check_wheel_skills: wheel carries files absent from the tree:", file=sys.stderr)
            for entry in extra:
                print(f"  + inspire/skills/{entry}", file=sys.stderr)
        return 1

    skill_count = len({entry.split("/", 1)[0] for entry in expected})
    print(
        f"check_wheel_skills: {len(expected)} file(s) across {skill_count} skill(s) "
        f"match {args.wheel.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
