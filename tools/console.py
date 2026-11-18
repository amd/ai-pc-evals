# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Output for a person at a terminal: ``key: value`` lines, wrapped under their value, and markdown turned into
plain text.

The tools write markdown to files, and print it when their output goes to a pipe, a log or CI, where it is
parsed or pasted. A terminal gets plain text instead (for_terminal()): a table as aligned columns when it fits
the window, else one ``key: value`` block per row. KW_MARKDOWN=1 keeps markdown on a terminal too.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from typing import Any, List, Optional, TextIO

#: Width of the key column: "judge note:" and the like, then one space.
KEY_WIDTH = 11
#: The marks summary tables use, in words.
MARKS = {"✅": "pass", "❌": "fail", "⚠️": "not graded", "⏳": "not run"}


def for_terminal(stream: Optional[TextIO] = None) -> bool:
    """True when *stream* (stdout) is a terminal a person reads, and KW_MARKDOWN doesn't ask for markdown."""
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("KW_MARKDOWN", "") not in ("1", "true")


def columns() -> int:
    return max(60, shutil.get_terminal_size((100, 24)).columns)


def wrap(text: str, indent: int, first: str = "") -> str:
    """*first* then *text*, wrapped at the terminal's width with later lines indented *indent* spaces. Only on a
    terminal: a log keeps each line whole, for grep."""
    if not for_terminal():
        return first + text
    lines = textwrap.wrap(text, max(30, columns() - indent - 1), break_on_hyphens=False, break_long_words=False)
    return first + ("\n" + " " * indent).join(lines or [""])


def kv(key: str, value: Any, indent: str = "  ") -> str:
    """One ``key: value`` line, values aligned; a long value wraps under itself on a terminal."""
    head = f"{indent}{(key + ':').ljust(KEY_WIDTH)} "
    return wrap(str(value), len(head), head)


def _cells(line: str) -> List[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def clip(text: str, n: int) -> str:
    """*text* cut to *n* characters, with an ellipsis when it was cut."""
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _words(cell: str) -> str:
    """A cell or line without ** and `, its leading mark (✅, ❌, ...) in words."""
    cell = re.sub(r"\*\*(.+?)\*\*", r"\1", cell).replace("`", "")
    for mark, word in MARKS.items():
        if cell == mark or cell.startswith(mark + " "):
            return word + cell[len(mark):]
    return cell


def _table(rows: List[List[str]]) -> List[str]:
    """A markdown table's rows (the header first) as aligned columns when they fit the terminal, else one block
    per row: its first cell, then each other non-empty cell as ``header: value``."""
    header, body = rows[0], rows[1:]
    widths = [max(len(r[i]) if i < len(r) else 0 for r in rows) for i in range(len(header))]
    if sum(widths) + 2 * (len(widths) - 1) <= columns():
        return ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    width = max(len(h) for h in header[1:]) + 1
    out: List[str] = []
    for r in body:
        out += ["", r[0]]
        for h, c in zip(header[1:], r[1:]):
            if c and c != "—":
                head = f"  {(h.lower() + ':').ljust(width)} "
                out.append(wrap(c, len(head), head))
    return out + [""]


def plain(markdown: str) -> str:
    """Markdown as plain text: no #, **, `, > or table rules; tables per _table(); long lines wrapped."""
    out: List[str] = []
    rows: List[List[str]] = []
    for line in markdown.splitlines() + [""]:
        if line.startswith("|"):
            cells = [_words(c) for c in _cells(line)]
            if "".join(cells).strip("-: "):  # not the |---| rule
                rows.append(cells)
            continue
        if rows:
            out += _table(rows)
            rows = []
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", re.sub(r"^#+\s*|^>\s*", "", line)).replace("`", "")
        if line.startswith("- "):
            out.append(wrap(line[2:], 4, "  - "))
        elif line:  # a note: flush left, and apart from the note above it when that one is a note too
            if out and out[-1] and not out[-1].startswith("  "):
                out.append("")
            out.append(wrap(line, 0))
        else:
            out.append("")
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")
    return text + "\n"


def show(markdown: str, stream: Optional[TextIO] = None) -> str:
    """What to print for *markdown*: itself for a pipe or a log, plain() for a terminal."""
    return plain(markdown) if for_terminal(stream) else markdown
