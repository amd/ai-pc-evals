# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Console output: plain text for a person at a terminal, markdown for pipes, logs and CI."""
import io

import console

MD = ("### Table 1 — Results\n\n| Use case | Agent A | Agent B |\n|---|---|---|\n"
      "| Research (2) | 1/2 · Q 4.0 | ⏳ |\n| **All** (2) | 1/2 · Q 4.0 | — |\n\n"
      "> ⚠️ graded by different judges\n\n- **Not run**: kw-02\n")


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_a_pipe_gets_the_markdown_a_terminal_gets_plain_text(monkeypatch):
    monkeypatch.delenv("KW_MARKDOWN", raising=False)
    assert console.show(MD, io.StringIO()) == MD
    assert console.show(MD, Tty()) == console.plain(MD)
    monkeypatch.setenv("KW_MARKDOWN", "1")
    assert console.show(MD, Tty()) == MD


def test_a_table_that_fits_is_aligned_columns_without_rules(monkeypatch):
    monkeypatch.setattr(console, "columns", lambda: 100)
    assert console.plain(MD) == (
        "Table 1 — Results\n\n"
        "Use case      Agent A      Agent B\n"
        "Research (2)  1/2 · Q 4.0  not run\n"
        "All (2)       1/2 · Q 4.0  —\n\n"
        "⚠️ graded by different judges\n\n"
        "  - Not run: kw-02\n")


def test_a_table_too_wide_for_the_window_is_one_block_per_row(monkeypatch):
    monkeypatch.setattr(console, "columns", lambda: 20)
    text = console.plain(MD)
    assert "Research (2)\n  agent a: 1/2 · Q 4.0\n  agent b: not run\n" in text
    assert "All (2)\n  agent a: 1/2 · Q 4.0\n\n" in text  # an empty cell (—) is left out
    assert "⚠️ graded by different judges" in text  # a mark becomes a word only in a table cell


def test_a_long_value_wraps_under_itself_on_a_terminal_only(monkeypatch):
    long = "the agent runs as you: code it runs can read your files. Fine for a shakedown only."
    monkeypatch.setattr(console, "columns", lambda: 50)
    monkeypatch.setattr(console, "for_terminal", lambda stream=None: True)
    lines = console.kv("warning", long, indent="").splitlines()
    assert len(lines) > 1 and lines[0].startswith("warning:    the agent")
    assert all(line.startswith(" " * 12) and len(line) <= 50 for line in lines[1:])
    monkeypatch.setattr(console, "for_terminal", lambda stream=None: False)
    assert console.kv("warning", long, indent="") == "warning:    " + long  # a log keeps it whole, for grep


def test_the_summary_says_in_one_line_when_the_tasks_changed_since_the_run():
    import run_eval

    md = ("# pi / q on everyday\n\n**Ran with tasks 63e84f17ed5d but graded with tasks d1652cf1fa16**: the probes, "
          "keys or prompts changed in between, so compare these scores only with runs graded the same way.\n")
    assert "tasks:      ran with 63e84f17ed5d, graded with d1652cf1fa16: the tasks changed" in run_eval.plain(md)
