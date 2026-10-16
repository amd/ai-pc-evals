"""Print one version's section of CHANGELOG.md, for the GitHub release.

usage: python tools/release_notes.py v0.2.0 [--changelog CHANGELOG.md]

Exits 1 if the version has no section, so a release can't go out without its notes.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def section(changelog: str, version: str) -> str:
    """The text under '## v<version>' up to the next '## ' heading."""
    v = version if version.startswith("v") else f"v{version}"
    m = re.search(rf"^## {re.escape(v)}\s*$(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    if not m or not m.group(1).strip():
        raise SystemExit(f"ERROR: CHANGELOG.md has no notes for {v}; add a '## {v}' section before releasing")
    return m.group(1).strip() + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("version")
    ap.add_argument("--changelog", type=Path, default=REPO / "CHANGELOG.md")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    print(section(args.changelog.read_text(encoding="utf-8"), args.version), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
