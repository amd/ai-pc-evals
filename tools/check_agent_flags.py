# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Check that the flags tools/agents/agents.json passes to each agent's CLI still exist in the installed CLI.

usage: python tools/check_agent_flags.py [--agents FILE] [--require-all] [--report FILE]

Third-party CLIs change quickly: OpenCode 2.x dropped --dir and --pure, and an entry still passing them failed
every task with "Unrecognized flag" (#44, #61). For each command-line entry this runs its command's --help (the
subcommand's, e.g. `opencode run --help`, and the top-level one) and checks that every flag in its "cmd" and
"web_flag" is listed as an option there. A flag only mentioned in another option's description doesn't count.

Exit 1 when a flag is missing, or, with --require-all, when a CLI isn't installed or its --help fails. Runs no
model and needs no key. CI runs it weekly and on pull requests that change tools/agents/
(.github/workflows/agent-clis.yml). Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402

AGENTS = Path(__file__).resolve().parent / "agents" / "agents.json"
HELP_TIMEOUT_S = 60
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
#: An option in a help line's left column: "-m, --model MODEL", "--no-skills, -ns", "--cwd <dir>".
OPTION = re.compile(r"(?<![\w-])(--?[A-Za-z0-9][\w-]*)")


def entry_command(cmd: Sequence[str]) -> List[str]:
    """The CLI and its subcommands: the leading words of *cmd* before any flag or placeholder."""
    words = [cmd[0]]
    for arg in cmd[1:]:
        if arg.startswith("-") or "{" in arg:
            break
        words.append(arg)
    return words


def entry_flags(spec: Dict) -> List[str]:
    """Every flag the entry passes, as named (``--timeout`` for ``--timeout=0``), in order, once each."""
    flags = []
    for arg in [*spec.get("cmd", [])[1:], *spec.get("web_flag", [])]:
        if arg.startswith("-") and arg.strip("-") and "{" not in arg:
            name = arg.split("=", 1)[0]
            if name not in flags:
                flags.append(name)
    return flags


def listed_options(help_text: str) -> set:
    """The options a --help output lists: those in each line's left column, before the gap to its description.
    "(server logs require --standalone)" in a description doesn't list --standalone."""
    found = set()
    for line in ANSI.sub("", help_text).splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        column = re.split(r"\s{2,}", stripped, maxsplit=1)[0]
        found.update(OPTION.findall(column))
    return found


def run_help(words: Sequence[str]) -> Optional[str]:
    """*words* --help's output (stdout and stderr: some CLIs print help on stderr), or None if it couldn't run."""
    exe = shutil.which(words[0])
    if not exe:
        return None
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "CI": "1"}
    try:
        done = subprocess.run([exe, *words[1:], "--help"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=HELP_TIMEOUT_S, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout or "") + "\n" + (done.stderr or "")


def version(cli: str) -> str:
    exe = shutil.which(cli)
    if not exe:
        return "not installed"
    try:
        done = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=HELP_TIMEOUT_S, stdin=subprocess.DEVNULL,
                              env={**os.environ, "NO_COLOR": "1"})
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    lines = [ln.strip() for ln in ANSI.sub("", (done.stdout or "") + "\n" + (done.stderr or "")).splitlines()
             if ln.strip()]
    # Some print a notice first (hermes --version checks for updates, which can hit a rate limit).
    versioned = [ln for ln in lines if re.search(r"\d+\.\d+", ln)]
    return (versioned or lines or ["unknown"])[0][:80]


def check(agents: Dict[str, Dict], require_all: bool) -> List[Dict]:
    """One row per command-line entry: its CLI, version, and the flags its CLI doesn't list (or why unchecked)."""
    rows = []
    for name, spec in agents.items():
        if name.startswith("_") or spec.get("manual") or not spec.get("cmd"):
            continue
        words = entry_command(spec["cmd"])
        flags = entry_flags(spec)
        row = {"agent": name, "command": " ".join(words), "version": version(words[0]), "flags": flags,
               "missing": [], "problem": ""}
        texts = [run_help(words)] + ([run_help(words[:1])] if len(words) > 1 else [])
        if texts[0] is None:
            row["problem"] = f"{words[0]} is not installed" if not shutil.which(words[0]) else \
                f"`{' '.join(words)} --help` did not run"
            row["failed"] = require_all
        else:
            listed = set().union(*(listed_options(t) for t in texts if t))
            row["missing"] = [f for f in flags if f not in listed]
            row["failed"] = bool(row["missing"])
        rows.append(row)
    return rows


def report(rows: Sequence[Dict]) -> str:
    lines = ["| Agent | Command | CLI version | Result |", "|---|---|---|---|"]
    for r in rows:
        if r["problem"]:
            result = ("**FAILED**: " if r["failed"] else "not checked: ") + r["problem"]
        elif r["missing"]:
            result = "**FAILED**: not in its --help: " + ", ".join(f"`{f}`" for f in r["missing"])
        else:
            result = f"ok ({len(r['flags'])} flags)"
        lines.append(f"| {r['agent']} | `{r['command']}` | {r['version']} | {result} |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agents", type=Path, default=AGENTS, help="the agents file (default: tools/agents/agents.json)")
    ap.add_argument("--require-all", action="store_true",
                    help="a CLI that isn't installed, or whose --help fails, is a failure (CI); else it is skipped")
    ap.add_argument("--report", type=Path, help="also write the table to this file (Markdown)")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    rows = check(json.loads(args.agents.read_text(encoding="utf-8")), args.require_all)
    text = report(rows)
    print(console.show(text), end="")  # plain text on a terminal; markdown for CI and pipes
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    return 1 if any(r["failed"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
