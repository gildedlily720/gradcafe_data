---
name: jobsweep
description: Run, review and triage the user's lane-based job sweeps (specialty insurance, intelligence and security, investigations and due diligence, geopolitical risk, sanctions and financial crime, trust and safety, policy research, strategic comms, NYC/NYS/federal government, graduate/analyst/fellowship routes) using the local jobsweep MCP server or CLI plus the Indeed, Jobs and Careers and Workopia connectors. Use when the user asks to run a job sweep, find or review new jobs, triage the job board, check an employer's openings, or import connector results into jobsweep.
---

# jobsweep: run and review job sweeps

jobsweep (in `jobsweep/` at the repo root) sweeps employer ATS boards and job feeds, sorts postings into
the user's **lanes**, scores them for entry-level fit and location, and keeps them in a SQLite store. The
claude.ai connectors add Indeed and other job indexes, but they only reach jobsweep when you **import**
their raw output. A sweep has four steps: local sweep, connector searches, import, then digest and triage.

## Ground rules

- **Entry-level first.** The user is early-career (at most about 3 years of experience). Lead with Analyst,
  Associate, Assistant, Coordinator, Graduate, Trainee, Fellow, Junior, Pathways and GS-5/7/9 roles.
  Put senior roles (Senior, Lead, Director, VP, 5+ years) in a short "stretch" list only when they are
  unusually on-target, and leave them out otherwise.
- **Never apply on the user's behalf.** Don't submit applications, create accounts on employer sites,
  message recruiters, or call any apply-style action. Jobs and Careers' `job-search` can apply, so use it
  only to search. Workopia save/tailor/cover-letter tools, Dreamwork and other "apply" skills need the
  user to ask for that exact action. Searching, reading and importing are always fine.
- **Always show links.** Every job you mention gets its posting URL, linked on the title. Keep URL query
  parameters intact. If a source gave no URL, write "no link" and don't make one up.
- **Locations:** New York, Washington DC, remote (US) and London, in that order of preference. Keep a
  strong lane match elsewhere, ranked lower and with the location labelled.
- **Don't invent anything.** If a salary, deadline or requirement isn't in the posting, write "not stated".
- **Spending needs approval.** Apify, TheirStack and HasData cost credits. Ask before calling them or before
  `run_sweep(include_metered=True)`. Pipeworx USAJOBS gives about 50 free calls a day, shared with the
  sweep's USAJOBS fallback, so keep it to a handful.
- **Change statuses only when told.** `set_job_status` / `jobsweep status` record the user's decisions,
  not yours.

## 1. Local sweep: jobsweep MCP tools (preferred)

The project `.mcp.json` registers the `jobsweep` server. Its tools are `mcp__jobsweep__<name>`:

| Tool | Use |
|---|---|
| `list_sources()` | which sources are available, and why any are missing (keys, metered) |
| `run_sweep(sources=None, lanes=None, groups=None, employers=None, include_metered=False, searches=True, boards=True)` | full or targeted sweep. It runs in the background and returns status "finished" (counts, errors, `top_new`) or "running" |
| `sweep_status(wait_seconds=45)` | poll until a running sweep finishes |
| `search_jobs(query=None, lanes=None, groups=None, sources=None, statuses=None, entry_level_only=True, location=None, remote=None, new_within_days=None, min_score=None, sort="score", limit=25)` | query the store |
| `get_job(uid)` | full posting, including description and match reasons |
| `digest(days=1)` | new matches grouped by group, then lane (markdown) |
| `live_search(keywords, location=None, sources=None, limit=25)` | ad hoc search across the API sources (stored) |
| `check_employer(name_or_url)` | find and snapshot one employer's ATS board |
| `list_lanes(include_inactive=False)`, `list_employers(group=None, manual_only=False)` | taxonomy (add `include_queries=True` for the query plan) and registry; `manual_only=True` lists "check by hand" career sites |
| `import_jobs(jobs=None, text=None, origin="claude-connector")` | load connector output (step 3) |
| `set_job_status(uid, status, note=None)` | statuses: new, saved, applied, interviewing, offer, rejected, dismissed |

Typical calls:

1. `list_sources()`, then `run_sweep()`. The default sweep covers every non-metered source, all active
   lanes and the employer boards. For a narrower sweep, use something like
   `run_sweep(groups=["specialty-insurance"])` or `run_sweep(sources=["usajobs","nycjobs"], boards=False)`.
   - A full sweep takes several minutes. While the status is "running", call `sweep_status(wait_seconds=45)`.
   - Meanwhile you can run the connector searches (step 2).
2. `search_jobs(new_within_days=1, entry_level_only=True, limit=50)` and `digest(days=1)`.

The server also offers the MCP prompts `daily_sweep` and `triage_inbox`, which give the same workflow
in short form.

**CLI fallback.** If the MCP server isn't connected, run the CLI from the repo root. Use `uv run --directory
jobsweep jobsweep ...`, or `.venv/bin/jobsweep ...` when the repo venv exists:

```bash
uv run --directory jobsweep jobsweep doctor                 # keys, sources, catalog counts
uv run --directory jobsweep jobsweep sweep --json           # add --groups/--lanes/--sources to narrow it
uv run --directory jobsweep jobsweep list --entry --days 1 --limit 50
uv run --directory jobsweep jobsweep digest --days 1
uv run --directory jobsweep jobsweep show UID
```

## 2. Connector searches (claude.ai connectors in this session)

First check which of these tools exist in the session, and skip the ones that don't:

- **Indeed:** `mcp__Indeed__search_jobs(search, location, country_code, job_type=None)`.
  - Use `location` "New York, NY", "Washington, DC" or "remote" with `country_code` "US", and "London" with "GB".
  - `mcp__Indeed__get_job_details(job_id)` gives the full text of promising hits, and
    `mcp__Indeed__get_company_data(...)` answers employer questions.
  - Call `mcp__Indeed__get_resume()` only when the user asks for a resume-fit check.
- **Jobs and Careers:** `mcp__Jobs_and_Careers__job-search` covers the US only.
  - Put title variants in `keywords` (an OR list; use `keywordsAnd` for two required axes) and the location
    in `city`/`state`.
  - Pass `seniority: ["ENTRY"]`, plus `remote: true` for remote roles.
  - `vquery` is the plain-English request without the location.
  - Search only; never apply.
- **Workopia:** `mcp__Workopia__job_tool` with
  `{"action": "search", "search_jobs": {"job_title": "...", "city": "New York", "posted_within_days": 7}}`.
  Add `"workMode": "remote"` for remote roles. `mcp__Workopia__job_detail_tool(job_id=...)` returns details.
- **Optional, if connected:**
  - `pipeworx-usajobs` (`search`) for federal jobs.
  - `exa` (`web_search_exa`) for think-tank, IGO and law-firm career pages.
  - `jobspy`, `ats-jobs`, `adzuna`, `federal-compass`.
  - The metered servers `apify`, `theirstack` and `hasdata-indeed` (ask first).
  - Setup is in `jobsweep/docs/CONNECTORS.md`.

**Budget per sweep:** about 12 Indeed calls, 6 Jobs and Careers calls and 6 Workopia calls. Run the core
queries every day and rotate the rest by weekday (see the plan below). Don't repeat a query that came back
empty earlier the same day.

## 3. Import the raw connector output

After each connector batch, pass the tool's raw output unchanged, so links and IDs survive. Use one call
per batch:

```
import_jobs(text=<raw Indeed markdown>, origin="indeed-connector")
import_jobs(text=<raw Jobs and Careers JSON>, origin="jobs-and-careers")
import_jobs(text=<raw Workopia JSON>, origin="workopia")
import_jobs(jobs=[{"title": ..., "company": ..., "url": ..., "location": ..., "posted_at": ...}], origin="exa")
```

The parser understands:
- Indeed `**Job Title:** ...` blocks
- JSON lists or wrapped JSON (`items`, `topMatches`, ...)
- CSV
- markdown tables with a `Title` column
- numbered lists like `N. **[Title](url)** — Company · Location · ...`

Imported jobs are classified and scored like swept ones, and cross-source duplicates collapse.

Without MCP, save the raw output to a file and run
`uv run --directory jobsweep jobsweep import FILE --origin indeed-connector`. Use `-` to read from stdin.

## 4. Digest and triage

1. Run `digest(days=1)` and `search_jobs(new_within_days=1, entry_level_only=True, limit=50)`. When the user
   asks for "this week", use `new_within_days=7`.
2. Open the strongest ones with `get_job(uid)` and check level, years required, location and closing date.
3. Report in the output format below. Ask which to mark `saved` or `dismissed`, then call
   `set_job_status(uid, status, note)` only for the ones the user names.
4. For employers under `list_employers(manual_only=True)` (iCIMS, Taleo and custom career pages), list them
   as "check by hand" with their careers URL. The user can browse them with Claude in Chrome.

## Lane to query plan (summary)

The full plan comes from `list_lanes()`, `uv run --directory jobsweep jobsweep lanes --json`, or `catalog/lanes.yaml`.
The `queries` of each lane drive keyword sources; `usajobs` and `nycjobs` blocks drive the government feeds.

| Group | Lanes | Connector queries (rotate) | Best sources |
|---|---|---|---|
| Specialty insurance | political-risk, political-violence-terrorism, war-risk, kidnap-ransom, special-risks, trade-credit-surety, transactional-liability, financial-lines, fine-art-specie, intangibles, aviation, marine-cargo, reinsurance-broking, insurance-fraud-siu | political risk underwriter; underwriting assistant specialty; K&R underwriter; trade credit underwriter; transactional risk analyst; D&O underwriter trainee; marine underwriting assistant; reinsurance broker graduate; SIU investigator | employer boards (Workday/Oracle carriers and brokers), Indeed US + London |
| Intelligence and security | strategic-intelligence, threat-intelligence, protective-intelligence, travel-risk-crisis, gsoc-watch, corporate-security, osint, counterterrorism, regional-research-specialist, defense-intel-data | intelligence analyst entry level; threat intelligence analyst; protective intelligence analyst; GSOC analyst; OSINT analyst; counterterrorism analyst | Greenhouse/Lever boards, Indeed, Jobs and Careers |
| Investigations and DD | corporate-intelligence, due-diligence, beneficial-ownership, fraud-investigations, forensic-investigations, law-firm-analyst, litigation-case-analyst, public-defense-investigation, nonprofit-grant-dd | investigative research analyst; due diligence analyst; corporate investigations analyst; fraud investigator entry level; white collar analyst law firm | boards, Indeed, Exa for law-firm pages |
| Geopolitical risk | country-sovereign-risk, geopolitical-analysis, political-forecasting, geoeconomics, economic-security, supply-chain-forced-labor, risk-consulting, primary-research-expert-networks, investment-research-dd | geopolitical analyst; political risk analyst; country risk analyst; risk consulting analyst; expert network associate | boards, Indeed (London too) |
| Sanctions and fincrime | sanctions, export-controls, aml-kyc, fincrime-intelligence, illicit-finance, crypto-investigations, investigative-compliance, market-surveillance | sanctions analyst; export compliance analyst; financial intelligence analyst; AML analyst entry level; blockchain investigator; trade surveillance analyst | boards, Indeed, Jobs and Careers |
| Trust and safety | trust-safety-policy, policy-enforcement-ai-safeguards, ai-policy-forecasting, election-integrity, disinformation, human-rights | trust and safety analyst; policy enforcement analyst; disinformation analyst; AI policy research assistant; human rights research assistant | Greenhouse/Ashby boards, Indeed |
| Policy research | national-security, foreign-policy, think-tank-research, international-orgs, policy-legislative-analyst, tech-regulatory-policy, criminal-justice-policy, social-policy-advocacy, urban-planning-research, research-writing | national security research assistant; foreign policy research assistant; think tank research associate; legislative analyst; junior professional officer | boards, Exa, Indeed DC |
| Strategic comms | strategic-comms-advisory, crisis-comms, public-government-affairs, reputation-media-analysis, insights-monitoring | strategic communications associate; crisis communications associate; government affairs associate; media monitoring analyst | Indeed, Workopia |
| Government | district-attorney, nyc-integrity, nyc-mayors-office, nys-agencies, ny-fed, emergency-management, federal-pathways, federal-investigator | investigative analyst district attorney; DOI investigator; pathways recent graduate (series 0130/0131/0132/0301/0343/1801, GS-5 to 9); emergency management specialist | `usajobs`, `nycjobs`, `nysjobs` feeds, Pipeworx USAJOBS, federal-compass |
| Entry routes (modifier) | graduate-programs, analyst-programs, fellowships, entry-level-analyst | graduate programme insurance; analyst program; policy fellowship; junior fellow | boards, Indeed London |

Daily core for connectors: "political risk analyst", "intelligence analyst", "threat intelligence analyst",
"due diligence analyst", "sanctions analyst" and "trust and safety analyst" in New York and Washington DC
(remote for the first three). In London, run "graduate underwriter" and "political risk analyst".

Weekday rotation:

| Day | Groups |
|---|---|
| Monday | Specialty insurance |
| Tuesday | Intelligence and security |
| Wednesday | Investigations and DD, sanctions and fincrime |
| Thursday | Geopolitical risk, policy research |
| Friday | Trust and safety, strategic comms, government |
| Saturday and Sunday | Entry routes |

## Output format

Start with one summary line: how many new matches, how many entry-level, which sources ran, and which
failed or were skipped. Then add one section per group (best first), each holding this table:

| # | Title | Company | Location | Posted | Pay | Lane | Fit | Source |
|---|---|---|---|---|---|---|---|---|
| 1 | [Political Risk Underwriting Assistant](https://example.com/job/123) | Example Re | New York, NY | 2026-10-24 | $70,000-$80,000 | political-risk | entry | Indeed |

- **Fit** is `entry`, `unclear` or `stretch`, followed by a 3-8 word reason when useful.
- The jobsweep `uid` goes in a final column only when the user is triaging.
- End with these lists:
  - **closing soon** (deadline within 7 days)
  - **check by hand** (manual employers with links)
  - **next steps**, which offers to save or dismiss and never offers to apply.

The table is also importable: `jobsweep import` reads markdown tables that have a `Title` column.
