# Job search analysis

`analyze_tracker.py` runs a statistical analysis of a job-application tracker kept in Excel. It is the same
style of analysis as the "stats from my job search" write-ups, done with methods that respect how small
and how incomplete a personal job search dataset is.

No tracker data lives in this repository: `.gitignore` already excludes `*.xlsx` and `*.html`.

## Input

An `.xlsx` workbook with an **Applications** sheet, one row per application, with at least:

| Column | Notes |
|---|---|
| `Date Applied` | a real date, not text |
| `Employer`, `Role / Req`, `Track`, `Channel`, `Status`, `Notes` | free text |
| `Stage` | `Live`, `Silent`, `Closed`, `Stalled after interview`, ... |
| `Last Contact` | date of the last employer contact (the decision date for closed rows) |

Optional coded columns make the analysis sharper. When one is missing, a default is derived from the columns above.

| Column | Values |
|---|---|
| `App ID` | stable id, e.g. `A001` |
| `Sector (grouped)` | a handful of broad sectors |
| `Channel (grouped)` | `Employer ATS / careers site`, `NYC / NY State portal`, `USAJOBS`, `Direct email`, `LinkedIn Easy Apply`, `Referral link` |
| `Insider contact` | `Formal referral`, `Insider outreach`, `Recruiter-initiated`, `No insider contact` |
| `Furthest stage` | `No employer reply`, `Declined / closed, no screen`, `Referred / eligible list`, `Screen / assessment`, `Interview` |
| `Outcome` | `Open - no decision`, `In process`, `Stalled - no closure`, `Declined - no interview`, `Declined - after screen/interview`, `Withdrew`, `Role filled / closed`, `Not a job application` |
| `First reply` | date of the first substantive employer reply (not an automatic receipt) |
| `In rate stats` | `Yes` / `No`, to keep talent-database sign-ups and similar rows out of the rates |

An optional **Events** sheet (`App ID`, `Date`, `Event`, `Event group`, `Detail`) holds one row per event. The
analysis uses its `Application submitted` events as the submission date when that differs from `Date Applied`.

## Run

```bash
pip install pandas numpy scipy openpyxl
python3 analyze_tracker.py tracker.xlsx --as-of 2026-10-23 --out-dir out \
    --write-xlsx out/tracker_with_analysis.xlsx --dataset-dir out/datasets
```

Outputs:

- `out/analysis.json`: every result.
- `--write-xlsx`: a copy of the workbook with an **Analysis** sheet of tables and an **Open pipeline** sheet.
  The pipeline sheet lists each open application with its chance of a reply and of a screen, plus a suggested action.
- `--dataset-dir`: flat JSON tables (summary, funnel, reply curve, silence, segments, weekly, pipeline, advanced)
  for a dashboard page.

## Methods

- **Funnel**: application, employer reply, advanced (a screen, assessment, federal referral list or interview),
  live interview, offer.
- **Time to first reply**: Kaplan-Meier with Greenwood log-log 95% bands. Applications still waiting are
  right-censored at `--as-of`, not counted as "never".
- **Competing first replies**: Aalen-Johansen cumulative incidence of "advanced" versus "declined / closed".
- **Segments**: reply and advance rates with Wilson 95% intervals, a 30-day Kaplan-Meier reply rate, and Fisher
  exact tests against the rest. The tests use only applications old enough to have been answered.
- **Sector shrinkage**: an empirical-Bayes beta-binomial fit, with a chi-square test for any sector difference.
- **Silence**: P(reply still coming | silent for t days) = (S(t) - S(end)) / S(t), and the same for advancing.
- **Forecast**: those conditional chances summed over the open applications, with an 80% range from the
  Poisson-binomial distribution. It is repeated using only recent applications as the reference
  (`--recent-since`, default 150 days back) as a sensitivity check.
