"""Extract the agent's final answer from a copied Perplexity thread (stdin) to stdout.

usage: python thread_answer.py [--prompt-file FILE] < thread.txt

A copied thread is the prompt, then the agent's turn, then the app's chrome:

    <prompt>
    Local agent: <model>        (local mode only)
    2m 51s                      (the agent's time; local mode only)
    <answer>
    Sources / Usage             (only when the agent cited or the app reports usage)

The rule, applied to the last turn in the thread:

1. Start: the answer begins after the last *anchor* line outside a Sources/Usage section.
   Anchors are, in order of where they appear: the last line of the prompt (when
   --prompt-file gives it; cloud copies need it), an agent header ('Local agent: ...',
   'Cloud agent: ...'), and the agent's time ('45s', '2m 51s', '1h 2m 3s', optionally
   after 'Finished'/'Completed'/'Worked for'). Agent headers, time lines and step counts
   ('12 steps', 'Completed 12 steps') right after the anchor are skipped too.
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
AGENT_LINE = re.compile(r"^(?:Local|Cloud) agent\b.*$", re.I)
STEP_LINE = re.compile(r"^(?:(?:Finished|Completed|Ran) )?\d+ steps?(?: completed)?$", re.I)
SECTION_LINE = re.compile(r"^(?:Sources|Usage)$")
CHROME_LINE = re.compile(r"^(?:Share|Export|Rewrite|Copy)$")


class NoAnswer(ValueError):
    pass


def _is_preamble(line: str) -> bool:
    return bool(TIME_LINE.match(line) or AGENT_LINE.match(line) or STEP_LINE.match(line))


def extract(text: str, prompt: str | None = None) -> str:
    """The final answer in a copied thread; raises NoAnswer when it can't be found."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    stripped = [ln.strip() for ln in lines]
    prompt_lines = [ln.strip() for ln in (prompt or "").replace("\r\n", "\n").split("\n") if ln.strip()]
    prompt_last = prompt_lines[-1] if prompt_lines else None

    # Anchors outside Sources/Usage sections: a section runs until the next prompt or agent header.
    anchor = None
    in_section = False
    for i, s in enumerate(stripped):
        if SECTION_LINE.match(s):
            in_section = True
        elif (prompt_last and s == prompt_last) or AGENT_LINE.match(s):
            in_section = False
            anchor = i
        elif not in_section and TIME_LINE.match(s):
            anchor = i
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
