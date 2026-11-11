# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Extract the agent's final answer from a copied Perplexity thread (stdin) to stdout.

usage: python thread_answer.py [--prompt-file FILE] < thread.txt

A copied thread is the prompt, then the agent's turn, then the app's chrome:

    <prompt>
    Local agent: <model>        (local mode only)
    3m 00s                      (the agent's time; local mode only)
    <answer>
    Sources / Usage             (only when the agent cited or the app reports usage)

The rule, applied to the last turn in the thread:

1. Start: the last turn begins at its agent header ('Local agent: <model>', 'Cloud agent:
   <model>': a short line with a colon and no sentence punctuation, followed by the agent's
   time or step count; an answer line that merely starts with those words is not one);
   with none, at the end of the last place the whole prompt appears, every non-blank line in
   order (when --prompt-file gives it; cloud copies need it); with neither, at the first
   agent time ('45s', '3m 00s', '1h 2m 3s', optionally after 'Finished'/'Completed'/'Worked
   for') outside a Sources/Usage section. Agent headers, time lines and step counts ('12
   steps', 'Completed 12 steps') right after it are skipped, and the answer begins there. A
   line inside the answer (the prompt's last line repeated, a bare '45s') never moves the
   start.
2. End: the first standalone 'Sources' or 'Usage' line after the start, or the end of the
   text when there is none (short local threads). Trailing app buttons (Share, Export,
   Rewrite, Copy) and time or step lines are dropped.
3. No anchor, or nothing between the start and the end, is an error (exit 1, a message on
   stderr, nothing on stdout): the whole thread is never returned as the answer.
"""
from __future__ import annotations

import argparse
import re
import sys

_DURATION = r"(?:\d+h ?)?(?:\d+m ?)?\d+s"
TIME_LINE = re.compile(rf"^(?:(?:Finished|Completed|Worked)(?: in| for)? )?{_DURATION}$", re.I)
#: The app's agent header: "Local agent: <model>", short, a model name rather than a sentence (no ". ", "?", "!",
#: ";", no trailing period). An answer line such as "Cloud agent mode adds latency." is not one.
AGENT_LINE = re.compile(r"^(?:Local|Cloud) agent: ?(?!.*(?:[.!?;]\s|[.!?;]$))\S.{0,60}$", re.I)
STEP_LINE = re.compile(r"^(?:(?:Finished|Completed|Ran) )?\d+ steps?(?: completed)?$", re.I)
SECTION_LINE = re.compile(r"^(?:Sources|Usage)$")
CHROME_LINE = re.compile(r"^(?:Share|Export|Rewrite|Copy)$")


class NoAnswer(ValueError):
    pass


def _is_preamble(line: str) -> bool:
    return bool(TIME_LINE.match(line) or AGENT_LINE.match(line) or STEP_LINE.match(line))


def _opens_turn(stripped: list, i: int) -> bool:
    """An agent header opens its turn: the agent's time or step count comes next, before the answer body."""
    nxt = next((s for s in stripped[i + 1:] if s), "")
    return bool(TIME_LINE.match(nxt) or STEP_LINE.match(nxt))


def extract(text: str, prompt: str | None = None) -> str:
    """The final answer in a copied thread; raises NoAnswer when it can't be found."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    stripped = [ln.strip() for ln in lines]
    prompt_lines = [ln.strip() for ln in (prompt or "").replace("\r\n", "\n").split("\n") if ln.strip()]
    prompt_last = prompt_lines[-1] if prompt_lines else None

    # Where the last turn starts: its agent header, else the end of the whole prompt (every non-blank line of it,
    # in order), else the first time line outside a Sources/Usage section. Never a line inside the answer: an
    # answer may repeat the prompt's last line or hold a line such as "45s".
    anchor = None
    for i, s in enumerate(stripped):
        if AGENT_LINE.match(s) and _opens_turn(stripped, i):
            anchor = i
    if anchor is None and prompt_lines:
        filled = [i for i, s in enumerate(stripped) if s]
        n = len(prompt_lines)
        for k in range(len(filled) - n, -1, -1):
            if [stripped[i] for i in filled[k:k + n]] == prompt_lines:
                anchor = filled[k + n - 1]
                break
    if anchor is None:
        in_section = False
        for i, s in enumerate(stripped):
            if SECTION_LINE.match(s):
                in_section = True
            elif not in_section and TIME_LINE.match(s):
                anchor = i
                break
    if anchor is None:
        hint = "" if prompt_last else "; for a cloud thread pass --prompt-file"
        raise NoAnswer("found no agent time, agent line or prompt in the copied thread" + hint)

    start = anchor + 1
    while start < len(lines) and (not stripped[start] or _is_preamble(stripped[start])):
        start += 1
    end = next((i for i in range(start, len(lines)) if SECTION_LINE.match(stripped[i])), len(lines))
    while end > start and (not stripped[end - 1] or CHROME_LINE.match(stripped[end - 1])
                           or _is_preamble(stripped[end - 1])):
        end -= 1

    answer = "\n".join(lines[start:end]).strip()
    if not answer:
        raise NoAnswer("the copied thread has no answer after the agent's time or the prompt; "
                       "copy the whole thread again once the agent has finished")
    return answer


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prompt-file", help="the prompt the agent was given (needed for cloud threads)")
    args = ap.parse_args(argv)
    prompt = None
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as f:
            prompt = f.read()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        print(extract(sys.stdin.read(), prompt))
    except NoAnswer as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
