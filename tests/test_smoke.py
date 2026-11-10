"""tools/smoke.py's own logic. The smoke test itself (build, install, run) is the CI job "smoke", not a unit test."""
import json
import subprocess
import sys

import pytest

import smoke


def _copy(tmp_path):
    root = tmp_path / "ai-pc-evals-9.9.9"
    (root / "tools" / "agents").mkdir(parents=True)
    (root / "tools" / "agents" / "agents.json").write_text(json.dumps({"hermes": {"cmd": ["hermes"]}}), encoding="utf-8")
    return root


def test_stub_agent_goes_into_the_copy_only(tmp_path):
    repo_config = smoke.REPO / "tools" / "agents" / "agents.json"
    before = repo_config.read_bytes()
    root = _copy(tmp_path)
    smoke.add_stub_agent(root, tmp_path / "python", tmp_path / "stub.py", ["summary.md"])
    agents = json.loads((root / "tools" / "agents" / "agents.json").read_text(encoding="utf-8"))
    assert set(agents) == {"hermes", smoke.STUB}
    assert agents[smoke.STUB]["cmd"] == [str(tmp_path / "python"), str(tmp_path / "stub.py"), "{folder}", "summary.md"]
    assert repo_config.read_bytes() == before


@pytest.mark.parametrize("where", [lambda: smoke.REPO, lambda: smoke.REPO / "tools"])
def test_stub_agent_refuses_the_repo(where):
    before = (smoke.REPO / "tools" / "agents" / "agents.json").read_bytes()
    with pytest.raises(smoke.SmokeError, match="refusing"):
        smoke.add_stub_agent(where(), smoke.REPO / "python", smoke.REPO / "stub.py", ["x.md"])
    assert (smoke.REPO / "tools" / "agents" / "agents.json").read_bytes() == before


def test_eval_env_drops_every_kw_setting_and_uses_its_own_homes(tmp_path):
    base = {"PATH": "p", "KW_ANSWERS_PASSPHRASE": "secret", "KW_AGENT_USER": "agent", "kw_fixtures": "f",
            "HOME": "/real", "USERPROFILE": "C:/real"}
    env = smoke.eval_env(base, tmp_path)
    assert env["PATH"] == "p" and not any(k.upper().startswith("KW_") and k not in ("KW_EVAL_HOME", "KW_GRADED_HOME")
                                          for k in env)
    assert env["HOME"] == env["USERPROFILE"] == str(tmp_path / "home") and (tmp_path / "home").is_dir()
    assert env["KW_EVAL_HOME"] == str(tmp_path / "kw-eval") and env["KW_GRADED_HOME"] == str(tmp_path / "kw-graded")
    assert env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"


def test_stub_outputs_are_the_deliverables_or_its_own_file():
    assert smoke.stub_outputs({"deliverables": ["summary.md", "out/a.xlsx"]}) == ["summary.md", "out/a.xlsx"]
    assert smoke.stub_outputs({"deliverables": []}) == [smoke.STUB_OUTPUT]


def test_stub_script_writes_its_files_and_prints_the_answer(tmp_path):
    script = tmp_path / "stub.py"
    script.write_text(smoke.STUB_SCRIPT, encoding="utf-8")
    out = subprocess.run([sys.executable, str(script), str(tmp_path), "summary.md", "out/b.md"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == smoke.STUB_ANSWER
    assert (tmp_path / "summary.md").is_file() and (tmp_path / "out" / "b.md").is_file()


def _run(tmp_path, answer=smoke.STUB_ANSWER, error="", outputs=("summary.md",)):
    run_dir = tmp_path / "run"
    (run_dir / ".records" / "t1").mkdir(parents=True)
    (run_dir / "t1").mkdir()
    (run_dir / "kw-run.json").write_text(json.dumps({"tasks": {"t1": {"folder": str(run_dir / "t1")}}}), encoding="utf-8")
    (run_dir / ".records" / "t1" / "result.json").write_text(json.dumps({"answer": answer, "error": error}),
                                                              encoding="utf-8")
    for name in outputs:
        (run_dir / "t1" / name).write_text("x", encoding="utf-8")
    return run_dir


def test_check_run_passes_a_complete_run(tmp_path):
    assert smoke.check_run(_run(tmp_path), "t1", ["summary.md"]) == []


@pytest.mark.parametrize("kwargs, expect", [
    ({"outputs": ()}, "summary.md is not in the task folder"),
    ({"error": "stub.py exited 1"}, "records an error: stub.py exited 1"),
    ({"answer": ""}, "answer is not the stub's"),
])
def test_check_run_reports_what_is_wrong(tmp_path, kwargs, expect):
    problems = smoke.check_run(_run(tmp_path, **kwargs), "t1", ["summary.md"])
    assert len(problems) == 1 and expect in problems[0]


def test_check_run_without_records(tmp_path):
    run_dir = _run(tmp_path)
    (run_dir / ".records" / "t1" / "result.json").unlink()
    assert smoke.check_run(run_dir, "t1", ["summary.md"]) == ["no .records/t1/result.json"]
    (run_dir / "kw-run.json").unlink()
    assert smoke.check_run(run_dir, "t1", ["summary.md"]) == [f"no kw-run.json in {run_dir}"]
    assert smoke.check_run(_run(tmp_path / "b"), "t2", []) == ["kw-run.json does not list t2"]
