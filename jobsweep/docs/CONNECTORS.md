# Connectors: linking job-search tools to jobsweep and Claude

This page covers every job-search connector, MCP server and Claude extension used with this project. It
says what each one covers, what it costs, how it's wired up here, whether it answered when probed on
**2026-10-25**, and which of the user's lanes it helps most.

How the pieces fit:

```
employer ATS boards, USAJOBS, NYC Jobs, NYS, Adzuna, TheirStack, HasData, JobSpy, Apify
        |  (jobsweep adapters, run by `jobsweep sweep`, the MCP tool run_sweep, or GitHub Actions)
        v
  jobsweep store (SQLite) --> web board / static Pages board / CLI / digest
        ^
        |  import_jobs / `jobsweep import`  (raw connector output: Indeed markdown, JSON, tables)
claude.ai connectors (Indeed, Jobs and Careers, Workopia, Pipeworx USAJOBS, Exa, ...)
```

Files in this repo:

| File | Purpose |
|---|---|
| `/.mcp.json` | Claude Code project servers (jobsweep and the job MCP servers) with `${VAR:-}` key expansion |
| `/.claude/settings.json` | pre-approves the project servers that need no secrets (`enabledMcpjsonServers`) and allows only the read-only jobsweep tools |
| `/.claude/skills/jobsweep/SKILL.md` | how Claude runs a sweep end to end (local tools, then connectors, import, digest) |
| `jobsweep/integrations/claude_code_setup.sh` | idempotent `claude mcp add` script (user scope by default, keys from env/.env, `--dry-run`) |
| `jobsweep/integrations/claude_desktop_config.json` | Claude Desktop `mcpServers` block |
| `jobsweep/integrations/claude_ai_connectors.md` | claude.ai connector steps |
| `jobsweep/integrations/project_instructions.md` | paste-ready instructions for the claude.ai Project "Job sweeps" |
| `jobsweep/integrations/scheduled_task_prompt.md` | paste-ready daily 7:40am ET scheduled task |
| `jobsweep/.env.example` | every credential jobsweep reads, with where to get it |

## Connector table

**How linked** values:
- **.mcp.json**: Claude Code project scope. ✓ means auto-enabled by `.claude/settings.json`.
- **setup.sh**: added by `claude_code_setup.sh` (user scope).
- **Desktop**: in `claude_desktop_config.json`.
- **claude.ai**: a claude.ai connector, which also shows up in Desktop and in Claude Code under the same
  login.
- **adapter**: jobsweep calls the API directly.
- **doc-only**: documented here, not wired.

| Connector / tool | Covers | Auth / cost | How linked here | Verified 2026-10-25 | Most useful lanes |
|---|---|---|---|---|---|
| **jobsweep** (local MCP, `jobsweep-mcp`) | the swept store: search, sweep, digest, triage, import | none (reads `jobsweep/.env`) | .mcp.json ✓, setup.sh, Desktop | entry point `jobsweep-mcp` installed in the repo venv; server module owned by the mcp component | all |
| **Indeed** connector (beta) | Indeed jobs: Job Search, Job Detail, Get Company Data, Get Resume | sign in with Indeed; free | claude.ai (directory). It's a remote Streamable HTTP server "available only for Claude Connector", with no URL for config files | tools live in this account's sessions (`mcp__Indeed__search_jobs`, `get_job_details`, `get_company_data`, `get_resume`). A real output sample parses via `import_jobs` (`tests/fixtures/import/indeed_connector_search.md`) | all; best for specialty insurance, intel and security, investigations, sanctions, T&S, comms; London via `country_code` GB |
| **Jobs and Careers** connector | US jobs index with a hard `seniority` filter (ENTRY) | account connector; free | claude.ai | tool live (`mcp__Jobs_and_Careers__job-search`); real output sample parses. **The tool can also apply, so only use it to search** | US entry-level: intel and security, sanctions and fincrime, investigations, comms |
| **Workopia** connector | jobs in 90+ countries, search/refine, detail cards | account connector; save needs OAuth | claude.ai | tools live (`mcp__Workopia__job_tool`, `job_detail_tool`); real output sample parses | London plus US: policy, comms, risk; use `workMode` remote for remote roles |
| **Pipeworx USAJOBS** | USAJOBS search: `search`, `get_job`, `list_agencies`, `list_pay_grades`, `list_occupational_series`, plus about 42 shared meta-tools (`ask_pipeworx`, ...) | free, about 50 calls/day per IP; your own key via `?_apiKey=<auth_key>:<email>` | .mcp.json ✓ (http), setup.sh (adds the key when `PIPEWORX_API_KEY` is set), Desktop (via `npx -y mcp-remote`), claude.ai custom connector; jobsweep's `usajobs` adapter falls back to the Pipeworx REST gateway | **responds**: initialize and tools/list OK without auth (47 tools). No tool calls were made, to save the shared quota | government: federal-pathways, federal-investigator, emergency-management; policy-research at agencies |
| Pipeworx full gateway `https://gateway.pipeworx.io/mcp` | 7,130 tools across 1,876 sources | as above | doc-only (too many tools for a job session) | **responds** (initialize OK) | none (general data) |
| `@pipeworx/mcp-usajobs` (stdio) | the same USAJOBS pack as a local process: `npx -y @pipeworx/mcp-usajobs` | as above | doc-only alternative | npm 0.1.2 exists; not run | government |
| **JobSpy MCP** (`uvx mcp-jobspy@latest`) | LinkedIn, Indeed, ZipRecruiter, Glassdoor, Google, Bayt, Naukri, BDJobs, HelloWork scraping | free (scraping; rate-limited by the sites) | .mcp.json ✓, setup.sh, Desktop; jobsweep also has a `jobspy` adapter (python-jobspy) | **responds**: PyPI `mcp-jobspy` 1.4.1; started over stdio, tools `search_jobs`, `get_job_details` | broad keyword coverage for all lanes; LinkedIn helps intel, geopolitical and T&S |
| aRustyDev/mcp-jobspy, borgius/jobspy-mcp-server | Node JobSpy servers: local clone plus a JobSpy `run.sh`; `ENABLE_SSE`, port 9423 | free | doc-only alternatives (not enabled) | GitHub repos exist; not run | as JobSpy |
| **Apify MCP** (job actors preloaded) | `fetch-actor-details` plus the actors in the next table (JobSpy, hiring.cafe, job-search MCP, Workday monitor) | OAuth or `Authorization: Bearer <APIFY_TOKEN>`; pay per result | .mcp.json (http, OAuth), setup.sh (Bearer when `APIFY_TOKEN` is set, else `--oauth`), claude.ai custom connector; jobsweep `apify` adapter (metered) | **needs auth**: 401 with `WWW-Authenticate: Bearer`; OAuth metadata published (dynamic client registration); all 4 actors exist in the Apify store; the combined `?tools=` URL wasn't exercised (needs a token) | T&S, intel, sanctions at tech and finance companies (hiring.cafe, 46 ATS); specialty insurance and banks on Workday |
| **TheirStack** | jobs and company search (`search_jobs`, `search_companies`, company lists, saved searches, webhooks, credits) | OAuth (default) or `Authorization: Bearer <key>`; consumes API credits | .mcp.json (http, OAuth), setup.sh, claude.ai custom connector; jobsweep `theirstack` adapter (metered) | **needs auth**: 401; OAuth metadata at `/.well-known/oauth-protected-resource/mcp` | sanctions and fincrime, T&S, geopolitical risk (company-level discovery) |
| **HasData Indeed** | Indeed listings and details (`hasdata_indeed_listing_getJobListings`, `hasdata_indeed_job_getJobDetails`) | `x-api-key` header or OAuth; free 1,000 credits (about 100 calls) a month | .mcp.json (http, OAuth), setup.sh (`x-api-key` when `HASDATA_API_KEY` is set), Desktop (stdio `npx -y @hasdata/indeed-mcp`), claude.ai custom connector; jobsweep `hasdata_indeed` adapter (metered) | **responds / tool calls need auth**: initialize and tools/list OK without a key, tool call answered 401; npm `@hasdata/indeed-mcp` 1.0.8 and PyPI `hasdata-indeed-mcp` 1.0.8 exist | Indeed coverage for unattended GitHub Actions sweeps: all lanes |
| **ATS jobs** (benkrasin/ats-jobs-mcp) | single-company Greenhouse/Lever/Ashby boards (`list_company_jobs`, `detect_ats`); `search_jobs_all_companies` over 16,000+ boards | single-company tools free; all-companies search needs `APIFY_TOKEN` (about $1 per 1k jobs) | .mcp.json ✓ (`npx -y github:benkrasin/ats-jobs-mcp mcp`, `APIFY_TOKEN=${APIFY_TOKEN:-}`), setup.sh, Desktop | repo exists and its README confirms the command and tools. **Never use the bare npm name `ats-jobs-mcp`**: it's an unrelated package (0.1.1, from VovikP/ats-jobs-mcp). Not run here: this sandbox's policy blocks executing code fetched from GitHub | spot checks: Dataminr-style intel firms, T&S at AI labs, risk consultancies |
| eosphor/ats-jobs-mcp | 10 ATS incl. Workday, Personio, Recruitee, Teamtailor, Workable, SmartRecruiters, Breezy | local `PORT=8080 python src/server.py` serves `http://localhost:8080/mcp`; hosted on MCPize: free 100 calls/month, Pro $12/month | doc-only (local clone) | repo exists; README checked | specialty insurance and banks on Workday |
| LinkedIn: ynsz-66/mcp-linkedin | LinkedIn public guest endpoints (search, batch details) | free; rate-limited by LinkedIn | doc-only (local install; next section) | repo exists; README: `HOST=127.0.0.1 uv run server.py` serves `http://127.0.0.1:8080/mcp-linkedin`, or `MCP_TRANSPORT=stdio` | intel and security, geopolitical, comms |
| LinkedIn: administrativetrick/linkedin-mcp | LinkedIn (Node build) | free | doc-only (local build) | repo exists; not run | as above |
| **federal-compass-mcp** | USAJOBS API: `search_jobs`, `get_job_details`, `compare_jobs`, `save_cv`, `explain_federal_concept`, `calculate_salary`, `find_matching_jobs`, `check_qualification`, `extract_ksa` | free USAJOBS key + email; Node 22+ | .mcp.json (`${USAJOBS_API_KEY:-}`, `${USAJOBS_EMAIL:-}`), setup.sh (when keys are set), Desktop | npm 0.1.10 (engines node >= 22); source reads exactly `USAJOBS_API_KEY` and `USAJOBS_EMAIL`; not run | federal-pathways (KSA extraction, qualification checks), federal-investigator |
| amv2/usajobs-monitor | scheduled USAJOBS search with eligibility screening; MCP tools `search_jobs`, `screen_eligibility`, `explain_match` | free USAJOBS key; Python 3.11+ | doc-only (`pipx install "usajobs-monitor[mcp]"`, `usajobs-monitor init --profile default`, then `usajobs-monitor mcp --profile default`) | PyPI 0.4.0 exists; README checked | federal-pathways (hiring-path eligibility) |
| **Adzuna MCP** | Adzuna search plus salary analytics: `search_jobs`, `get_categories`, `get_salary_histogram`, `get_top_companies`, `get_geodata`, `get_salary_history`, `get_api_version` | free app id + key (about 250 calls/day) | .mcp.json (`uvx --from git+https://github.com/folathecoder/adzuna-job-search-mcp@e00a595 adzuna-mcp`), setup.sh, Desktop; jobsweep `adzuna` adapter | **`uvx adzuna-mcp` doesn't work: the package is not on PyPI (404)**, although its README says it is. The source repo defines script `adzuna-mcp` and the 7 tools, so it's pinned to the reviewed commit. npm `adzuna-mcp` 0.1.0 is a different Node server (one tool, country hard-coded to `gb`), so it isn't used | keyword coverage US plus UK; salary benchmarks for offers |
| **Exa** | neural web search and fetch (`web_search_exa`, `web_fetch_exa`) | keyless use works (initialize and tools/list); OAuth metadata published (auth.exa.ai) | .mcp.json ✓ (http), setup.sh, claude.ai directory connector; also `claude plugin install exa@claude-plugins-official` | **responds**: initialize and tools/list OK without a key; the plugin route wasn't verified here | think tanks, IGOs, law-firm analyst programs, fellowships, policy research |
| **Firecrawl** | crawl and read a careers page | native claude.ai connector (sign in, choose a team); credits | claude.ai | not probed (claude.ai-only setup) | think-tank and agency pages without an API |
| **Playwright MCP** | headless browser for JS-heavy career sites | free | .mcp.json ✓ (`npx -y @playwright/mcp@latest`), setup.sh, Desktop | npm `@playwright/mcp` 0.0.83 exists; not run | "check by hand" employers (iCIMS, Taleo, custom) |
| **Brave Search** | web search | `BRAVE_API_KEY` (2,000 free queries) | setup.sh and Desktop, using **`@brave/brave-search-mcp-server`** | npm `@modelcontextprotocol/server-brave-search` 0.6.2 is **deprecated** ("Package no longer supported"); successor `@brave/brave-search-mcp-server` 2.1.4 (stdio default, `BRAVE_API_KEY`) | career-page discovery |
| Dreamwork (`npx -y @dreamworkhq/mcp`) | tech-focused job search, ranking, tailoring and tracking; it applies only after you confirm each one | works without a key; `DREAMWORK_API_KEY` optional; Node 22+ | doc-only. It's tech-focused, and this project never applies on the user's behalf | npm 1.9.1 exists | T&S and AI policy at tech companies, at best |
| career-ops (`npx @santifer/career-ops init`) | Claude Code CLI job-search pipeline | free | doc-only (optional complementary tool) | npm 1.38.0 (Node ^22.13 or >= 23.4); repo now career-ops-hq/career-ops | n/a (separate workflow) |
| proficiently job-search skill | hiring.cafe search through Claude in Chrome | free plugin; needs Chrome and the extension | doc-only: `claude plugin marketplace add https://github.com/proficientlyjobs/proficiently-claude-skills.git`, then `claude plugin install proficiently@proficiently` | repo exists; its job-search skill drives hiring.cafe via Claude in Chrome. The plugin also ships an **apply** skill, which isn't used here | T&S, intel, sanctions (hiring.cafe coverage) |
| claude-job-hunt plugin | end-to-end job hunt skills (board scans, ledger, cover letters) | free | doc-only: `/plugin marketplace add dominiquevienne/claude-job-hunt`, then `/plugin install claude-job-hunt@claude-job-hunt` | repo exists; README checked | n/a (separate workflow) |
| **Claude in Chrome** | lets Claude browse career sites that have no API | paid plans; Chrome only | Chrome extension (steps below) | n/a | employers listed as "check by hand" (`jobsweep employers --manual`) |
| **NYC Jobs open data** (dataset `kpav-sd4t`) | every current City of New York posting (JSON/CSV) | free; optional `SOCRATA_APP_TOKEN` | jobsweep `nycjobs` adapter (no connector needed) | **responds**: `https://data.cityofnewyork.us/resource/kpav-sd4t.json` returned 2,594 rows | nyc-integrity (DOI, CCRB), nyc-mayors-office, emergency-management (NYCEM), policy-research |

Apify actors preloaded in the `apify` server URL
(`https://mcp.apify.com/?tools=fetch-actor-details,aakash19/jobspy,blackfalcondata/hiringcafe-scraper,digital_influx/job-search-mcp,piracema_labs/workday-jobs-monitor`):

| Actor | What | Price (from the user's research) | Store check |
|---|---|---|---|
| `aakash19/jobspy` | JobSpy (LinkedIn, Indeed, Glassdoor, ...) as an actor | pay per use | exists (`/v2/acts/aakash19~jobspy`) |
| `blackfalcondata/hiringcafe-scraper` | hiring.cafe: 2.8M listings from 46 ATS | about $1.15 per 1k results | exists, pay-per-event |
| `digital_influx/job-search-mcp` | `search_jobs`, `list_company_jobs`, `get_job_details` over 20k+ career sites | about $2 per 1k jobs | exists (MCP-server actor), pay-per-event |
| `piracema_labs/workday-jobs-monitor` | Workday career sites plus new-job alerts | about $1 per 1k | exists, pay-per-event |

## Quotas and costs at a glance

| Source | Free allowance | Paid | Where jobsweep caps it |
|---|---|---|---|
| Pipeworx USAJOBS | about 50 calls/day per egress IP (shared by the MCP server and jobsweep's fallback) | own key via `?_apiKey=` | `usajobs` query budget: 12 on the fallback, 200 with a direct USAJOBS key |
| USAJOBS API (direct) | free key | n/a | 200 queries/sweep |
| HasData | 1,000 credits = about 100 calls/month | credits | metered: only with `--include-metered` |
| Apify | depends on your Apify plan | pay per result (actor prices above) | metered |
| TheirStack | depends on your TheirStack plan | API credits | metered |
| Adzuna | about 25/min, 250/day | n/a | adapter budget |
| Brave | 2,000 free queries | paid tiers | n/a |
| MCPize (eosphor) | 100 calls/month | $12/month | n/a |

## claude.ai (web)

Full steps are in [`integrations/claude_ai_connectors.md`](../integrations/claude_ai_connectors.md). In short:

1. **Directory connectors:** in a chat, go to **Search and tools > + Add connectors** and add **Indeed**
   (sign in with Indeed), **Exa** and **Firecrawl** (sign in, choose a team). Jobs and Careers and Workopia
   are already connected.
2. **Custom connectors:** go to **Settings > Connectors > Add custom connector** and add Pipeworx USAJOBS
   (No sign in), plus Apify, TheirStack and HasData Indeed (Sign in now, or a request header with the API
   key; header values are sent verbatim, so include `Bearer `).
3. Rules:
   - Remote servers must be publicly reachable from Anthropic's cloud.
   - The **Free plan allows one custom connector**; choose Pipeworx USAJOBS.
   - On Team and Enterprise, owners add org connectors under **Organization settings > Connectors**.
4. Turn connectors on per chat in **Search and tools**.

## Claude Desktop

1. Connectors added on claude.ai (Indeed, Exa, Firecrawl, custom connectors) appear in Desktop
   automatically. You don't need to duplicate them in the config file.
2. Local servers go in `claude_desktop_config.json`:
   - On macOS it's at `~/Library/Application Support/Claude/claude_desktop_config.json`.
   - On Windows it's at `%APPDATA%\Claude\claude_desktop_config.json`.
   - Or open it from **Settings > Developer > Edit Config**.
3. Print the template with this checkout's path filled in:
   ```bash
   jobsweep/integrations/claude_code_setup.sh --print-desktop-config
   ```
   Merge its `mcpServers` entries into your file.
4. Replace the `<...>` placeholders (USAJOBS, Adzuna, HasData, Brave keys) or delete the entries you don't
   use. Leave `APIFY_TOKEN` empty unless you want ats-jobs' paid all-companies search. Desktop doesn't
   expand `${VAR}`, so values are literal.
5. Delete `pipeworx-usajobs` from the file if you already added it as a claude.ai custom connector.
6. Quit and restart Claude Desktop. The servers then appear under the chat's tools menu.

## Claude Code

1. **Project servers.**
   - Starting `claude` at the repo root loads `.mcp.json`. `.claude/settings.json` pre-approves the
     servers that need no secrets: `jobsweep`, `pipeworx-usajobs`, `exa`, `jobspy`, `ats-jobs` and
     `playwright`.
   - The rest stay pending until you approve them. Approve them in `/mcp`, or list them in your personal
     `.claude/settings.local.json` under `enabledMcpjsonServers`:
     - `apify`, `theirstack` and `hasdata-indeed` (OAuth, metered)
     - `adzuna` and `federal-compass` (keys)
2. **Keys.**
   - `${VAR:-}` placeholders expand from the shell environment, and expand to empty when unset, so the
     file always parses.
   - Export your keys first: `set -a; . jobsweep/.env; set +a; claude`.
3. **OAuth.** In Claude Code run `/mcp`, select `apify`, `theirstack` or `hasdata-indeed`, then
   **Authenticate**.
4. **User scope** (available in every directory, with API-key headers instead of OAuth):
   ```bash
   jobsweep/integrations/claude_code_setup.sh --dry-run    # preview (keys masked)
   jobsweep/integrations/claude_code_setup.sh              # add; idempotent, --force to replace
   jobsweep/integrations/claude_code_setup.sh --oauth      # also add metered servers without keys
   jobsweep/integrations/claude_code_setup.sh --scope local  # keyed variants win inside this repo
   ```
   Precedence for the same name is local > project > user. So inside this repo the project `.mcp.json`
   entries shadow user-scope ones, unless you use `--scope local`.
5. **claude.ai connectors in Claude Code.** When you're signed in with your claude.ai account, your claude.ai
   connectors (Indeed, Jobs and Careers, Workopia, ...) appear in Claude Code as `mcp__Indeed__search_jobs`,
   etc. The jobsweep skill uses them and imports their output.
6. **Skill.** `.claude/skills/jobsweep/SKILL.md` loads when you ask Claude to run or review a job sweep.
7. **Optional plugins** (not required):
   - `claude plugin install exa@claude-plugins-official`
   - career-ops: `npx @santifer/career-ops init`
   - proficiently: marketplace `https://github.com/proficientlyjobs/proficiently-claude-skills.git`
   - claude-job-hunt: `/plugin marketplace add dominiquevienne/claude-job-hunt`
8. **Optional local servers:**
   - LinkedIn (ynsz-66): `git clone https://github.com/ynsz-66/mcp-linkedin && cd mcp-linkedin && HOST=127.0.0.1 uv run server.py`,
     then `claude mcp add --transport http linkedin http://127.0.0.1:8080/mcp-linkedin`.
   - eosphor ATS: `PORT=8080 python src/server.py`, then `claude mcp add --transport http ats-eosphor http://localhost:8080/mcp`.
     Change one of the ports if you run both.
   - usajobs-monitor: `claude mcp add usajobs -- /path/to/usajobs-monitor mcp --profile default`.

## Chrome (Claude in Chrome)

1. Claude in Chrome needs a paid plan and Google Chrome. Install the **Claude in Chrome** extension from the
   Chrome Web Store and sign in.
2. In claude.ai, go to **Settings > Customize > Connectors > Claude in Chrome > Configure** to set it up.
3. Get the list of employers without a sweepable API: run `jobsweep employers --manual`, or check the "check
   by hand" panel on the board. Then ask Claude in Chrome to open each careers URL and list entry-level
   roles for your lanes.
4. Paste or save its findings as a markdown table with a `Title` column, then run
   `jobsweep import FILE --origin chrome`.

## Projects (claude.ai)

Create a project named **Job sweeps** at claude.ai/projects. Paste
[`integrations/project_instructions.md`](../integrations/project_instructions.md) (the text below its cut
line) into the project instructions. Upload `catalog/lanes.yaml` and `catalog/profile.yaml` as knowledge.

## Scheduled tasks

Open **Scheduled > New task** and set it to Daily (or Weekdays) at **7:40 AM America/New_York**. Enable
Indeed, Jobs and Careers, Workopia, Pipeworx USAJOBS and Google Drive, and paste
[`integrations/scheduled_task_prompt.md`](../integrations/scheduled_task_prompt.md).

The task:
- runs remotely and uses connectors only;
- appends a dated section to the Google Doc **"Job sweep log"**;
- writes tables that `jobsweep import` can read later.

Local, credentialed sweeps of the employer boards run in GitHub Actions instead (next section).

## GitHub Actions secrets (names only)

Add these in the repository under **Settings > Secrets and variables > Actions**, or with
`gh secret set NAME`. Each is optional; missing ones disable that source:

- `USAJOBS_API_KEY`, `USAJOBS_EMAIL`, `PIPEWORX_API_KEY`
- `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`
- `THEIRSTACK_API_KEY`, `HASDATA_API_KEY`, `APIFY_TOKEN` (metered: used only when the workflow runs with
  include-metered)
- `SOCRATA_APP_TOKEN`

These are exactly the credentials in `jobsweep.config.ENV_VARS` and `jobsweep/.env.example`. The workflow
details are in `docs/GITHUB.md`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Claude Code shows a project server as "Pending approval" | Approve it in `/mcp`, or add it to `enabledMcpjsonServers` in `.claude/settings.local.json` |
| A keyed server starts but every call fails | The key wasn't in the shell that launched `claude`. `${VAR:-}` expands to empty, so run `set -a; . jobsweep/.env; set +a` and restart |
| `apify` / `theirstack` / `hasdata-indeed` say "needs authentication" | Run `/mcp`, choose the server, then Authenticate (OAuth). Or rerun the setup script with the key set, which adds a header-based variant. Inside the repo, use `--scope local` |
| `jobsweep` server fails to start in Claude Code | `uv` isn't on PATH, or this is the first run (uv creates `jobsweep/.venv`). Run `uv run --directory jobsweep jobsweep doctor` once in a terminal. Without uv, point `command` at `.venv/bin/jobsweep-mcp` |
| Claude Desktop: "spawn uv ENOENT" / "spawn npx ENOENT" | Desktop doesn't use your shell PATH. Put absolute paths in `command` (`which uv`, `which npx`) |
| `uvx adzuna-mcp` fails with "not found" | The package isn't on PyPI. Use the `--from git+https://github.com/folathecoder/adzuna-job-search-mcp@<commit>` form used here |
| ats-jobs installs the wrong server | Use `github:benkrasin/ats-jobs-mcp`, never the bare npm name `ats-jobs-mcp` |
| federal-compass exits immediately | It needs Node 22+ and both `USAJOBS_API_KEY` and `USAJOBS_EMAIL` |
| Pipeworx returns quota or rate-limit errors | The free 50 calls/day is shared per IP, and the jobsweep USAJOBS fallback uses it too. Set `PIPEWORX_API_KEY`, or get a direct USAJOBS key (jobsweep then stops using Pipeworx) |
| Brave server fails to install | `@modelcontextprotocol/server-brave-search` is deprecated; use `@brave/brave-search-mcp-server` |
| claude.ai custom connector won't connect | The URL must be public HTTPS (no localhost). Free plan: only one custom connector. Header values are sent verbatim, so include `Bearer ` |
| Indeed connector not in Claude Code | It's claude.ai-only. Sign in to Claude Code with the same claude.ai account; it has no URL for `.mcp.json` |
| Imported connector output gives 0 jobs | Pass the raw tool output (not a summary). Markdown tables need a `Title` column. Indeed markdown, JSON and numbered lists are auto-detected; force one with `--format md|json|csv` |
| LinkedIn / JobSpy return nothing | The sites rate-limit scraping. Lower `results_wanted`, wait, or use the Indeed connector instead |

## Probe log (2026-10-25)

How the statuses above were checked:

- Packages:
  - `npm view <pkg> version` for mcp-remote, `@pipeworx/mcp-usajobs`, `@hasdata/indeed-mcp`,
    federal-compass-mcp, `@playwright/mcp`, `@modelcontextprotocol/server-brave-search` (deprecated),
    `@brave/brave-search-mcp-server`, `@dreamworkhq/mcp`, `@santifer/career-ops`, ats-jobs-mcp (unrelated)
    and adzuna-mcp (unrelated, Node).
  - `https://pypi.org/pypi/<pkg>/json` for mcp-jobspy (1.4.1), hasdata-indeed-mcp (1.0.8),
    usajobs-monitor (0.4.0) and adzuna-mcp (**404**).
- Remote MCP: a POST of `initialize` (protocol 2025-06-18) to each URL, then tools/list through the MCP
  Python client:
  - Pipeworx USAJOBS and the gateway: 200.
  - Exa: 200.
  - HasData: 200 (tool calls 401).
  - Apify: 401.
  - TheirStack: 401.
  - OAuth discovery via `/.well-known/oauth-protected-resource[/mcp]`.
- stdio: `uvx mcp-jobspy@latest` was started and listed its tools. The GitHub-hosted and npm stdio servers
  weren't executed in the build sandbox (its policy blocks running externally fetched code). They were
  checked by registry metadata and source/README review instead.
- GitHub repos: `git ls-remote` for every repo named above.
- NYC Jobs: Socrata `count(*)` on `kpav-sd4t` returned 2,594 rows.
