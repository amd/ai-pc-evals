# Third-party notices

The code and the files in this repository are released under the MIT License (see [LICENSE](LICENSE)),
except where this page says otherwise.

## Documents and data the tasks download

Most tasks are built from public documents and data. **None of these files are stored in this
repository or in its release zip.** `knowledgework/build_fixtures.py` downloads each one from its
publisher when you build a task, and checks it against a SHA-256 fingerprint. Each file stays under
its publisher's terms. Using this repository does not give you any rights to them.

If you share task folders you built, you share these files too. Check the terms below first, and don't
share modified copies of files whose licence forbids changes.

| Source | Used by | Terms |
|---|---|---|
| US federal agencies: BTS, BEA, BLS, Census, EIA, FDIC, FHFA, Federal Reserve (FOMC), GSA, IRS, NASA, NIST, NOAA, OPM, USCIS, USGS, eCFR and GovInfo | many tasks | Works of the US government: public domain in the United States |
| BLS bulletins served by FRASER, the Federal Reserve Bank of St. Louis's archive | knowledge-work tasks | The bulletins are works of the US government (public domain); FRASER only hosts them |
| Federal Reserve Bank of St. Louis (FRED) | knowledge-work tasks | [FRED terms of use](https://fred.stlouisfed.org/legal/). The series used are public-domain government data |
| GSA CALC+ labour rates (an extract is embedded in `build_fixtures.py`) | kw-08 | CC0 1.0, via data.gov |
| HM Revenue & Customs spending files (assets.publishing.service.gov.uk) | kw-05 | Contains public sector information licensed under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) |
| W3C Web Payments Working Group minutes | kw-13 | [W3C Document License](https://www.w3.org/copyright/document-license-2023/). Used unmodified; the task's additions are kept in a separate file |
| curl project mailing-list archive (curl.se) | kw-16 | Downloaded for local use only, never redistributed |
| Bonterms Cloud Terms, DPA and SLA | kw-10, kw-11 | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), © Bonterms. The derived files the build writes carry their own attribution |
| Common Paper Cloud Service Agreement | kw-10, kw-11 | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), © Common Paper |
| Series Seed equity documents | kw-03 | CC0 1.0 |
| Y Combinator post-money SAFE | kw-03 | © Y Combinator. Downloaded for local use only, never redistributed |
| Walmart earnings releases | kw-12 | © Walmart Inc. Downloaded for local use only, never redistributed |
| Raspberry Pi datasheets and product briefs | kw-02 | [CC BY-ND 4.0](https://creativecommons.org/licenses/by-nd/4.0/), © Raspberry Pi Ltd. Never redistribute modified copies |
| Loghub system logs, via Zenodo record 8196385 | kw-09 | The two published terms disagree: the [Zenodo record](https://zenodo.org/records/8196385) says CC BY 4.0, while the [Loghub repository](https://github.com/logpai/loghub) says the logs are for research or academic work only. Downloaded for local use only, never redistributed. Check both before you use or share them |
| NASA images, and Wikimedia Commons files marked public domain or CC0 | kw-14 | As marked on each file's page |
| Open-source project files: express, react, kubernetes, vscode, requests, python-pptx, bootstrap, vue, the Apache License text | kw-14, everyday tasks | Each file's own open-source licence (MIT, BSD or Apache-2.0) |
| ROCm TheRock, checked out at a pinned commit | dev-tr-* | MIT, © Advanced Micro Devices, Inc. |
| AMD web pages (amd.com, developer.amd.com), saved by `tools/snapshot_sources.py` | pc-01 | © Advanced Micro Devices, Inc. Saved for local use only, never redistributed |
| A Perplexity blog post (perplexity.ai), saved by hand | pc-01 | © Perplexity AI, Inc. Saved for local use only, never redistributed |
| Public Reddit threads, which pc-07's request points the agent to | pc-07 | Nothing is downloaded or stored by this repository; the agent reads them itself, under [Reddit's terms](https://redditinc.com/policies/user-agreement) |

`SOURCES` in `knowledgework/build_fixtures.py` lists every URL a knowledge-work task uses and the terms
that apply; `knowledgework/portable/packs.json` lists the portable tasks' saved pages.

## Files stored in this repository

The documents under `knowledgework/assistant/corpus/`, `knowledgework/fixtures/` and
`knowledgework/developer/toybox/` were written for this project and are released under the MIT License.
Some of them were first written for AMD's [GAIA](https://github.com/amd/gaia) evaluation fixtures (MIT).
The companies, people and products in them are fictional, except for:

- `knowledgework/assistant/corpus/eval/corpus/real_world/legal/apache_license_2.0.txt` and `mit_license.txt`:
  paraphrases of the Apache License 2.0 and the MIT License, whose texts may be freely copied.
- `knowledgework/assistant/corpus/eval/corpus/real_world/spreadsheets/us_labor_statistics_2024.xlsx`: rounded
  figures from the US Bureau of Labor Statistics (public domain).

The document files in the repository were all generated by scripts, not taken from anywhere:

| File | Generated by |
|---|---|
| `fixtures/tiers_documents/*.pdf`, `orion_onboarding.docx` and `malformed/board_minutes_corrupt.pdf` (random bytes after a PDF header, deliberately unreadable) | ReportLab and python-docx, from GAIA's [`tests/fixtures/gaia/tiers_documents/build_fixtures.py`](https://github.com/amd/gaia/blob/main/tests/fixtures/gaia/tiers_documents/build_fixtures.py) |
| `eval/corpus/real_world/spreadsheets/us_labor_statistics_2024.xlsx` | openpyxl, from GAIA's [`eval/corpus/gen_real_world.py`](https://github.com/amd/gaia/blob/main/eval/corpus/gen_real_world.py) |
| `tests/regression/pc10-haiku-dimension-grid.pptx` | A deck that Claude Haiku wrote with python-pptx during a run of pc-10, kept as a test input for the grader |

(The first two paths are under `knowledgework/assistant/corpus/`.)

`knowledgework/assistant/corpus/fixtures/tiers_skills_web/downloads/atlas-companion-setup.exe` is not a
program. It is an 86-byte text file (`MZ`, then "KW EVAL FIXTURE - this is not a program and cannot run.") that
tests whether an agent runs a downloaded installer. There are no other executables in this repository.

## Python packages

`requirements.txt` lists the packages the evals install. None of them is copied into this
repository. Each is installed from PyPI under its own licence, including MIT, BSD, Apache-2.0, PSF-2.0
(`typing_extensions`) and MIT-CMU (Pillow). One dependency of a dependency is weak copyleft: `certifi`
(installed by `requests` and `httpx`) is MPL-2.0; it is used unmodified. Some wheels bundle native
libraries under their own licences, listed in each wheel: `pypdfium2` ships the PDFium binary (BSD-3-Clause
and Apache-2.0, with FreeType, libjpeg-turbo, OpenJPEG, lcms2 and others), and `numpy`, `lxml` and Pillow
bundle libraries too. To see every installed package's licence, run
`python -m pip install pip-licenses && pip-licenses --with-urls` in a venv made with
`-r requirements.txt -c constraints.txt`. `shellcheck-py` in `requirements-dev.txt` installs the ShellCheck binary, which is
GPL-3.0. It is a development tool only, used by the lint workflow: it is not in `requirements.txt`, nothing in the evals links to it, and the release zip leaves out `requirements-dev.txt`.

## Trademarks

AMD, the AMD Arrow logo, Ryzen, Radeon, ROCm and combinations thereof are trademarks of Advanced Micro
Devices, Inc.

Other product and company names in this repository are trademarks of their owners, among them:
Anthropic and Claude; Microsoft, Windows, Office and OneDrive; Google, Google Drive and NotebookLM;
NVIDIA; Perplexity; OpenAI; Reddit; GitHub; Slack; Ollama; LM Studio; OpenCode; OpenClaw; Hermes (Nous
Research); Pi; Ubuntu (Canonical); Linux (Linus Torvalds); Walmart; Raspberry Pi; Y Combinator; and curl.
They are used only to name the products, data or documents involved. This project is not affiliated with
or endorsed by those owners.
