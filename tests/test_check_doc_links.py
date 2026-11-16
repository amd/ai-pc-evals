# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""tools/check_doc_links.py: every relative Markdown link resolves, anchors included, as GitHub renders them."""
import shutil
import subprocess

import pytest

import check_doc_links as cdl

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

GUIDE = """# Guide

## The quick way: one entry in `agents.json`

## Setup
## Setup

<a id="custom-spot"></a>
"""


def repo_with(tmp_path, readme):
    files = {"README.md": readme, "docs/GUIDE.md": GUIDE, "tools/x.py": "",
             "knowledgework/assistant/corpus/note.md": "[gone](nowhere.md)",  # a task input: not checked
             "knowledgework/portable/prompts/p.md": "[gone](nowhere.md)"}
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_good_links_pass(tmp_path):
    readme = """See [the guide](docs/GUIDE.md), [the quick way](docs/GUIDE.md#the-quick-way-one-entry-in-agentsjson),
[setup again](docs/GUIDE.md#setup-1), [a spot](docs/GUIDE.md#custom-spot), [code](tools/x.py), [site](https://x.org/a),
[mail](mailto:a@b.c), [top](#readme-title) and [answers](answers/) before they are published.

# README title

```
[not a link](missing.md)
```
Inline `[nor this](missing.md)`.

[ref]: docs/GUIDE.md#setup
"""
    assert cdl.broken(repo_with(tmp_path, readme)) == []


@pytest.mark.parametrize("link, why", [
    ("[old](docs/SECURITY.md)", "docs/SECURITY.md: no such file"),
    ("[gone](docs/GUIDE.md#setup-2)", "no heading with that id"),
    ("[self](#nowhere)", "no heading with that id"),
    ("[ref]: docs/OLD.md", "docs/OLD.md: no such file"),
])
def test_a_broken_link_or_anchor_is_reported_with_its_line(tmp_path, link, why):
    problems = cdl.broken(repo_with(tmp_path, f"# Title\n\n{link}\n"))
    assert len(problems) == 1 and problems[0].startswith("README.md:3: ") and why in problems[0]


def test_links_into_answers_count_once_the_answers_are_published(tmp_path):
    repo = repo_with(tmp_path, "# T\n\n[keys](answers/keys/x.json)\n")
    assert cdl.broken(repo) == []  # not published yet: answers/ doesn't exist
    (repo / "answers").mkdir()
    assert "answers/keys/x.json: no such file" in cdl.broken(repo)[0]


@pytest.mark.parametrize("heading, slug", [
    ("The quick way: one entry in `agents.json`", "the-quick-way-one-entry-in-agentsjson"),
    ("What `run_eval.py` writes", "what-run_evalpy-writes"),
    ("Layer 4 on Linux (sudo, ACLs)", "layer-4-on-linux-sudo-acls"),
    ("[Linked](x.md) **bold** heading", "linked-bold-heading"),
])
def test_heading_ids_follow_github(heading, slug):
    assert cdl.slug(heading) == slug


def test_the_repository_has_no_broken_links():
    assert cdl.broken() == []
