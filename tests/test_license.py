# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Every source file carries the MIT header, and the release ships the licence."""
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPDX = "SPDX-License-Identifier: MIT"
#: Exempt: task inputs (their bytes are part of the task).
EXEMPT = ("knowledgework/assistant/corpus/", "knowledgework/developer/toybox/", "knowledgework/fixtures/")


def tracked(*patterns):
    try:
        out = subprocess.run(["git", "ls-files", *patterns], cwd=REPO, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return out.stdout.splitlines()


def test_every_source_file_has_the_spdx_header():
    missing = [f for f in tracked("*.py", "*.sh", "*.ps1")
               if not f.startswith(EXEMPT)
               and SPDX not in "".join((REPO / f).read_text(encoding="utf-8").splitlines(keepends=True)[:4])]
    assert not missing, f"add the two-line copyright and SPDX header (see kwengine/grade.py) to: {missing}"


def test_the_licence_and_notices_ship_in_the_release():
    import sys

    sys.path.insert(0, str(REPO / "tools"))
    import package

    shipped = package.shipped(package.tracked_files())
    assert {"LICENSE", "THIRD_PARTY_NOTICES.md"} <= set(shipped)
    assert "MIT License" in (REPO / "LICENSE").read_text(encoding="utf-8")
