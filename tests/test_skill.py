# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""The Claude Code skill that ships in the release: well-formed, its references and commands real, no secrets."""
import functools
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import evalversion
import package

REPO = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO / ".claude" / "skills" / "ai-pc-evals"
SKILL = SKILL_DIR / "SKILL.md"
SKILL_FILES = sorted(SKILL_DIR.rglob("*.md"))
#: Everything in the skill folder, whatever its type: all of it must ship.
ALL_SKILL_FILES = sorted(p for p in SKILL_DIR.rglob("*") if p.is_file())
#: The docs whose commands are checked like the skill's: every flag a doc shows must be one the tool takes.
DOCS = [REPO / name for name in ("README.md", "CONTRIBUTING.md", "CLAUDE.md", "USE_CASES.md", "secrets/README.md",
                                 "knowledgework/SCHEMA.md", "scoreboard/README.md")] + sorted((REPO / "docs").glob("*.md"))


def _frontmatter(text: str) -> dict:
    assert text.startswith("---\n"), "SKILL.md must start with a YAML frontmatter block"
    head, sep, _ = text[4:].partition("\n---\n")
    assert sep, "the frontmatter block is not closed with ---"
    fields = {}
    for line in head.splitlines():
        key, colon, value = line.partition(":")
        assert colon and re.fullmatch(r"[a-z][a-z0-9-]*", key), f"not a 'key: value' line: {line!r}"
        value = value.strip()
        # A plain YAML scalar: no ': ' or ' #' inside, no leading indicator character.
        assert ": " not in value and " #" not in value and value[:1] not in "&*!|>'\"%@`[]{},#?-", line
        fields[key] = value
    try:
        import yaml  # noqa: PLC0415 - optional: not in requirements.txt
    except ImportError:
        return fields
    assert yaml.safe_load(head) == fields
    return fields


def test_skill_has_valid_frontmatter():
    fields = _frontmatter(SKILL.read_text(encoding="utf-8"))
    assert fields["name"] == SKILL_DIR.name == "ai-pc-evals"
    assert 50 < len(fields["description"]) <= 1024
    assert len(SKILL.read_text(encoding="utf-8").splitlines()) <= 300  # long material goes in the supporting files


def test_every_supporting_file_is_referenced_and_every_reference_exists():
    text = SKILL.read_text(encoding="utf-8")
    links = set(re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text))
    for f in SKILL_FILES:
        if f != SKILL:
            assert f.name in links, f"{f.name} is not referenced from SKILL.md, so it never loads"
    for f in SKILL_FILES:
        for link in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", f.read_text(encoding="utf-8")):
            if "://" not in link:
                assert (f.parent / link).is_file(), f"{f.name} links to a missing file: {link}"


REPO_PATH = re.compile(r"(?<![\w/.~$-])((?:docs|secrets|tools|knowledgework|scoreboard)/[\w./-]+\.(?:md|py|sh|json)"
                       r"|(?:CLAUDE|README|CHANGELOG|USE_CASES)\.md|requirements\.txt)")


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_repo_files_the_skill_names_exist(path):
    text = path.read_text(encoding="utf-8")
    named = set(REPO_PATH.findall(text))
    assert named
    # A scoreboard file the operator writes may not exist yet; everything else must.
    missing = [n for n in named if not (REPO / n).exists() and not n.startswith("scoreboard/")]
    assert not missing, missing
    for script in set(re.findall(r"\b([\w-]+\.sh)\b", text)):
        assert any((REPO / d / script).is_file() for d in ("tools", "tools/perplexity")), script


def _chunks(text: str):
    """Code a reader would run: fenced lines (continuations joined) and inline code spans."""
    fenced = re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S)
    for block in fenced:
        yield from block.replace("\\\n", " ").splitlines()
    yield from re.findall(r"`([^`\n]+)`", re.sub(r"```.*?```", "", text, flags=re.S))


COMMAND = re.compile(r"python (-m kwengine|(?:tools|knowledgework)/[\w/]+\.py)")
FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")


def _commands():
    """(file, script, args) for every `python tools/x.py ...`, `python knowledgework/x.py ...` and
    `python -m kwengine ...` in the skill and in the docs a newcomer follows (DOCS: the README, docs/, ...)."""
    out = []
    for path in [*SKILL_FILES, *DOCS]:
        for chunk in _chunks(path.read_text(encoding="utf-8")):
            starts = [m for m in COMMAND.finditer(chunk)]
            for i, m in enumerate(starts):
                end = starts[i + 1].start() if i + 1 < len(starts) else len(chunk)
                seg = re.split(r"\s#|;|&&|\||\)|`", chunk[m.end():end])[0]
                out.append((path.name, m.group(1), seg))
    return out


COMMANDS = _commands()


@functools.lru_cache(maxsize=None)
def _help(*argv: str) -> str:
    done = subprocess.run([sys.executable, *argv, "--help"], cwd=REPO, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    assert done.returncode == 0, f"{argv} --help failed: {done.stderr[-500:]}"
    return done.stdout


def _choices(help_text: str) -> list:
    m = re.search(r"\{([\w,-]+)\}", help_text)
    return m.group(1).split(",") if m else []


def test_the_skill_has_commands_to_check():
    scripts = {s for _, s, _ in COMMANDS}
    assert {"tools/run_eval.py", "tools/results_tables.py", "tools/keys.py", "-m kwengine"} <= scripts
    assert {"app-operator.md", "gaia.md", "perplexity.md"} <= {f for f, _, _ in COMMANDS}


@pytest.mark.parametrize("where,script,args", COMMANDS, ids=[f"{f}:{s}{a[:40]}" for f, s, a in COMMANDS])
def test_every_command_and_flag_in_the_skill_exists(where, script, args):
    words = args.split()
    flags = set(FLAG.findall(args))
    if script == "-m kwengine":
        actions = _choices(_help("-m", "kwengine"))
        assert words and words[0] in actions, f"{where}: python -m kwengine {words[:1]}: not one of {actions}"
        known = _help("-m", "kwengine", words[0])
    else:
        assert (REPO / script).is_file(), f"{where}: {script} does not exist"
        source = (REPO / script).read_text(encoding="utf-8")
        if "argparse" not in source:
            # never run a script without argparse with --help: it may take that as an argument. Its usage line in
            # the docstring names the flags it reads from sys.argv.
            usage = re.search(r"usage: python \S+(.*)", source)
            for flag in flags:
                assert usage and re.search(re.escape(flag) + r"(?![\w-])", usage.group(1)), \
                    f"{where}: {script} takes no {flag}"
            return
        has_sub = "add_subparsers" in source
        if not flags and not has_sub:
            return
        known = _help(script)
        if has_sub:
            sub = _choices(known)
            assert words and words[0] in sub, f"{where}: {script} {words[:1]}: not one of {sub}"
            known += _help(script, words[0])
    for flag in flags:
        assert re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", known), \
            f"{where}: {script} has no {flag}"


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_the_skill_never_holds_or_fetches_the_passphrase(path):
    text = path.read_text(encoding="utf-8")
    # Clearing it (`set KW_ANSWERS_PASSPHRASE=` with nothing after) is fine; giving it a value is not.
    for var in ("KW_ANSWERS_PASSPHRASE", "KW_HELDOUT_PASSPHRASE"):
        assert not re.search(var + r"\s*=\s*[^\s`'\"]", text), "a passphrase assignment"
        assert not re.search(r"\$env:" + var + r"\s*=", text), "a passphrase assignment"
    assert not re.search(r"keyring\s+get\b|get_password|Get-StoredCredential|cmdkey\s+/list", text, flags=re.I)
    assert not re.search(r"python tools/keys\.py (?:decrypt|publish)", text)
    assert not re.search(r"(?i)\b(?:give|send|tell|paste|share|show)\s+(?:me|us|claude|the agent)\b[^.\n]*passphrase", text)
    assert not re.search(r"(?i)ask (?:the person|them|the user) for (?:the|their) passphrase", text)


def test_the_release_zip_ships_the_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(evalversion, "version", lambda repo=None: {"framework": "9.9.9", "tasks": "abc", "commit": "c", "dirty": False})
    files = [f for f in package.tracked_files() if (package.REPO / f).is_file()]
    assert ".claude/skills/ai-pc-evals/SKILL.md" in files, "the skill must be committed (see .gitignore)"
    names = {n.split("/", 1)[1] for n in zipfile.ZipFile(package.build(tmp_path, "", files=files)).namelist()}
    assert len(ALL_SKILL_FILES) >= 6
    for f in ALL_SKILL_FILES:
        assert f.relative_to(REPO).as_posix() in names, f"{f.name} is not in the release zip"
    assert not any(n.startswith(".claude/") and not n.startswith(".claude/skills/") for n in names)


def test_the_skill_is_not_part_of_the_task_fingerprint():
    assert not any(rel.startswith(".claude") for rel in evalversion.FINGERPRINTED)


# ---------------------------------------------------------------------------
# The skill against the code it describes: agent names, and what the runner asks and the operator types
# ---------------------------------------------------------------------------


def test_every_agent_the_skill_names_exists():
    from agents import available  # noqa: PLC0415

    agents = available()
    named = {name for path in SKILL_FILES
             for name in re.findall(r"--agent\s+([a-z][a-z0-9-]*)", path.read_text(encoding="utf-8"))}
    assert {"claude-code", "perplexity-computer", "gaia-app"} <= named
    assert not named - set(agents), f"agents the skill names but agents.json lacks: {sorted(named - set(agents))}"
    for name in ("perplexity-computer", "gaia-app"):  # the apps the operator pages are about are manual agents
        from agents import get_agent  # noqa: PLC0415

        assert get_agent(name, "m").manual, name


def _runner_dialogue(monkeypatch, tmp_path, capsys) -> str:
    """Everything the runner asks or prints to the operator of a manual agent, from scripted runs of the real code:
    the version, the picker (and Enter keeping it), the staged prompt's steps, step 4, queue and tokens, the
    clipboard's y/N, the empty answer file asked again, stalled and skip; then run_eval's connector y/N and the
    quit-before-grading prompt. Each reply is checked to do what the skill says it does."""
    import builtins  # noqa: PLC0415

    import run_eval  # noqa: PLC0415
    from agents import TaskSpec, get_agent  # noqa: PLC0415
    from agents import manual as m  # noqa: PLC0415

    asked = []
    replies = []
    agent = get_agent("gaia-app", "m", ask=lambda q: asked.append(q) or replies.pop(0), clock=lambda: 0.0)
    clip = {"text": "", "staged": True}
    monkeypatch.setattr(m, "set_clipboard", lambda text: clip["staged"])
    monkeypatch.setattr(m, "get_clipboard", lambda: clip["text"])
    monkeypatch.setattr(m, "engine_build", lambda: "")
    spec = TaskSpec(id="t1", prompt="Do the task.", folder=tmp_path / "task", timeout_s=300, needs=["web"])

    # 1. Prompt on the clipboard, answer copied there: the operator says y.
    replies[:] = ["0.25.0", "local", "40s", "queue 2m 10s, tokens 48210", "y"]
    clip["text"] = "The agent's answer."
    r = agent.run(spec, tmp_path / "log1")
    assert r.answer == "The agent's answer." and r.extra["run_info"]["outcome"] == "finished"
    assert r.extra["run_info"]["queue_seconds"] == 130 and r.extra["run_info"]["local_tokens"] == 48210
    assert r.wall_seconds == 0.0 and not replies  # 40 s of app time, less the 130 s queue
    # 2. Enter keeps the last picker; `stalled`.
    replies[:] = ["", "stalled", ""]
    clip["text"] = ""
    r = agent.run(spec, tmp_path / "log2")
    assert r.extra["run_info"]["outcome"] == "stalled-asked-human" and r.extra["run_info"]["mode"] == "local"
    # 3. Nothing staged: text typed at the empty-file prompt is set aside until a bare Enter.
    clip["staged"] = False
    replies[:] = ["", "", "", "pasted here by mistake", ""]
    r = agent.run(spec, tmp_path / "log3")
    assert r.answer == "" and not replies
    # 4. `skip`: did not run, and nothing more is asked.
    replies[:] = ["", "skip"]
    r = agent.run(spec, tmp_path / "log4")
    assert r.extra["run_info"]["outcome"] == "did-not-run" and not replies

    # run_eval's own questions to the operator of a manual agent.
    monkeypatch.setattr(run_eval.sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("KW_CONNECTED", raising=False)
    answers = iter(["y", "", ""])
    monkeypatch.setattr(builtins, "input", lambda q="": asked.append(q) or next(answers))
    assert run_eval.connectors_confirmed(agent, None, ["pc-09-drive-budget-copy"]) == (True, "")
    assert run_eval.connectors_confirmed(agent, None, ["pc-09-drive-budget-copy"])[0] is False  # Enter is no
    assert run_eval.app_closed(run_eval.app_name(agent))
    monkeypatch.setattr(run_eval.sys.stdin, "isatty", lambda: False)
    assert not run_eval.app_closed("GAIA")
    return "\n".join(asked) + "\n" + capsys.readouterr().out


#: Runner text the skill quotes outside the prompts (printed lines).
PRINTED = ("This task needs the web.", "or copy it to the clipboard", "not grading: no one to confirm")
#: A quoted span is runner text the operator answers when it holds one of these.
PROMPT_MARKS = ("[y/N]", "press Enter", "Press Enter", "Enter keeps", "Enter to skip", "is empty: save",
                "Check the model picker")


def _quoted_prompts():
    for path in SKILL_FILES:
        for span in re.findall(r"`([^`\n]+)`", path.read_text(encoding="utf-8")):
            if any(mark in span for mark in PROMPT_MARKS) and " " in span:
                yield path.name, span


def _pattern(span: str) -> str:
    """A quoted prompt as a regex: '...' and <placeholders> stand for any text."""
    parts = re.split(r"\s*\.\.\.\s*|<[^>]+>", span)
    return ".*?".join(re.escape(p) for p in parts)


def test_what_the_skill_quotes_and_tells_the_operator_to_type_matches_the_runner(monkeypatch, tmp_path, capsys):
    dialogue = _runner_dialogue(monkeypatch, tmp_path, capsys)
    quoted = list(_quoted_prompts())
    # The prompts the operator pages walk through, in the runner's words.
    for must in ("The app's version, from its About page (Enter to skip)", "Use this as the agent's answer? [y/N]",
                 "Is one connected? [y/N]"):
        assert any(must in span for _, span in quoted), f"the skill no longer quotes {must!r}"
    for where, span in quoted:
        assert re.search(_pattern(span), dialogue, flags=re.S), f"{where} quotes a prompt the runner doesn't ask: {span!r}"
    operator = (SKILL_DIR / "app-operator.md").read_text(encoding="utf-8")
    for line in PRINTED:
        assert line in dialogue, f"the runner no longer prints {line!r}"
        assert line.split("(")[0] in "".join(p.read_text(encoding="utf-8") for p in SKILL_FILES), line
    # The words the operator types at step 4 are the ones the step-4 prompt offers, and the ones the code reads.
    step4 = next(q for q in dialogue.splitlines() if q.startswith("4. Press Enter"))
    for word in ("stalled", "skip"):
        assert f"'{word}'" in step4 and f"`{word}`" in operator, word
    assert "`queue 2m 10s, tokens 48210`" in operator and "'queue 2m 10s, tokens 48210'" in dialogue
