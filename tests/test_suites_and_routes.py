"""Runners find a task's suite instead of assuming one; tables label hybrid runs."""
import json

import pytest

import run_task
import suite_of
import tables


def tasks_file(tmp_path):
    f = tmp_path / "tasks.json"
    f.write_text(json.dumps({"suites": {"knowledgework": ["kw-02"], "portable": ["pc-10", "pc-04"]},
                             "tasks": []}), encoding="utf-8")
    return f


def test_suite_lookup_in_both_runners(tmp_path):
    f = tasks_file(tmp_path)
    assert suite_of.suite_of("pc-10", f) == "portable"
    assert suite_of.suite_of("kw-02", f) == "knowledgework"
    assert suite_of.suite_of("pc-99", f) is None
    assert run_task.suite_of("pc-04", f) == "portable"
    with pytest.raises(SystemExit, match="in no suite"):
        run_task.suite_of("pc-99", f)


def test_every_committed_task_is_in_a_suite():
    for t in json.loads(suite_of.TASKS_FILE.read_text(encoding="utf-8"))["tasks"]:
        assert suite_of.suite_of(t["id"]) is not None, t["id"]


def test_route_labels(monkeypatch):
    monkeypatch.setattr(tables, "NEEDS", {"pc-04": {"web"}, "pc-09": {"connectors"}, "pc-08": set()})
    assert tables.route("perplexity-cloud", "pc-04") == "cloud"
    assert tables.route("perplexity-local", "pc-04") == "hybrid"
    assert tables.route("perplexity-lemonade", "pc-09") == "hybrid"
    assert tables.route("perplexity-local", "pc-08") == "local"
    assert tables.route("perplexity-local", "unknown-task") == "local"
