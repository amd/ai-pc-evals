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
| US federal agencies: BTS, BEA, BLS, Census, EIA, FDIC, FHFA, Federal Reserve (FOMC, FRASER), GSA, IRS, NASA, NIST, NOAA, OPM, USCIS, USGS, eCFR and GovInfo | many tasks | Works of the US government: public domain in the United States |
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
| Loghub system logs, via Zenodo record 8196385 | kw-09 | As published by the Loghub project (LogPAI). Read [its terms](https://github.com/logpai/loghub) before you use or share them |
| NASA images, and Wikimedia Commons files marked public domain or CC0 | kw-14 | As marked on each file's page |
| Open-source project files: express, react, kubernetes, vscode, requests, python-pptx, bootstrap, vue, the Apache License text | kw-14, everyday tasks | Each file's own open-source licence (MIT, BSD or Apache-2.0) |
| ROCm TheRock, checked out at a pinned commit | dev-tr-* | MIT, © Advanced Micro Devices, Inc. |

The source note for each task (`knowledgework/sources/<task>.md`) lists every URL it uses and the terms
that apply.

## Files stored in this repository

The documents under `knowledgework/assistant/corpus/`, `knowledgework/fixtures/` and
`knowledgework/developer/toybox/` were written for this project and are released under the MIT License.
Some of them were first written for AMD's [GAIA](https://github.com/amd/gaia) evaluation fixtures (MIT).
The companies, people and products in them are fictional, except for:

- `knowledgework/assistant/corpus/eval/corpus/real_world/legal/apache_license_2.0.txt` and `mit_license.txt`:
  paraphrases of the Apache License 2.0 and the MIT License, whose texts may be freely copied.
- `knowledgework/assistant/corpus/eval/corpus/real_world/spreadsheets/us_labor_statistics_2024.xlsx`: rounded
  figures from the US Bureau of Labor Statistics (public domain).

## Python packages

`requirements.txt` lists the packages the evals install. None of them is copied into this
repository. Each is installed from PyPI under its own licence, including MIT, BSD, Apache-2.0 and the
HPND licence (Pillow). `shellcheck-py` in `requirements-dev.txt` installs the ShellCheck binary, which is
GPL-3.0. It is a development tool only: nothing in the evals links to it or ships it.

## Trademarks

Product and company names in this repository, such as Claude, Perplexity, Walmart, Raspberry Pi, Y
Combinator and curl, are trademarks of their owners. They are used only to name the products, data or
documents involved. This project is not affiliated with or endorsed by those owners.
