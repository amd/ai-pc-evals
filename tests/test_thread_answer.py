"""tools/perplexity/thread_answer.py: the final answer from a copied Perplexity thread."""
import io
import subprocess
import sys
from pathlib import Path

import pytest

import thread_answer as ta

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "perplexity" / "thread_answer.py"
PROMPT = "Summarise the attached report.\nSave it as summary.docx in the task folder."

LOCAL = """Summarise the attached report.
Save it as summary.docx in the task folder.

Local agent: qwen3-32b

2m 51s

The report finds **three** risks.

- Cost overrun
- Delay

Sources

1 report.pdf

Usage
104 credits
"""

CLOUD = """Summarise the attached report.
Save it as summary.docx in the task folder.

The report finds **three** risks.

- Cost overrun
- Delay

Sources

1 report.pdf
"""

SHORT_LOCAL = """Summarise the attached report.
Save it as summary.docx in the task folder.

Local agent: qwen3-32b

45s

The report finds **three** risks.

- Cost overrun
- Delay

Share
Export
"""

ANSWER = "The report finds **three** risks.\n\n- Cost overrun\n- Delay"


def test_time_line_and_sources():
    assert ta.extract(LOCAL) == ANSWER
    assert ta.extract(LOCAL.replace("\n", "\r\n"), PROMPT) == ANSWER


def test_cloud_thread_without_time_line_starts_after_the_prompt():
    assert ta.extract(CLOUD, PROMPT) == ANSWER


def test_cloud_thread_skips_step_and_finished_lines():
    thread = CLOUD.replace("folder.\n\n", "folder.\n\nCompleted 12 steps\nFinished in 3m 4s\n\n", 1)
    assert ta.extract(thread, PROMPT) == ANSWER


def test_no_sources_block_runs_to_the_end_minus_buttons():
    assert ta.extract(SHORT_LOCAL) == ANSWER
    assert ta.extract(CLOUD.split("Sources")[0], PROMPT) == ANSWER


def test_last_turn_wins_and_usage_times_are_ignored():
    thread = LOCAL.replace("104 credits", "4m 2s\n104 credits") + "\nNow shorten it.\n\nLocal agent: qwen3-32b\n\n9s\n\nShorter.\n"
    assert ta.extract(thread) == "Shorter."


def test_fails_loudly_when_there_is_no_answer():
    with pytest.raises(ta.NoAnswer, match="--prompt-file"):
        ta.extract(CLOUD)  # cloud thread, no prompt given: nothing marks where the answer starts
    with pytest.raises(ta.NoAnswer, match="no answer"):
        ta.extract(PROMPT + "\n\nLocal agent: qwen3-32b\n\n12s\n\nSources\n\n1 a.pdf\n")
    with pytest.raises(ta.NoAnswer):
        ta.extract("", PROMPT)
    with pytest.raises(ta.NoAnswer, match="no answer"):
        ta.extract(PROMPT + "\n", PROMPT)  # the clipboard still holds only the prompt


@pytest.mark.parametrize("thread, code", [(CLOUD, 0), ("just some text\nno markers\n", 1)])
def test_cli_exit_codes(tmp_path, thread, code):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(PROMPT, encoding="utf-8")
    out = subprocess.run([sys.executable, str(SCRIPT), "--prompt-file", str(prompt)], input=thread.encode("utf-8"),
                         capture_output=True)
    assert out.returncode == code
    if code:
        assert out.stdout == b"" and b"ERROR:" in out.stderr
    else:
        assert out.stdout.decode("utf-8").replace("\r\n", "\n").strip() == ANSWER


def test_main_reports_error_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"nothing here\n"), encoding="utf-8"))
    assert ta.main([]) == 1
    assert "ERROR: found no agent time" in capsys.readouterr().err
