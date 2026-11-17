# Troubleshooting

Symptom (what a tool printed) → cause → what to do. For anything touching a passphrase, your action is always **stop and tell the person what to do**, never a workaround. Never delete files, stop processes or change ACLs without the person's go-ahead.

## Packages and folders

| Symptom | Cause | What to do |
|---|---|---|
| `ERROR: psutil is missing from this Python (<path>): install the requirements first: python -m pip install -r requirements.txt -c constraints.txt` (from `run_eval.py`; `error: psutil is missing ...` from `grade_run.py`) | The packages aren't installed for the Python that runs the evals. The runner stops before any agent work, since without psutil it couldn't stop an agent at its time cap. | Run the command it prints, from the release folder, with that same `python` (the path it names); retry. |
| `ModuleNotFoundError: No module named 'cryptography'` / `'keyring'` / `'docx'`... | Same | `python -m pip install -r requirements.txt -c constraints.txt` from the release folder, with the same `python`; retry. For a separate agent user, the person installs them for all users (secrets/README.md, Layer 4, step 2). |
| `No module named keyring` from `python -m keyring set ...` | Same | Same; then the person runs the keyring command again. |
| `python: can't open file '...tools/keys.py'` (or any tool) | Run from the wrong folder | `cd` to the unzipped release folder (the one with `VERSION`, `tools/`, `secrets/`). |
| `invalid choice: 'copy-passphrase'` or `'fingerprint'` | An old checkout, branch or release | Use an up-to-date release folder (or a current checkout of `main`) for that command. |
| `python tools/evalversion.py` prints `"dirty": true` | A git checkout with modified tracked files; a release folder always reports `false` | Use a fresh release folder for runs you will report. In a release, `sha256sum -c MANIFEST.sha256` (from the release folder, in Git Bash) checks every file. Don't revert the person's changes yourself. |
| `note: suite 'assistant' is now 'everyday'` | The old suite name | Nothing; use `everyday` from now on. |
| `the working tree has uncommitted changes; package a clean commit` | `tools/package.py` on a dirty checkout | Maintainers only: commit first. |

## Passphrases (stop and ask)

Which passphrase a run needs depends on whether the answers are published yet (SKILL.md, "Which passphrase, when"). After publishing, public tasks need none, and only held-out tasks ask for `heldout-passphrase`.

| Symptom | Cause | Tell the person |
|---|---|---|
| `grading needs the answers passphrase (secrets/answers.enc, until the answers are published) ...: No passphrase for the encrypted answers (secrets/answers.enc), so they can't be graded.` | Answers not yet published; nothing stored, and `KW_ANSWERS_PASSPHRASE` unset | "Please set the answers passphrase: in a cmd or PowerShell window run `python -m keyring set ai-pc-evals answers-passphrase`, right-click to paste it at the hidden prompt, press Enter, then `python tools/keys.py check --deep` should print `decrypted OK`." Offer `--no-grade` runs meanwhile only if they agree (it costs money, no scores yet). |
| `grading needs the held-out answers passphrase ...: No passphrase for the held-out answers, so they can't be graded.` | Held-out tasks selected (`KW_HELDOUT_BUNDLE` set); nothing stored, and `KW_HELDOUT_PASSPHRASE` unset | The same, with `heldout-passphrase`. Or run only the public tasks, if the person prefers. |
| `Could not decrypt <file>: wrong passphrase, or the file was altered.` | The value stored here differs from the one the bundle was encrypted with: a character lost in the paste, an old passphrase after a rotation, a release older or newer than the passphrase | "Run `python tools/keys.py fingerprint` (for the held-out one, `fingerprint --heldout`) here and on a PC where grading works, from an up-to-date folder: both must print the same length and fingerprint. If not, set it again with `python -m keyring set ai-pc-evals <name>` (right-click paste), then `python tools/keys.py check --deep`." If `fingerprint` says the value comes from `KW_ANSWERS_PASSPHRASE` (or `KW_HELDOUT_PASSPHRASE`), that variable wins over the credential store and may hold an old value: the person clears it (PowerShell `Remove-Item Env:KW_ANSWERS_PASSPHRASE`, cmd `set KW_ANSWERS_PASSPHRASE=` with nothing after it, Git Bash `unset KW_ANSWERS_PASSPHRASE`) or closes that shell. |
| `decrypted OK` from `check --deep` but `run_eval.py` still says wrong passphrase | The variable is set differently in the shell that runs the evals | Same as above: compare `python tools/keys.py fingerprint` in that shell. |
| `nothing to decrypt: every answer is public, so grading needs no passphrase` | The answers are published and no held-out set is configured | Nothing to fix: public tasks grade without a passphrase. |
| A hidden prompt hangs, or nothing appears when typing or pasting | Git Bash's window (mintty) can't do hidden prompts | The person uses a cmd or PowerShell window for `python -m keyring set ...` and `python tools/keys.py rotate`. |
| `Could not read the passphrase from the OS credential store: ... Set KW_ANSWERS_PASSPHRASE for this shell instead.` (or `KW_HELDOUT_PASSPHRASE`) | The credential store failed (locked store, remote session policy; on Linux over SSH a locked keyring) | Tell the person the message; they may set the variable in a one-off grading shell they type into themselves (README, "Set up grading once"). You never set it. |
| `No passphrase in the OS credential store (ai-pc-evals/answers-passphrase), so nothing was copied` from `copy-passphrase` | Run on a PC that doesn't hold it | The maintainer runs it on their own PC. |
| `No clipboard tool found ...` | No `clip`/`pbcopy`/`wl-copy`/`xclip`/`xsel` | The maintainer moves it through the password manager instead. |

## Preflight (before any agent starts)

| Symptom | Cause | What to do |
|---|---|---|
| `preflight found answer material an agent could read` then `FAIL <path>` / reason / `fix: ...` | A readable copy of answer material where the agent could reach it: v0.5.x `~/.gaia/kw-keys`, `~/.kw-eval/keys`, leftover `kw-grade-*` / `kw-probe-*` / `kw-build-keys-*` temp dirs, a grading output in a run folder, a stray key copy | Show the person each path and its printed fix. Don't open the files. Delete or move nothing without their go-ahead (for the v0.5.x folders: archive old runs first). Then rerun. |
| `warn` lines: earlier runs, the package folder, `~/kw-graded`, a release zip not swept | Normal without `--strict` | Nothing; with Layer 4 and `KW_PREFLIGHT_STRICT=1` their ACLs are checked instead. |
| `warn` lines listing local model servers (process id, port, model) and free memory | Other local model servers are running | Tell the person (they compete with a local agent for memory); ask before stopping any. An app's own server (GAIA's Lemonade) is expected. |
| The sweep ran out of time (a warning; a failure with `KW_PREFLIGHT_STRICT=1`) | A large home folder, 300 s limit | The person may set `KW_PREFLIGHT_SECONDS` higher. |
| Strict preflight fails on ACLs of the package folder, `~/kw-graded` or a zip | Layer 4 step 3 not done for that path | The person applies the `icacls` deny from secrets/README.md, Layer 4, step 3. |
| Asked to use `--no-preflight` | It skips the check | Only for runs whose numbers won't be published, and only with the person's agreement. |

## Separate agent user (Layer 4)

| Symptom | Cause | What to do |
|---|---|---|
| A `runas` password prompt, or `runas as kwagent did not start within N s` | The agent user's password isn't saved yet | The person signs it in once from a cmd window with `runas /savecred` (secrets/README.md, Layer 4, step 2). You never type that password. |
| `runas as kwagent failed (exit N)` | Wrong user name, `/savecred` disabled by policy, user missing | Tell the person; see Layer 4. |
| An agent user that can't enter its task folder (exit code 98) | A run outside `KW_EVAL_HOME` (`--out` elsewhere) | Rerun without `--out` so the run stays under the eval home. |
| `STOPPED: ...` (exit 3) | The agent user's processes outlived the time cap, and a standard user can't stop them | The person runs `taskkill /F /FI "USERNAME eq kwagent"` from an elevated prompt or signs kwagent out; then grade what finished with `python tools/run_eval.py --grade-only <run folder>`. |
| `could not list kwagent's processes in time: make sure it is signed out before grading` | A slow (domain-joined) PC | The person signs the agent user out, then grade. |

## Agent apps (the manual agent)

| Symptom | Cause | What to do |
|---|---|---|
| `<agent> is driven by a person: run this from an interactive terminal` | `run_eval.py` with a manual agent started without a terminal (piped, or from a background job) | Start it in a terminal window on screen. |
| `skipping pc-09-...: needs a connector (e.g. Google Drive), and none is connected ...` | Nobody confirmed a connector (the `[y/N]` at the start, `--connected`, `KW_CONNECTED`) | Report it as not run. Pass `--connected google-drive` only when the person says Drive is connected to the app. |
| `skipping <task>: needs ..., which <agent> doesn't have here (--include-unsupported runs it anyway)` | The agent's `capabilities` in agents.json lack what the task needs (`gaia-app` declares only `web`) | Report it as not run. `--include-unsupported` only if the person asks. |
| `The prompt could not be put on the clipboard ...: copy it from <prompt.txt>` | Another app (a clipboard sync, a clipboard manager) held the clipboard | Copy the prompt from that file exactly. The answer must then go into `answer.md`: the clipboard is not offered for it. |
| `... is empty: save the agent's final answer in that file (don't paste it here), then press Enter` and it repeats | The answer was pasted into the terminal rather than saved in the file | Save the answer in that file, then press Enter alone. Typed text is ignored. |
| `Use this as the agent's answer? [y/N]` | `answer.md` was empty and the clipboard held other text | `y` only if what it shows is the app's final answer. |
| `Quit <app> now (grading puts each task's answer key in a temp folder), then press Enter` | Grading is about to start | Quit the app completely (tray icon too), then Enter. |

## Runs and grading

| Symptom | Cause | What to do |
|---|---|---|
| `not built <task>: snapshot pack ... is not ready on this machine` (pc-01, the pc-06 days) | Their saved web pages aren't on this PC (the packs aren't locked yet) | Report as not run with the reason. Never fetch replacements or edit build code. docs/TASKS.md, "Saving linked sources once", has how maintainers install them. |
| `not built <task>: ...` naming a source file whose fingerprint changed | The public source was revised upstream and this PC has no copy of the original | Report as not run. A mirror of the originals (`KW_MIRROR`, docs/RUNNING.md) fixes it, if the person has one. |
| A developer task not run because `git`, `gzip` or `pytest` is missing | A tool missing on PATH | Tell the person the fix the runner printed. |
| `not graded: quit <app> first, then run again (or pass --agent-closed)` | `--grade-only` on an app's run with no terminal to ask on | Quit the app (or have the person quit it), then `--grade-only ... --agent-closed`. In a terminal, `--grade-only` asks instead. |
| `not grading: no one to confirm <app> is closed (pass --agent-closed once it is)` | Same, during a run | Quit the app, then grade with `--grade-only <run folder> --agent-closed`. |
| `error: quit the agent app first, then pass --agent-closed` | `grade_run.py` (dedicated runner) without `--agent-closed` | Quit the app, then rerun with `--agent-closed`. |
| `not graded; grade later with: run_eval.py --grade-only <run>` | Grading was skipped (`--no-grade`, or the app wasn't confirmed closed) | Run that command once ready. |
| `<dir> is not a run folder (no kw-run.json)` | `--grade-only` given a `kw-graded` folder | Give the folder under `<eval home>/runs/`. |
| `these runs share a folder name ...` | Two runs with the same name (copies from two PCs) | Ask the person before renaming a run folder. |
| `CONTAMINATED <task>: ...`, ☣ in the tables | The agent reached for answer material (or its task folder changed after its turn) | A fail, by design. Report it with the printed finding. Don't investigate by opening answer files or the agent's files; don't rerun to "fix" it unless the person asks. Don't work in a separate-user run's task folders before grading. |
| `CHECK <task>: ...` (audit notes) | Answer-key text in the agent's own work; may simply be the right answer | Not counted; mention it so a person can check. |
| `— not graded` / `pass: null` | The judge failed for that task | Report it; regrade with `--grade-only RUN --tasks <id>` if the person wants. |
| A grading error about the judge or `claude` | Claude Code not signed in or not on PATH | `claude auth login`, then `--grade-only`. |
| `--vision` / `--compare-visuals` errors about the API key | `ANTHROPIC_API_KEY` unset | Ask the person; grading itself stands without it. |
| The run printed a same-user warning | No `KW_AGENT_USER`, or an app driven by hand: the agent runs as the person | Fine for trial runs; say the numbers are not for publishing. |
