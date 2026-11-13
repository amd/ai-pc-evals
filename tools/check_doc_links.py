# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Check every relative link in the repository's Markdown: the file exists, and an #anchor names a heading in it.

usage: python tools/check_doc_links.py

Docs move (docs/SECURITY.md became docs/ISOLATION.md in #33) and headings get renamed; a link that silently
points nowhere is what a newcomer follows first. This reads every tracked .md file except task inputs (the
everyday corpus, the fixtures, the planted toybox project, the prompts: their bytes are part of a task), skips
code blocks, inline code and links to other sites, and checks each relative link and its anchor against
GitHub's heading ids. Links into answers/ are accepted while answers/ doesn't exist yet: it appears when the
answers are published. Exit 1 on a broken link. CI runs it (lint.yml). Standard library only.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote

REPO = Path(__file__).resolve().parents[1]
#: Task inputs and prompts: part of a task's ground truth, not docs.
SKIP = ("knowledgework/assistant/corpus/", "knowledgework/fixtures/", "knowledgework/developer/toybox/")
SKIP_PARTS = ("/prompts/",)
FENCE = re.compile(r"^\s*(```|~~~)")
CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
#: [text](target "title") and ![alt](target); the target may be in <...>.
INLINE = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)")
#: [label]: target
DEFINITION = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
HTML_ANCHOR = re.compile(r"<a\s+[^>]*(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.I)
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def tracked_docs(repo: Path = REPO) -> List[Path]:
    out = subprocess.run(["git", "ls-files", "*.md"], cwd=repo, capture_output=True, text=True, encoding="utf-8",
                         check=True).stdout
    return [repo / p for p in out.splitlines()
            if p and not p.startswith(SKIP) and not any(part in "/" + p for part in SKIP_PARTS)]


def prose_lines(text: str, keep_code: bool = False) -> Iterator[Tuple[int, str]]:
    """(line number, line) outside fenced code blocks, with inline code spans removed (kept for headings,
    whose ids include their text)."""
    fence: Optional[str] = None
    for n, line in enumerate(text.splitlines(), 1):
        m = FENCE.match(line)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            continue
        if fence is None:
            yield n, line if keep_code else CODE_SPAN.sub("", line)


def slug(heading: str) -> str:
    """GitHub's id for a heading: its text as shown (no Markdown), lowercased, punctuation dropped except - and _,
    spaces turned into -."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # a link shows its text
    text = re.sub(r"<[^>]+>", "", text).replace("`", "").replace("*", "")
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def anchors(path: Path, cache: Dict[Path, Set[str]]) -> Set[str]:
    if path not in cache:
        seen: Dict[str, int] = {}
        found: Set[str] = set()
        text = path.read_text(encoding="utf-8", errors="replace")
        for _, line in prose_lines(text, keep_code=True):
            found.update(HTML_ANCHOR.findall(line))
            m = HEADING.match(line)
            if m:
                base = slug(m.group(2))
                n = seen.get(base, 0)
                found.add(base if n == 0 else f"{base}-{n}")
                seen[base] = n + 1
        cache[path] = found
    return cache[path]


def links(text: str) -> Iterator[Tuple[int, str]]:
    for n, line in prose_lines(text):
        for m in INLINE.finditer(line):
            yield n, m.group(1)
        m = DEFINITION.match(line)
        if m:
            yield n, m.group(1)


def broken(repo: Path = REPO) -> List[str]:
    """``file:line: target: why`` for each broken relative link."""
    problems: List[str] = []
    cache: Dict[Path, Set[str]] = {}
    unpublished = not (repo / "answers").is_dir()
    for doc in tracked_docs(repo):
        rel_doc = doc.relative_to(repo).as_posix()
        for n, raw in links(doc.read_text(encoding="utf-8", errors="replace")):
            target = raw.strip("<>")
            if not target or SCHEME.match(target) or target.startswith("//"):
                continue
            path_part, _, anchor = target.partition("#")
            dest = (doc.parent / unquote(path_part)).resolve() if path_part else doc
            if path_part and not dest.exists():
                try:
                    inside = dest.relative_to(repo.resolve()).as_posix()
                    in_answers = inside == "answers" or inside.startswith("answers/")
                except ValueError:
                    in_answers = False
                if not (unpublished and in_answers):
                    problems.append(f"{rel_doc}:{n}: {target}: no such file")
                continue
            if anchor and dest.suffix.lower() == ".md" and dest.is_file() and \
                    unquote(anchor).lower() not in anchors(dest, cache):
                problems.append(f"{rel_doc}:{n}: {target}: no heading with that id in "
                                f"{dest.relative_to(repo.resolve()).as_posix()}")
    return problems


def main(argv: Optional[Sequence[str]] = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    problems = broken()
    for p in problems:
        print(f"::error::{p}")
    print(f"doc links: {len(problems)} broken" if problems else "doc links: all resolve")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
