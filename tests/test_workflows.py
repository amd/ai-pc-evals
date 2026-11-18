# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""The CI workflows: a release is gated on every check workflow, and the probe tests' log shows ids, not answers."""
import re
import textwrap
from pathlib import Path

import pytest

import ci_probe_tests

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
#: Workflows that are not checks, and why release.yml doesn't call them.
NOT_CHECKS = {
    "release.yml": "the release itself",
    "outside-prs.yml": "closes outside pull requests; checks nothing",
}


def jobs(path: Path) -> dict:
    """{job id: its block of text}, from the top-level 'jobs:' map (two-space indented ids)."""
    text = path.read_text(encoding="utf-8")
    body = text.split("\njobs:\n", 1)[1]
    found, current = {}, None
    for line in body.splitlines():
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            found[current] = []
        elif current is not None:
            found[current].append(line)
    return {k: "\n".join(v) for k, v in found.items()}


def needs(block: str) -> set:
    m = re.search(r"^    needs:\s*\[([^\]]*)\]", block, re.M) or re.search(r"^    needs:\s*(\S+)\s*$", block, re.M)
    return {n.strip() for n in m.group(1).split(",")} if m else set()


def test_package_needs_every_check_workflow():
    """A new workflow can't be added without gating releases: release.yml calls every check workflow from the
    tagged commit, and the job that publishes needs every one of those calls."""
    release = jobs(WORKFLOWS / "release.yml")
    calls = {}
    for job, block in release.items():
        m = re.search(r"^    uses:\s*\./\.github/workflows/(\S+)\s*$", block, re.M)
        if m:
            calls[m.group(1)] = job
    checks = sorted(p.name for p in WORKFLOWS.glob("*.yml") if p.name not in NOT_CHECKS)
    assert len(checks) >= 6, checks
    missing = [c for c in checks if c not in calls]
    assert not missing, f"release.yml doesn't call {missing}: add a job that uses it, and to package's needs"
    assert not set(calls) - set(checks), "release.yml calls a workflow listed as not a check"
    assert set(calls.values()) <= needs(release["package"]), (
        f"package must need every check: missing {sorted(set(calls.values()) - needs(release['package']))}")
    for name in checks:
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert re.search(r"^  workflow_call:", text, re.M), f"{name} can't be called: add on.workflow_call"


def test_jobs_with_the_answers_use_the_environment_and_never_run_on_pull_requests():
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        for job, block in jobs(path).items():
            if "secrets.KW_ANSWERS_PASSPHRASE" in block:
                assert re.search(r"^    environment: answers\s*$", block, re.M), f"{path.name}:{job}"
                assert "::add-mask::" in block, f"{path.name}:{job} masks the passphrase"
                assert not re.search(r"^  pull_request(_target)?:", text, re.M), \
                    f"{path.name} runs on pull requests, whose branches can't use the answers environment"


def test_the_issue_filing_jobs_only_run_on_the_weekly_or_manual_run():
    """Called from release.yml (event 'push' on a tag) they are skipped: a release only checks."""
    for path in WORKFLOWS.glob("*.yml"):
        for job, block in jobs(path).items():
            if "issues: write" in block and "uses:" not in block:
                cond = re.search(r"^    if:\s*(.+)$", block, re.M)
                assert cond and "github.event_name == 'schedule'" in cond.group(1), f"{path.name}:{job}"
                assert "'push'" not in cond.group(1)


# ---- tools/ci_probe_tests.py ---------------------------------------------------------------------------------

SECRET = "Zephyrcore Mantis 7 drives two panels"


@pytest.fixture
def fake_suite(tmp_path):
    (tmp_path / "test_fake.py").write_text(textwrap.dedent(f"""
        import pytest

        def test_passes():
            assert 1

        def test_fails():
            expected = {SECRET!r}
            print("probe said: " + expected)
            assert "something else" == expected, "expected " + expected

        def test_skips_for_want_of_answers():
            pytest.skip("this machine has no passphrase for it")

        def test_skips_for_git():
            pytest.skip("needs git")
        """), encoding="utf-8")
    return tmp_path


def test_the_probe_test_summary_shows_ids_and_outcomes_only(fake_suite):
    results = ci_probe_tests.run(["test_fake.py"], repo=fake_suite)
    summary, bad = ci_probe_tests.summarize(results)
    assert bad
    assert SECRET not in summary and "probe said" not in summary
    assert "FAILED test_fake.py::test_fails" in summary
    assert "SKIPPED test_fake.py::test_skips_for_want_of_answers (no answers" in summary
    assert "SKIPPED test_fake.py::test_skips_for_git\n" in summary + "\n"
    assert summary.startswith("1 passed, 1 failed, 0 errors, 2 skipped")


def test_a_skip_for_want_of_the_answers_fails_the_run():
    _, bad = ci_probe_tests.summarize({"exit": 0, "records": [
        {"id": "t::a", "outcome": "passed"}, {"id": "t::b", "outcome": "skipped", "no_answers": True}]})
    assert bad
    _, bad = ci_probe_tests.summarize({"exit": 0, "records": [
        {"id": "t::a", "outcome": "passed"}, {"id": "t::b", "outcome": "skipped", "no_answers": False}]})
    assert not bad


def test_nothing_run_is_a_failure():
    assert ci_probe_tests.summarize({"exit": 5, "records": []})[1]


def test_the_default_tests_are_the_ones_that_skip_without_the_answers():
    for name in ci_probe_tests.DEFAULT_TESTS:
        text = (REPO / name).read_text(encoding="utf-8")
        assert "full_tasks" in text or "public_payload" in text or "NO_PROBES" in text, name
    for test in ci_probe_tests.DESELECT:
        file, func = test.split("::")
        assert f"def {func}(" in (REPO / file).read_text(encoding="utf-8"), test


def test_release_passes_the_answers_secret_to_every_workflow_that_needs_it():
    """A called workflow gets an environment secret only if its caller passes it; otherwise it is silently empty
    (v0.7.1's first release run skipped every probe test). release.yml passes it, and an empty secret can't pass
    unnoticed: either the workflow requires it when called (allowed only when every job is in the answers
    environment, since GitHub checks a required secret for every job: v0.7.1's second run failed on sources.yml's
    drift job), or every step that reads it fails on an empty value."""
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if path.name == "release.yml" or "secrets.KW_ANSWERS_PASSPHRASE" not in text or "workflow_call" not in text:
            continue
        call = re.search(rf"uses: \./\.github/workflows/{re.escape(path.name)}\n((?:    .*\n)*)", release)
        assert call and "secrets: inherit" in call.group(1), f"release.yml must pass secrets to {path.name}"
        blocks = jobs(path)
        required = re.search(r"workflow_call:.*?\n\s+secrets:.*?\n\s+KW_ANSWERS_PASSPHRASE:\s*\n\s+required: true", text,
                             re.S)
        if required:
            outside = [job for job, block in blocks.items() if not re.search(r"^    environment: answers\s*$", block, re.M)]
            assert not outside, f"{path.name}: requires the secret, but {outside} run outside the answers environment"
        else:
            for job, block in blocks.items():
                for step in re.split(r"\n      - ", block):
                    if "secrets.KW_ANSWERS_PASSPHRASE" in step:
                        assert '-z "$KW_ANSWERS_PASSPHRASE"' in step, (
                            f"{path.name}:{job}: a step reading the secret must fail when it is empty")
