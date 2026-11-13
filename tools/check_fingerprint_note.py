# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Fail a change that touches a fingerprinted file without saying so in the CHANGELOG.

usage: python tools/check_fingerprint_note.py [--base REF]

The task fingerprint (tools/evalversion.py, FINGERPRINTED) covers what a score depends on: the task files, the
builders, the engine's grading files, the planted projects. Changing any of them makes results graded before not
directly comparable, and CLAUDE.md asks for a CHANGELOG line that says so. This lists the fingerprinted files the
branch changes since its merge base with --base (default origin/main), and exits 1 if there are some and no line
the branch adds to CHANGELOG.md mentions the fingerprint. CI runs it on every pull request (lint.yml).
Standard library only.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evalversion  # noqa: E402

REPO = evalversion.REPO


def _git(*args: str, repo: Path = REPO) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True).stdout


def changed_files(base: str, repo: Path = REPO) -> List[str]:
    """Every path the branch adds, changes, deletes or renames since its merge base with *base* (both names of a
    rename)."""
    return [p for p in _git("diff", "--name-only", "--no-renames", f"{base}...HEAD", repo=repo).splitlines() if p]


def fingerprinted(paths: Sequence[str], entries: Sequence[str] = evalversion.FINGERPRINTED) -> List[str]:
    """The *paths* the task fingerprint covers: an entry itself, or anything in an entry that is a folder."""
    return [p for p in paths if any(p == e or p.startswith(e.rstrip("/") + "/") for e in entries)]


def changelog_lines_added(base: str, repo: Path = REPO) -> List[str]:
    diff = _git("diff", "--unified=0", f"{base}...HEAD", "--", "CHANGELOG.md", repo=repo)
    return [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]


def problem(base: str, repo: Path = REPO) -> Optional[str]:
    """Why the branch fails the check, or None."""
    touched = fingerprinted(changed_files(base, repo))
    if not touched or any("fingerprint" in ln.lower() for ln in changelog_lines_added(base, repo)):
        return None
    shown = ", ".join(touched[:8]) + (f" and {len(touched) - 8} more" if len(touched) > 8 else "")
    return (f"this changes fingerprinted files ({shown}), so results graded before it are not directly comparable, "
            "but no line it adds to CHANGELOG.md says so: add one that mentions the task fingerprint "
            "(see 'Changes to ground truth' in CLAUDE.md)")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="origin/main", help="what the branch is compared with (default origin/main)")
    args = ap.parse_args(argv)
    try:
        why = problem(args.base)
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", "") or e
        print(f"::error::could not compare with {args.base}: {str(detail).strip()}")
        return 2
    if why:
        print(f"::error file=CHANGELOG.md::{why}")
        return 1
    print("fingerprint note: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
