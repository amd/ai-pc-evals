# Security policy

## Reporting a vulnerability

Please **don't open a public GitHub issue, discussion or pull request** for a security vulnerability.

Report it to AMD's Product Security Incident Response Team (PSIRT):

- Email: [psirt@amd.com](mailto:psirt@amd.com)
- Web: [AMD Product Security](https://www.amd.com/en/resources/product-security.html), which also explains how AMD handles reports and how to encrypt yours

Please include:

- that the report is about this repository (github.com/amd/ai-pc-evals) and the version (`python tools/evalversion.py` prints it);
- what the problem is and what an attacker could do with it;
- the steps or a proof of concept to reproduce it;
- any suggested fix.

## What's in scope

This repository is a test suite that runs AI agents on a PC and grades their work. Examples of what we want to hear about:

- **Code execution or privilege problems in the runners**, for example in `tools/run_eval.py`, the agent adapters in `tools/agents/`, the separate-user runner (`tools/isolation.py`), or the grading engine (`kwengine/`): a way for a task's inputs, an agent, or a run folder to run code or gain access beyond what [docs/ISOLATION.md](docs/ISOLATION.md) describes.
- **Ways for the agent under test to escape its confinement** in ways the docs say are prevented, such as reading a passphrase, the encrypted answers (`secrets/answers.enc`) or their passphrase, held-out answers or another user's files during a separate-user run.
- **Weaknesses in how the answers are kept from the agent under test**: `tools/keys.py`, the encrypted held-out bundle, the preflight and the audit, or grading leaving held-out answers on disk.
- **Secrets leaking** into run folders, logs, results or release packages.
- **Supply-chain problems** in the release workflow or the release package.

Out of scope:

- The limits already documented in [docs/ISOLATION.md](docs/ISOLATION.md#known-limits) and [secrets/README.md](secrets/README.md), such as same-user runs, where the agent runs as you and can read what you can. These are known and described there.
- The held-out set being empty: every task's answers are in `secrets/answers.enc`, encrypted. A way to read them without the passphrase is in scope.
- Vulnerabilities in third-party agents (Claude Code, Perplexity Computer and others), models or the websites tasks download from. Please report those to their vendors.
- An agent getting a task wrong, or a grading mistake that isn't a security problem. Please open a normal issue for those.

## Supported versions

Fixes go into the latest release. Please check that the problem is still there in the latest release or on `main` before reporting.
