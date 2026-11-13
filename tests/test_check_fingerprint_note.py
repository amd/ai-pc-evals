# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""tools/check_fingerprint_note.py: a branch that changes a fingerprinted file must say so in the CHANGELOG."""
import shutil
import subprocess

import pytest

import check_fingerprint_note as cfn

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    """A main branch with a fingerprinted file, a folder entry, another file and a CHANGELOG; then a branch."""
    for rel, text in {"kwengine/grade.py": "x = 1\n", "knowledgework/developer/toybox/a.py": "a\n",
                      "tools/run_eval.py": "y\n", "CHANGELOG.md": "# Changelog\n\n## Unreleased\n"}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    git(tmp_path, "checkout", "-q", "-b", "change")
    return tmp_path


def commit(repo, changes):
    for rel, text in changes.items():
        path = repo / rel
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "change")


@pytest.mark.parametrize("changes", [
    {"kwengine/grade.py": "x = 2\n"},                          # a fingerprinted file
    {"knowledgework/developer/toybox/b.py": "new\n"},          # a file added in a fingerprinted folder
    {"kwengine/grade.py": None},                               # one deleted
])
def test_a_fingerprinted_change_needs_a_changelog_line_that_says_so(repo, changes):
    commit(repo, changes)
    why = cfn.problem("main", repo)
    assert why and "fingerprinted files" in why
    commit(repo, {"CHANGELOG.md": "# Changelog\n\n## Unreleased\n\n- Grading: tweak. The task fingerprint changes.\n"})
    assert cfn.problem("main", repo) is None


def test_other_changes_and_a_changelog_line_without_the_word_pass_or_fail_as_they_should(repo):
    commit(repo, {"tools/run_eval.py": "z\n"})  # not fingerprinted
    assert cfn.problem("main", repo) is None
    commit(repo, {"kwengine/grade.py": "x = 3\n", "CHANGELOG.md": "# Changelog\n\n## Unreleased\n\n- Grading.\n"})
    assert "kwengine/grade.py" in cfn.problem("main", repo)


def test_the_folder_and_file_entries_match_as_paths_not_prefixes():
    entries = ("kwengine/grade.py", "knowledgework/developer/toybox")
    assert cfn.fingerprinted(["kwengine/grade.py", "kwengine/grade.pyc", "knowledgework/developer/toybox/x",
                              "knowledgework/developer/toybox2/x"], entries) == \
        ["kwengine/grade.py", "knowledgework/developer/toybox/x"]


def test_a_base_that_cant_be_compared_is_an_error_not_a_pass(capsys):
    assert cfn.main(["--base", "no-such-ref-for-this-test"]) == 2
    assert "could not compare with no-such-ref-for-this-test" in capsys.readouterr().out
