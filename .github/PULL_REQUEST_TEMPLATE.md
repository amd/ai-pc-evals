## What this changes

<!-- One or two sentences: what and why. Link the issue if there is one. -->

## Checks

- [ ] `python -m pytest -q` passes
- [ ] `python -m ruff check .` passes
- [ ] `shellcheck -S warning $(git ls-files "*.sh")` passes (if shell scripts changed)
- [ ] `USE_CASES.md` regenerated with `tools/use_cases.py`, not edited by hand (if tasks changed)
- [ ] `CHANGELOG.md` updated

## Ground truth

Does this change what counts as a right answer? If not, tick the first box and skip the rest.
See "Changes to ground truth" in [CONTRIBUTING.md](../CONTRIBUTING.md).

- [ ] No ground truth changes: no answer keys, checkers, task definitions, prompts, pins or grading engine code
- [ ] **Answer keys, checkers or task definitions** (`answers/`, `knowledgework/tasks.public.json`, `knowledgework/xlsx_eval.py`): the tasks that change are listed below, every setup that ran them will be re-graded, answers are only in `answers/` (`python tools/keys.py refresh` was run), and `python tools/keys.py check` passes
- [ ] **Task prompts**: the prompt owner has signed off on the new wording
- [ ] **Pins** (`PINS` in `knowledgework/build_fixtures.py`): re-pinned only because the old file can't be had; the task's answer key and source note are updated in this pull request
- [ ] **The engine** (`kwengine/`): what grades differently is described below, and the runs being compared will be re-graded; the AMD copyright and MIT header are kept in each vendored file
- [ ] **The separate-user runner** (`tools/isolation.py`): `tests/test_isolation.py` still runs the generated batch file for real

Tasks affected and what grades differently:

<!-- e.g. kw-06: the checker now accepts ...; re-grade every kw-06 run. -->
