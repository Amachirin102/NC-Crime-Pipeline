# Build log

How this project was scoped, what was delegated to Claude Code, and how the
output was checked. The checks section is the important part: each row is a
problem that verification caught, not a hypothetical.

## 1. Scope

**Goal:** show the day-to-day of a public-data newsroom engineering role in one
working system, not four toy repos:

| Skill the role needs | Where it lives here |
|---|---|
| Messy multi-source ingestion | `civicpipe/ingest/` — ArcGIS API (live), agency CSVs, agency PDFs |
| Normalization & record linkage | `civicpipe/normalize/`, `civicpipe/resolve/` — jurisdictions (live data) and person/address linkage (benchmark) |
| Validated pipelines | `civicpipe/audit/` — data contracts, batch gates, deadline rules, month-over-month diff |
| Public-facing output | `output/index.html`, `output/cmpd_monthly_trend.png` — chart, stat tiles, plain-language caveats, audit table |
| AI-built tooling | this log |

**Ground rules set before any code was written**

- Anything public uses real data only (CMPD open data). Synthetic data is used
  in exactly two places, both labeled: the entity-resolution benchmark (no
  real people) and the parser fixtures (fictional agencies: "Town of
  Pinecrest", "Harlow County").
- Every number in the README comes from a command in this repo, not from memory.
- A failed audit stops publishing. Warnings are shown to the public, not hidden.

**Source reconnaissance (before writing the ingester)**

Direct queries against the CMPD ArcGIS layer established:

- 871,188 rows total, `maxRecordCount` 2,500, statistics queries supported.
- Dates are **local midnight encoded as UTC** (`04:00Z` in summer, `05:00Z` in
  winter). A naive UTC conversion shifts every record back one day, which moves
  month-end records into the wrong month. The ingester converts to America/New_York.
- The layer description says 800-series NIBRS codes are non-criminal and that
  unfounded reports are included. Both shaped the offense grouping and the
  unfounded exclusion.
- `CITY` is free text: `CHARLOTE`, `CHARLOTTE, NC 28211`, `28215`, `MATHEWS`,
  `CHARLOTTE6090`. That became the real-data entity-resolution task.
- Incident IDs start with the report date (`20260708-1046-03`), which gives a
  free cross-field consistency check.

## 2. What was delegated to Claude Code

Claude Code (in the desktop app) wrote the code, tests, fixtures, and first
drafts of these docs, working from the job spec plus the constraints above. It
also ran everything: the live pulls, the benchmark, the test suite, and a
browser check of the public page at desktop and phone widths.

The human-owned calls are the policy decisions in section 4. They are written
down so they can be challenged.

## 3. How the output was verified, and what verification caught

| # | Check | What it caught | Fix |
|---|---|---|---|
| 1 | First benchmark run | Precision was **1.000 at every threshold**. The benchmark was too easy: every hard negative differed on DOB. | Added twins (same address and DOB) and a 20% no-DOB rate, which is typical of owner-record sources. Numbers became realistic. |
| 2 | Error analysis of the misses (listed record by record) | **Two linker bugs.** Swapped first/last names were compared on the first token only (`VAN` vs `VAN BUREN`), and a `JR`/`SR` suffix that landed in the first-name field after a swap went undetected. | Compare full name strings; read the suffix from either field; add an order-insensitive blocking key. Dev recall at 0.65 rose from 96.6% to 99.4%. |
| 3 | Threshold sweep | Picking the threshold with the best F1 chose the bottom of the range, where F1 is flat. That rewards a benchmark quirk (one-to-one matching doing the work), not real accuracy. | Choose the **highest** threshold within 0.5 pt of the best recall at ≥ 99.5% precision. Pick it on a dev set (seed 7) and report on a held-out test set (seed 2026). |
| 4 | Ambiguous pairs | Twins at one address with no DOB can't be separated by any score. | `needs_review` flag when a competing candidate is within 0.05. These go to a person, not to production. |
| 5 | First test run: 4 failures | `123 N TRYON ST #4B` didn't split out the unit (a regex word boundary before `#`). A cross-field rule crashed when an optional column was missing. | Fixed both. The suite now has 48 tests. |
| 6 | PDF parser on the 2026 layout | The "Total" label moved from the description column to the code column, so the total was **parsed as a data row**. | Detect the label in any column. Reconcile the row sum against the printed total. Flag 10x jumps vs the prior year. Non-numeric cells raise an exception, never a silent zero. |
| 7 | First live audit run | **February 2026 was flagged** as a volume anomaly (z = −4.4). | Investigated: all 28 days present, 222 reports/day vs 225 in January. The check compared raw counts, so every February would flag. Changed it to reports per day and added a zero-report-day gap check. Still flagged at z = −3.6, down 10% from Feb 2025. Reviewed, judged real, and recorded in `config/reviewed.yaml`. |
| 8 | First live audit run | One real report was **quarantined because its incident date was `1926-07-19`**, a field the chart doesn't use. | Policy: severity follows whether the field feeds the published number. `date_occurred` range violations are now WARN. |
| 9 | Looking at the rendered chart | The current, partial month (Sept 1–22) showed as a **plunge**, and the "preliminary" label didn't stop it reading that way. | Months the data doesn't cover end-to-end are never charted. Complete but unsettled months show as hollow "preliminary" points. |
| 10 | Browser check at 375px | Chart axis text shrank to about 5px on phones (a fixed 820px SVG scaled down). | Redraw at the container's real width. |
| 11 | Jurisdiction crosswalk review | `CHARLOTTE, NORTH CAROLINA` and `CHARLOTTEJAVASCRIPT:VOID PT_SU` (web-form residue) were unresolved. | Strip the state and country names; add a prefix tier. `MIDLAND`, `CONCORD`, and `FAYETTEVILLE` stay unresolved **on purpose**: they are real places outside the county. |
| 12 | Live pull on Windows | The raw snapshot write failed: the path exceeded 260 characters in the temporary workspace. | Moved the project to a short path. |
| 13 | First GitHub Actions run | Tests passed, but the live pull **failed after 65 s on GitHub's runner**. The same command passed locally with identical data. The reason was only in the job log, which needs a sign-in to read. | Failures now surface as public annotations. The city server's F5 firewall cookies (`BIGipServer…`, `TS…`) point to cloud IPs being refused. The monthly job is now manual and runs from a home connection; tests and Pages deploy still run on GitHub. |

## 4. Decisions a person has to own

These are judgment calls, not facts. Each one changes a published number.

- **Months by report date, not incident date.** Stable and always present, but
  4,302 reports in this pull were filed more than a year after the incident began.
- **Unfounded reports excluded** (3.8% of violent and property reports in this pull).
- **30-day settle window** before a month counts in the year-over-year comparison.
- **"Violent" = FBI violent-crime offenses** (09A, 11A–11C, 120, 13A). Simple
  assault is excluded, which is standard but not universal.
- **Error budget of 0.5%** quarantined rows before publishing stops.
- **Entity-resolution threshold chosen for precision**, because a false merge
  costs more than a missed link.
- **ZIP → town table** (`resolve/jurisdiction.py`) is an assumption, marked in
  the code for review against county GIS.

## 5. What is *not* verified

- The linker is measured on synthetic data only. Real-world accuracy would need
  a hand-labeled sample, and the harness (`resolve/evaluate.py`) is ready for one.
- The diff harness is verified by unit tests with planted restatements and by
  two live pulls hours apart on 2026-09-24. Those produced a correct zero diff
  (the portal hadn't updated), which rules out phantom changes but doesn't show
  a real restatement being caught. That evidence comes from the first scheduled
  monthly run.
- The PDF parser is tested on generated PDFs that reproduce known layout
  problems. It has not yet been run against a real agency's PDF archive.
