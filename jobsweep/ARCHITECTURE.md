# jobsweep architecture and contracts

jobsweep runs **lane-based job sweeps** over employer ATS boards (Greenhouse, Lever, Ashby, Workday, …),
government feeds (USAJOBS, NYC Jobs, NY State), and keyword aggregators (Adzuna, TheirStack, Indeed via
HasData, JobSpy, Apify). It classifies every posting against the candidate's **lanes** (taxonomy), scores
it for entry-level fit and location, stores it in SQLite, and serves it through:

* a **web board** (FastAPI + vanilla JS, also exportable as a static site for GitHub Pages),
* a **CLI** (`jobsweep …`),
* an **MCP server** (`jobsweep-mcp`, stdio) so Claude Code / Claude Desktop can search, sweep, triage,
  and import jobs found through claude.ai connectors (Indeed connector etc.),
* **GitHub Actions** (scheduled sweep → state branch → Pages board → issue digest).

Everything lives under `jobsweep/` except files that must sit at the repository root
(`.mcp.json`, `.github/workflows/`, `.claude/skills/`).

Python ≥ 3.10, `from __future__ import annotations` everywhere, async adapters on one shared
`httpx.AsyncClient`. Tests: `pytest` + `respx` (no network in default tests; live tests are marked
`@pytest.mark.live`). Lint: `ruff check src tests`.

Run from `jobsweep/`: `uv venv && uv pip install -e '.[dev]'` (or use the repo `.venv`:
`/home/user/gradcafe_data/.venv/bin/python`, already provisioned with all deps + python-jobspy).

---------------------------------------------------------------------------------------------------

## 1. Module map and file ownership

| File | Owner | Purpose |
|---|---|---|
| `src/jobsweep/models.py` | core (done) | `Job`, `Employer`, `SearchQuery`, `Profile`, `SourceResult`, `JOB_STATUSES` |
| `src/jobsweep/config.py` | core (done) | `Settings` (env + `.env` files), `ENV_VARS`, paths |
| `src/jobsweep/http.py` | core (done) | `make_client`, `fetch_json`, `fetch_text`, `HostLimiter`, `HTTPError` |
| `src/jobsweep/text.py` | core (done) | `html_to_text`, `iso_date`, `relative_to_iso`, `parse_salary`, `truncate` |
| `src/jobsweep/employers.py` | core (done) | `EmployerRegistry` over `catalog/employers/*.yaml` |
| `src/jobsweep/sources/base.py` | core (done) | `Source` ABC, `SourceContext`, `SourceError`, `SourceUnavailable` |
| `src/jobsweep/sources/__init__.py` | core (done) | lazy registry `SOURCE_PATHS`, `get_source_class`, … |
| `src/jobsweep/sources/{greenhouse,lever,ashby,workday,smartrecruiters,workable,recruitee}.py`, `sources/detect.py` | ats-core | board adapters + ATS detection |
| `src/jobsweep/sources/{personio,breezy,bamboohr,paylocity,adp,ultipro,oracle_hcm,jobvite,rippling,pinpoint,teamtailor}.py` | ats-extended | board adapters |
| `src/jobsweep/sources/{usajobs,nycjobs,nysjobs}.py` | gov | government feeds |
| `src/jobsweep/sources/{adzuna,theirstack,hasdata_indeed,jobspy_source,apify,manual_import}.py` | aggregators | search adapters + import |
| `src/jobsweep/catalog/lanes.yaml`, `src/jobsweep/catalog/profile.yaml` | taxonomy-data | the lanes taxonomy |
| `src/jobsweep/taxonomy.py` | classifier | load lanes, classify/score jobs, search plan |
| `src/jobsweep/store.py`, `sweep.py`, `export.py`, `cli.py`, `__main__.py` | engine | SQLite store, sweep orchestrator, exports, CLI |
| `src/jobsweep/web/app.py`, `src/jobsweep/web/static/*` | web | FastAPI API + UI (same UI powers the static export) |
| `src/jobsweep/mcp_server.py` | mcp | FastMCP stdio server |
| repo-root `.mcp.json`, `jobsweep/integrations/*`, `jobsweep/docs/CONNECTORS.md`, `.claude/skills/jobsweep/SKILL.md`, `jobsweep/.env.example` | connectors | linking Claude Code / Desktop / claude.ai connectors |
| repo-root `.github/workflows/jobsweep-*.yml`, `jobsweep/docs/GITHUB.md` | github | CI, scheduled sweep, Pages, issue digest |
| `src/jobsweep/catalog/employers/<group>.yaml` | research-<group> | verified employer registry |
| `tests/test_<module>.py`, `tests/fixtures/<area>/…` | owner of the module | tests |

Never edit a file you do not own; if a contract is wrong, note it in your final report.

## 2. Data model (src/jobsweep/models.py)

`Job` source fields (adapters): `source, source_id, title, company, url, location, remote, description,
posted_at, closes_at, salary_min, salary_max, salary_currency, salary_period, employment_type,
department, seniority, grade, series, employer_key, tags, extra`.
Classification fields (taxonomy): `lanes, groups, score (0-100), lane_scores, entry_level,
years_required, match_reasons`. `uid = f"{source}:{source_id}"`. `fingerprint` = cross-source dedupe
key. `to_dict()` includes `uid`. `from_dict()` ignores unknown keys.

Adapter rules:
* `description` is **plain text** (use `text.html_to_text`), ≤ 12k chars.
* `posted_at`/`closes_at` via `text.iso_date` / `text.relative_to_iso`.
* `location` human readable; several locations joined with `"; "`.
* `remote` True only when the source says remote; False for on-site/hybrid; None if unknown.
* For board adapters set `employer_key = employer.key` and `company = employer.name` (unless the
  board says otherwise for multi-brand boards).
* `employment_type` normalised to one of: `full-time, part-time, contract, temporary, internship,
  fellowship, volunteer, per-diem, other`.
* Put `"internship"`, `"fellowship"`, `"graduate-program"`, `"pathways"`, `"recent-graduates"`,
  `"student"`, `"reservist"` in `tags` when the source exposes them.

## 3. Sources

`Source` subclasses (see `sources/base.py`): class attrs `name, kind ("board" | "search" |
"board+search"), label, requires_env, requires_module, metered, docs_url, max_queries`; classmethods
`availability(settings)`, `query_budget(settings)`; async `fetch_board(employer)`, `search(query)`,
`enrich(job)`; helpers `get_json`, `post_json`, `get_text` (always go through these for retries and
per-host limits). Constructed as `Cls(ctx: SourceContext)`.

### 3.1 Board formats (`Employer.ats` / `Employer.board`)

| ats | board value | API |
|---|---|---|
| `greenhouse` | board token, e.g. `dataminr` | `GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true` (content is HTML-escaped HTML) |
| `lever` | site name, e.g. `palantir`; prefix `eu:` for EU instance | `GET https://api.lever.co/v0/postings/{board}?mode=json` (`api.eu.lever.co` for `eu:`) |
| `ashby` | job board name, e.g. `openai` | `GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true` |
| `workday` | `tenant/wdN/site`, e.g. `rb/wd5/FRS` | `POST https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` body `{"appliedFacets":{},"limit":20,"offset":N,"searchText":""}`; detail `GET https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}`; public URL `https://{tenant}.{wdN}.myworkdayjobs.com/{site}{externalPath}` |
| `smartrecruiters` | company identifier, e.g. `Visa` | `GET https://api.smartrecruiters.com/v1/companies/{board}/postings?limit=100&offset=N`, detail `/postings/{id}` |
| `workable` | account subdomain | `POST https://apply.workable.com/api/v3/accounts/{board}/jobs` (paginate with `token`), detail `GET https://apply.workable.com/api/v2/accounts/{board}/jobs/{shortcode}` |
| `recruitee` | company subdomain | `GET https://{board}.recruitee.com/api/offers/` |
| `personio` | subdomain (optionally `sub.jobs.personio.com`) | `GET https://{board}.jobs.personio.de/xml` (XML) |
| `breezy` | subdomain | `GET https://{board}.breezy.hr/json` |
| `bamboohr` | subdomain | `GET https://{board}.bamboohr.com/careers/list` (JSON), detail `/careers/{id}/detail` |
| `paylocity` | company GUID | `GET https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{board}` |
| `adp` | `cid` (WorkforceNow), optionally `cid|ccId` | `GET https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions?cid={cid}&lang=en_US&$top=100&$skip=N` |
| `ultipro` | `tenantAlias/boardGuid` | `POST https://recruiting.ultipro.com/{tenant}/JobBoard/{guid}/JobBoardView/LoadSearchResults` |
| `oracle` | `host/siteNumber`, e.g. `jpmc.fa.oraclecloud.com/CX_1001` | `GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},limit=200,offset=N,keyword="…"` |
| `jobvite` | company id/slug | `GET https://jobs.jobvite.com/{board}/jobs` (HTML) or feed |
| `rippling` | board slug | `GET https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs` |
| `pinpoint` | subdomain | `GET https://{board}.pinpointhq.com/postings.json` |
| `teamtailor` | subdomain | public careers RSS/JSON if exposed (`https://{board}.teamtailor.com/jobs.rss`) |
| `other` / `null` | — | not sweepable; `careers_url` + `ats_vendor` shown in UI as "check by hand" |

Adapters that support server-side search (workday, oracle, smartrecruiters, adp, ultipro) must honour
`employer.search_terms` (one search per term, results de-duplicated) and `employer.max_jobs`
(default 400 for Workday/Oracle, 1000 otherwise). All adapters ignore `title_filter` (the sweep applies it).

### 3.2 Search sources (`search(query: SearchQuery)`)

| name | credentials | notes |
|---|---|---|
| `usajobs` | `USAJOBS_API_KEY` + `USAJOBS_EMAIL` → direct `https://data.usajobs.gov/api/search` (headers `Host`, `User-Agent: email`, `Authorization-Key`). **Without them falls back to the Pipeworx gateway** `POST https://gateway.pipeworx.io/v1/tools/usajobs_search` (args `keyword, location, position_title, organization, pay_grade_low, pay_grade_high, remote, results_per_page, page`; free ≈50 calls/day; optional `PIPEWORX_API_KEY` as `?_apiKey=`). `query_budget` = 200 direct / 12 fallback. Honours `extra.series` (JobCategoryCode), `extra.hiring_path` (e.g. `graduates`, `student`, `public`), `extra.grade_low/high`, `extra.organization`. Tags `pathways`/`recent-graduates` from `HiringPath`/title. |
| `nycjobs` | none (optional `SOCRATA_APP_TOKEN`) | NYC Open Data Socrata dataset `kpav-sd4t` (`https://data.cityofnewyork.us/resource/kpav-sd4t.json`, SoQL `$where/$q/$limit/$order`). Keyword query via `$q`, `extra.agencies`, `extra.career_level`. uid = `job_id` (+ `posting_type` suffix when both internal/external exist). |
| `nysjobs` | none | StateJobsNY public vacancy listing (`https://statejobs.ny.gov/public/vacancyTable.cfm`) — HTML parse, best effort. |
| `adzuna` | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | `GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}` |
| `theirstack` | `THEIRSTACK_API_KEY` (metered) | `POST https://api.theirstack.com/v1/jobs/search` |
| `hasdata_indeed` | `HASDATA_API_KEY` (metered, free 100 calls/mo) | `GET https://api.hasdata.com/scrape/indeed/listing?keyword=&location=` header `x-api-key` |
| `jobspy` | optional module `jobspy` | `jobspy.scrape_jobs` in a thread (Indeed, LinkedIn, Google, ZipRecruiter, Glassdoor) |
| `apify` | `APIFY_TOKEN` (metered) | run actors synchronously (`run-sync-get-dataset-items`): hiring.cafe scraper, job-search actor |
| `import` | none | `ImportSource.parse(payload, origin)` turns JSON / CSV / **Indeed-connector markdown** into `Job`s (`source="import"`, `source_id=f"{origin}:{id}"`, tag `origin`). Not used by `search`. |

## 4. Taxonomy (catalog/lanes.yaml + taxonomy.py)

### 4.1 Group and lane ids (fixed — every module and the employer registry use these)

```
specialty-insurance: political-risk, political-violence-terrorism, war-risk, kidnap-ransom, special-risks,
  trade-credit-surety, transactional-liability, financial-lines, fine-art-specie, intangibles, aviation,
  marine-cargo, reinsurance-broking, insurance-fraud-siu
intelligence-security: strategic-intelligence, threat-intelligence, protective-intelligence, travel-risk-crisis,
  gsoc-watch, corporate-security, osint, counterterrorism, regional-research-specialist, defense-intel-data
investigations-dd: corporate-intelligence, due-diligence, beneficial-ownership, fraud-investigations,
  forensic-investigations, law-firm-analyst, litigation-case-analyst, public-defense-investigation, nonprofit-grant-dd
geopolitical-risk: country-sovereign-risk, geopolitical-analysis, political-forecasting, geoeconomics,
  economic-security, supply-chain-forced-labor, risk-consulting, primary-research-expert-networks, investment-research-dd
sanctions-fincrime: sanctions, export-controls, aml-kyc, fincrime-intelligence, illicit-finance,
  crypto-investigations, investigative-compliance, market-surveillance
trust-safety: trust-safety-policy, policy-enforcement-ai-safeguards, ai-policy-forecasting, election-integrity,
  disinformation, human-rights
policy-research: national-security, foreign-policy, think-tank-research, international-orgs,
  policy-legislative-analyst, tech-regulatory-policy, criminal-justice-policy, social-policy-advocacy,
  urban-planning-research, research-writing
strategic-comms: strategic-comms-advisory, crisis-comms, public-government-affairs, reputation-media-analysis,
  insights-monitoring
government: district-attorney, nyc-integrity, nyc-mayors-office, nys-agencies, ny-fed, emergency-management,
  federal-pathways, federal-investigator
entry-routes: graduate-programs, analyst-programs, fellowships, entry-level-analyst
earlier-lanes (active: false): program-evaluation, philanthropy, digital-rights, economic-consulting,
  broadcast-journalism, real-estate
```

`entry-routes` lanes are *modifiers*: they match programme/level vocabulary and boost entry-level fit; a job
whose only match is an entry-route lane is kept but ranked below domain matches. `earlier-lanes` are
classified (so old interests still get labelled) but excluded from search plans and hidden by default.

### 4.2 lanes.yaml schema

```yaml
version: 1
groups:
  - id: specialty-insurance
    name: "Specialty insurance (underwriting and broking)"
    active: true
    lanes:
      - id: political-risk
        name: "Political risk (incl. credit and political risk)"
        active: true
        title:   ["political risk", "credit and political risk", "CPRI"]  # strong: matched in the job title
        phrases: ["political risk insurance", "contract frustration", "expropriation"]  # title or description
        context: ["underwriter", "underwriting", "broker", "Lloyd's"]  # support only, never sufficient alone
        exclude: ["political campaign"]  # title phrases that veto the lane
        queries: ["political risk underwriter", "political risk analyst"]  # keyword-search plan (generic sources)
        usajobs: {keywords: [...], series: ["0132"], hiring_path: ["graduates"], grade_low: "05", grade_high: "09", organization: "HSCB"}  # optional
        nycjobs: {keywords: [...], agencies: ["DEPARTMENT OF INVESTIGATION"]}  # optional
```

Terms are case-insensitive whole-word/phrase matches; a term wrapped in slashes (`"/\\bK&R\\b/"`) is a raw regex.

### 4.3 taxonomy.py API

```python
@dataclass class Lane:  id, name, group, active, title, phrases, context, exclude, queries, usajobs, nycjobs
@dataclass class Group: id, name, active, lanes: list[Lane]

class Taxonomy:
    groups: list[Group]; lanes: dict[str, Lane]; profile: Profile
    @classmethod
    def load(cls, path=None, profile: Profile | str | Path | None = None) -> Taxonomy
    def lane(self, lane_id) -> Lane | None
    def active_lanes(self) -> list[Lane]
    def classify(self, job: Job, employer: Employer | None = None) -> Job   # fills classification fields in place, returns job
    def title_signal(self, title: str, employer: Employer | None = None) -> float  # cheap pre-score used before enrich()
    def search_plan(self, lane_ids=None, source: str | None = None, posted_within_days=None) -> list[SearchQuery]
    def to_dict(self) -> dict   # {"groups":[{"id","name","active","lanes":[{"id","name","active","group","queries"}]}]}
```

Scoring contract: `score` 0–100 = domain relevance (best lane) + entry-level fit + location fit +
recency; `entry_level` False when title is senior (Senior/Sr/Lead/Principal/Director/Head of/VP/Chief/Partner/
Managing…) or `years_required > profile.max_years_experience`; True on entry vocabulary (Analyst I,
Associate, Assistant, Coordinator, Graduate, Junior, Entry, Early Career, Fellow, Trainee, Rotational,
Pathways, GS-5/7/9, NYC "Entry-Level"). `match_reasons` lists short strings like
`"title: 'political risk' → political-risk"`, `"employer prior: Beazley → specialty-insurance"`,
`"entry: 'Analyst' in title"`, `"senior: 'Director' in title"`, `"location: New York"`.

## 5. Store (store.py) — SQLite

```python
class Store:
    def __init__(self, path: str | Path)            # creates parent dir + schema; ":memory:" allowed
    def start_run(self, meta: dict) -> int
    def finish_run(self, run_id: int, stats: dict) -> None
    def upsert_jobs(self, jobs: list[Job], run_id: int | None = None) -> dict  # {"new": [uid...], "updated": int}
    def mark_board_complete(self, employer_key: str, seen_uids: set[str]) -> int  # deactivate board jobs not seen
    def expire(self, older_than_days: int = 30) -> int  # deactivate search-sourced jobs not seen recently or past closes_at
    def query(self, *, q=None, lanes=None, groups=None, sources=None, statuses=None, entry_only=False,
              include_unknown_entry=True, min_score=None, location=None, remote=None, since=None,
              active_only=True, include_duplicates=False, include_inactive_lanes=False,
              sort="score", limit=50, offset=0) -> list[dict]
    def count(self, **same_filters) -> int
    def get(self, uid: str) -> dict | None
    def set_status(self, uid: str, status: str, note: str | None = None) -> dict | None
    def stats(self) -> dict  # {"total","active","new_24h","new_7d","by_group":{},"by_lane":{},"by_source":{},"by_status":{},"last_run":{...}|None}
    def runs(self, limit: int = 20) -> list[dict]
    def export_snapshot(self) -> dict     # {"version":1,"exported_at","jobs":[row dicts],"runs":[...]}
    def import_snapshot(self, snap: dict) -> int   # merge: keeps earliest first_seen, user status/notes win
    def close(self) -> None
```

Row dicts (`query`, `get`, exports, API, MCP) = `Job.to_dict()` + `first_seen, last_seen, active (bool),
status, note, status_updated_at, duplicate_of`. `sort`: `score` (desc), `newest` (first_seen desc),
`posted` (posted_at desc), `closing` (closes_at asc, nulls last), `company`. `since` = ISO date/datetime on
`first_seen`. Duplicates: same `fingerprint` as an existing active job from another source →
`duplicate_of` = that uid (the earlier one stays canonical). Statuses: `models.JOB_STATUSES`.

## 6. Sweep (sweep.py)

```python
@dataclass class SweepOptions:
    sources: list[str] | None = None        # None = every available, non-metered source
    lanes: list[str] | None = None          # None = all active lanes
    groups: list[str] | None = None
    employers: list[str] | None = None      # employer keys
    include_metered: bool = False
    boards: bool = True                     # run board sources over the employer registry
    searches: bool = True                   # run search sources over taxonomy.search_plan
    max_queries_per_source: int = 40        # further capped by Source.query_budget
    posted_within_days: int | None = 30
    concurrency: int = 8
    enrich_threshold: float = 8.0           # title_signal >= this → call source.enrich(job)
    dry_run: bool = False                   # plan only

@dataclass class SweepReport:
    run_id: int | None; started_at: str; finished_at: str; plan: dict
    per_source: dict[str, dict]   # {"targets": n, "ok": n, "errors": n, "jobs": n}
    errors: list[dict]            # {"source","target","error"}
    new_uids: list[str]; total_jobs: int
    def summary(self) -> str      # human readable multi-line

async def run_sweep(settings, taxonomy, registry, store, options=SweepOptions(), progress=None) -> SweepReport
def run_sweep_sync(...) -> SweepReport
async def live_search(settings, taxonomy, query: SearchQuery, sources: list[str] | None = None, store=None) -> list[Job]
async def board_snapshot(settings, taxonomy, employer: Employer) -> list[Job]   # ad hoc check of one board
```

Flow: build plan → board targets (registry.select(...) × adapter) + search targets (search_plan per source,
capped) → run with `asyncio.Semaphore(concurrency)` → apply `employer.title_filter` → `title_signal` →
`enrich` promising jobs → `classify` → `upsert_jobs` → `mark_board_complete` per successful board →
`expire()` → `finish_run`. One failing target never aborts the sweep.

## 7. Export (export.py)

`export_site(store, taxonomy, registry, out_dir, *, min_score=None, days=None) -> Path` writes
`out_dir/index.html, app.js, styles.css` (copied from `web/static/`), `out_dir/config.js`
(`window.JOBSWEEP_MODE = "static";`), `out_dir/data/jobs.json` (`{"generated_at","total","items":[rows]}`),
`data/lanes.json` (= `taxonomy.to_dict()` + per-lane counts), `data/stats.json` (= `store.stats()`),
`data/employers.json` (`{"sweepable":[…],"manual":[…]}`), `data/sources.json`.
`digest(store, days=1, min_score=…, fmt="md"|"json") -> str` — new matches grouped by group → lane.
`write_csv(rows, path)`.

## 8. CLI (cli.py, `jobsweep` / `python -m jobsweep`)

```
jobsweep doctor                         # env, sources availability, catalog counts
jobsweep lanes [--all] [--json]
jobsweep employers [--group G] [--manual] [--json]
jobsweep sweep [--sources a,b] [--lanes x,y] [--groups g] [--employers k] [--include-metered]
               [--no-boards] [--no-searches] [--max-queries N] [--days N] [--dry-run] [--json]
jobsweep search "keywords" [--location L] [--sources a,b] [--no-store] [--limit N] [--json]
jobsweep list [--q TEXT] [--lane L] [--group G] [--source S] [--status S] [--entry] [--min-score N]
              [--days N] [--sort score|newest|posted|closing] [--limit N] [--json|--csv FILE]
jobsweep show UID
jobsweep status UID STATUS [--note TEXT]
jobsweep import FILE [--origin indeed-connector] [--format json|csv|md]
jobsweep detect "Company name or careers URL"
jobsweep verify-employers [--group G] [--write]
jobsweep export [--out DIR] [--min-score N] [--days N]
jobsweep digest [--days 1] [--format md|json]
jobsweep state export FILE | jobsweep state import FILE
jobsweep serve [--host 127.0.0.1] [--port 8765] [--reload]
jobsweep mcp                            # stdio MCP server (same as jobsweep-mcp)
```

## 9. Web API (web/app.py) — `create_app(settings=None) -> FastAPI`

```
GET  /                      static index.html (web/static)
GET  /config.js             window.JOBSWEEP_MODE = "api";
GET  /api/jobs              ?q&lane(*)&group(*)&source(*)&status(*)&entry=1&min_score&location&remote&since&days&sort&limit&offset&include_inactive_lanes
                            -> {"total": n, "items": [rows]}
GET  /api/jobs/{uid}        -> row (404 if missing); uid contains ':' — route uses {uid:path}
POST /api/jobs/{uid}/status {"status": "...", "note": "..."} -> row
GET  /api/lanes             -> taxonomy.to_dict() with "count" on each lane/group
GET  /api/stats             -> store.stats()
GET  /api/runs              -> store.runs()
GET  /api/sources           -> [{"name","label","kind","available","reason","metered","docs_url"}]
GET  /api/employers         -> {"sweepable":[employer dicts],"manual":[...]}
POST /api/sweep             SweepOptions fields as JSON -> {"started": true} (background task; one at a time, 409 if running)
GET  /api/sweep/status      -> {"running": bool, "last_report": {...} | null, "progress": {...}}
POST /api/import            {"origin": "...", "format": "json|csv|md", "payload": "..." | [...]} -> {"imported": n, "new": n}
GET  /api/digest            ?days=1&format=md|json
```

UI (vanilla JS, no build step) works in both modes: `config.js` sets `JOBSWEEP_MODE`; in `"static"` mode it
loads `data/*.json` and keeps statuses in `localStorage`; in `"api"` mode it calls the endpoints above.

## 10. MCP server (mcp_server.py) — FastMCP name `jobsweep`, stdio

Tools: `list_lanes(include_inactive=False)`, `search_jobs(query=None, lanes=None, groups=None, sources=None,
statuses=None, entry_level_only=True, location=None, remote=None, new_within_days=None, min_score=None,
sort="score", limit=25)`, `get_job(uid)`, `set_job_status(uid, status, note=None)`,
`run_sweep(sources=None, lanes=None, groups=None, employers=None, include_metered=False, searches=True,
boards=True)`, `live_search(keywords, location=None, sources=None, limit=25)`, `check_employer(name_or_url)`,
`import_jobs(jobs=None, text=None, origin="claude-connector")` (accepts a list of job objects *or* raw
connector output text such as the Indeed connector's markdown), `list_sources()`, `digest(days=1)`,
`list_employers(group=None, manual_only=False)`.
Prompts: `daily_sweep` (how to combine the jobsweep tools with the Indeed / other connectors).
Entry point `main()`; `jobsweep mcp` calls it.

## 11. Configuration / linking

* `.env` (see `.env.example`): credentials from `config.ENV_VARS`.
* Repo-root `.mcp.json`: the local `jobsweep` stdio server + remote job MCP servers (Pipeworx USAJOBS,
  TheirStack, HasData Indeed, Apify, Exa, …) with `${VAR:-}` env expansion.
* `jobsweep/integrations/claude_desktop_config.json`, `claude_ai_connectors.md`, `project_instructions.md`,
  `scheduled_task_prompt.md`.
* `.claude/skills/jobsweep/SKILL.md`: how Claude runs a sweep end to end (local tool + connectors + import).
* GitHub: `.github/workflows/jobsweep-ci.yml` (lint+tests), `jobsweep-sweep.yml` (cron + dispatch; state on
  branch `jobsweep-state`; Pages deploy; issue digest).
