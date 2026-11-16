# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""tools/check_agent_flags.py (#61): every flag an agents.json entry passes must be an option its CLI lists. The
help excerpts below are in each CLI's real format (OpenCode 2.2.7, argparse as Hermes 0.23.0, Pi 1.5.2)."""
import json

import pytest

import check_agent_flags as caf

OPENCODE_RUN = """FLAGS
  --continue, -c            Continue the last session
  --model, -m string        Model to use in the format provider/model#variant

GLOBAL FLAGS
  --print-logs              Print logs to stderr (server logs require --standalone)
"""
OPENCODE_TOP = """GLOBAL FLAGS
  --help, -h                Show help information
  --standalone              Run with a private server instead of the background service
"""
HERMES_CHAT = """options:
  -Q, --quiet           Quiet mode for programmatic use: suppress banner,
  --query-file PATH     Read the single query from a file instead of the
                        command line (- for stdin); see also -Q/--quiet.
  -m, --model MODEL     Model to use (e.g., anthropic/claude-sonnet-4)
"""
PI = """Options:
  --model <pattern>              Model pattern or ID
  --no-skills, -ns               Disable skills discovery and loading
"""


def test_the_command_is_the_cli_and_its_subcommands_and_the_flags_are_named_once():
    spec = {"cmd": ["openclaw", "agent", "exec", "--message-file", "{prompt_file}", "--timeout=0", "--timeout", "0"],
            "web_flag": ["--web"]}
    assert caf.entry_command(spec["cmd"]) == ["openclaw", "agent", "exec"]
    assert caf.entry_flags(spec) == ["--message-file", "--timeout", "--web"]
    assert caf.entry_command(["pi", "-p", "{prompt}"]) == ["pi"]


@pytest.mark.parametrize("text, listed, not_listed", [
    (OPENCODE_RUN, {"--model", "-m", "--continue", "-c", "--print-logs"}, {"--standalone"}),  # prose isn't a listing
    (HERMES_CHAT, {"-Q", "--quiet", "--query-file", "-m", "--model"}, set()),
    (PI, {"--model", "--no-skills", "-ns"}, set()),
    ("\x1b[1m  --model\x1b[0m, -m   Model\n", {"--model", "-m"}, set()),  # colour codes
])
def test_only_options_in_a_help_lines_left_column_count(text, listed, not_listed):
    found = caf.listed_options(text)
    assert listed <= found and not (not_listed & found)


def fake_clis(monkeypatch, helps, installed=None):
    """--help outputs by command (a tuple of words); None: it couldn't run."""
    monkeypatch.setattr(caf, "run_help", lambda words: helps.get(tuple(words)))
    names = {w[0] for w in helps} if installed is None else installed
    monkeypatch.setattr(caf.shutil, "which", lambda name: f"/bin/{name}" if name in names else None)
    monkeypatch.setattr(caf, "version", lambda cli: "1.0")


def test_44s_opencode_entry_fails_and_the_current_one_passes(monkeypatch):
    """The issue's "done when": --dir and --pure, which OpenCode 2.x rejects, are reported; a global flag listed
    only in the top-level help (--standalone) is found there."""
    fake_clis(monkeypatch, {("opencode", "run"): OPENCODE_RUN, ("opencode",): OPENCODE_TOP})
    old = {"opencode": {"cmd": ["opencode", "run", "--dir", "{folder}", "--pure", "--model", "{model}"]}}
    new = {"opencode": {"cmd": ["opencode", "run", "--standalone", "--model", "{model}"]}}
    (row,) = caf.check(old, require_all=True)
    assert row["failed"] and row["missing"] == ["--dir", "--pure"]
    (row,) = caf.check(new, require_all=True)
    assert not row["failed"] and row["missing"] == []


def test_a_cli_that_isnt_installed_fails_only_with_require_all(monkeypatch):
    fake_clis(monkeypatch, {}, installed=set())
    agents = {"pi": {"cmd": ["pi", "--model", "{model}"]}, "perplexity-computer": {"manual": True},
              "_about": "x"}
    (row,) = caf.check(agents, require_all=False)  # manual agents and notes are not CLIs
    assert not row["failed"] and "not installed" in row["problem"]
    (row,) = caf.check(agents, require_all=True)
    assert row["failed"]
    assert "**FAILED**: pi is not installed" in caf.report([row])


def test_the_exit_code_and_the_report_file(monkeypatch, tmp_path, capsys):
    fake_clis(monkeypatch, {("pi",): PI})
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({"pi": {"cmd": ["pi", "--model", "{model}", "--no-skills"]}}), encoding="utf-8")
    out = tmp_path / "report.md"
    assert caf.main(["--agents", str(agents), "--report", str(out)]) == 0
    assert "ok (2 flags)" in out.read_text(encoding="utf-8") and "| pi |" in capsys.readouterr().out
    agents.write_text(json.dumps({"pi": {"cmd": ["pi", "--no-such-flag"]}}), encoding="utf-8")
    assert caf.main(["--agents", str(agents)]) == 1
    assert "`--no-such-flag`" in capsys.readouterr().out


def test_every_shipped_entry_is_checkable():
    """Each command-line entry in agents.json yields a command and at least one flag to check."""
    agents = json.loads(caf.AGENTS.read_text(encoding="utf-8"))
    for name, spec in agents.items():
        if name.startswith("_") or spec.get("manual"):
            continue
        assert caf.entry_command(spec["cmd"])[0] == spec["cmd"][0] and caf.entry_flags(spec), name
