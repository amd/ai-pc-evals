# Contributing

Every change goes through a pull request against `main`; nobody pushes to `main` directly and nobody force-pushes. CI runs `pytest` on Ubuntu and Windows, and a PR merges once that is green and a maintainer has reviewed it.

Before opening one:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Changes to ground truth need extra care, because every recorded result was graded against the old version:

- **Answer keys, checkers or task definitions** (`answer-keys/`, `knowledgework/tasks.json`, `knowledgework/xlsx_eval.py`): say in the PR which tasks change, and re-grade every setup that ran them. `answer-keys/_xlsx_eval.py` must stay byte-identical to `knowledgework/xlsx_eval.py` (a test checks it).
- **Pins** (`PINS` in `knowledgework/build_fixtures.py`): re-pinning a source changes the test. Prefer building from a pinned copy (see the README); re-pin only when the old file can't be had, and update that task's answer key and source note in the same PR.
- **The confirmation policy** (`tools/gaia-tui/confirm_policy.py`): add a test case for every rule you add or relax.
