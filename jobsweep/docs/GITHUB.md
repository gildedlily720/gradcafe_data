# Running jobsweep on GitHub

Two workflows live in `.github/workflows/`:

| Workflow | When | What it does |
|---|---|---|
| `jobsweep-ci.yml` | every push / PR touching `jobsweep/**` | ruff, catalog validation, offline tests, packaging smoke test (dry-run sweep + export) |
| `jobsweep-sweep.yml` | daily at 11:23 UTC (07:23 New York in summer) and on demand | full sweep → digest → static board on GitHub Pages → state saved to the `jobsweep-state` branch → one issue per day with new matches |

## One-time setup

1. **Merge to the default branch (`master`).** GitHub only runs scheduled workflows from the default branch.
   Until then you can still start the sweep by hand from the Actions tab once the workflow file is on `master`.
2. **Turn on Pages:** Settings → Pages → Build and deployment → Source: **GitHub Actions**.
   The board then lives at `https://<user>.github.io/<repo>/`. Statuses you set on the Pages board are stored in
   your browser (localStorage), not in the repo.
3. **Add secrets** (Settings → Secrets and variables → Actions → New repository secret). All are optional; the sweep
   runs without any of them (employer boards, NYC Jobs, NY State, USAJOBS via the free Pipeworx gateway, JobSpy).

| Secret | What it unlocks | Where to get it | Cost |
|---|---|---|---|
| `USAJOBS_API_KEY`, `USAJOBS_EMAIL` | direct USAJOBS API (series / hiring-path filters, 200 queries per sweep) | https://developer.usajobs.gov/apirequest/ | free |
| `PIPEWORX_API_KEY` | higher Pipeworx quota for the USAJOBS fallback (`<auth_key>:<email>`) | https://www.pipeworx.io | free tier 50 calls/day |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Adzuna keyword search (US + UK) | https://developer.adzuna.com/ | free, rate limited |
| `THEIRSTACK_API_KEY` | TheirStack job search (metered — only with *include_metered*) | https://app.theirstack.com | credits |
| `HASDATA_API_KEY` | Indeed listings via HasData (metered) | https://hasdata.com | free 100 calls/month |
| `APIFY_TOKEN` | hiring.cafe / JobSpy actors on Apify (metered) | https://console.apify.com/account/integrations | pay per result |
| `SOCRATA_APP_TOKEN` | higher NYC Open Data rate limit | https://data.cityofnewyork.us/profile/edit/developer_settings | free |

## Running it by hand

Actions → **jobsweep daily sweep** → Run workflow. Inputs let you limit lanes / groups / sources, include metered
sources, and skip Pages or the issue.

## How state persists

Each run imports `state.json.gz` from the orphan branch `jobsweep-state`, sweeps, then writes the new snapshot back
together with `digest.md` and `history/<date>.md`. "New" therefore means *new since the last run*, and jobs keep
their first-seen date across runs. Delete the branch to start over.

## Reading the results

* **Issue** "Job sweep digest YYYY-MM-DD" (label `jobsweep`) — new matches grouped by lane, entry-level first.
* **Pages board** — the full board, filterable by lane, score, entry level, location and source.
* **Run summary** — the digest is also written to the workflow run's summary page and uploaded as an artifact.

## Limits to keep in mind

* Pipeworx USAJOBS fallback: about 50 calls/day per IP; the sweep uses at most 12.
* Workable's API sometimes rate-limits cloud IPs (Cloudflare 1015); those boards just report an error that day.
* Metered sources are off unless you tick *include_metered* (or name them in *sources*).
