# Integration to-do (handoff notes)

Status at the time of writing: the source adapters workflow is complete (ATS core + extended, government feeds,
aggregators, connector import; ~270 adapter tests passing, live smoke checks green except Workable which is
Cloudflare-rate-limited from the build machine). The app workflow (engine, taxonomy, classifier, web, MCP,
connectors, GitHub) and the employer-research workflows were still running.

Fixes the adapter agents flagged for the core / integration pass:

1. **pytest**: set `addopts = "-q -m 'not live'"` in `pyproject.toml` and add a shared `tests/conftest.py`
   (several test modules carry their own autouse live-skip fixture; consolidate).
2. **text.parse_salary**: `$85,000.00 - $150,000.00` parses as 85000-85000 (cents break the range) and
   `US$120k to US$175k` loses the high end. `manual_import.salary_from_text` has a workaround — fold it into `text.py`.
3. **text.iso_date**: does not parse `YYYY-MM-DD HH:MM:SS UTC` (Recruitee works around it).
4. **http.request**: add a per-request `timeout` and make metered POSTs default to `retries=0` (Apify/TheirStack
   work around it); keep response headers on `HTTPError` (Retry-After of hours = Cloudflare ban);
   add `apply.workable.com: 1` to `DEFAULT_HOST_LIMITS`.
5. **sweep**: `run_search` never calls `source.enrich()`; feed sources (nysjobs) reach the classifier with empty
   descriptions. Enrich promising search results too (at least those with empty descriptions).
6. **Shared helpers**: ATS normalisation helpers live in `sources/detect.py` (imported by all extended adapters) and
   aggregator helpers in `sources/manual_import.py`. Consider moving both into `text.py`/`sources/_common.py`
   keeping re-exports so imports don't break.
7. **Source kinds**: `ImportSource.kind == "import"` — any exact-kind checks must allow it.
8. **Empty URLs**: imported connector results may have `url == ""` — store, UI and MCP must tolerate it.
9. **ARCHITECTURE.md §3.1 corrections** (verified live):
   - greenhouse needs `&pay_transparency=true` for pay ranges.
   - workday: `limit` max 20; `total` only on offset 0; `source_id = "{tenant}:{req id}"`.
   - paylocity: v2 feed is opt-in/empty; adapter scrapes `/recruiting/jobs/All/{guid}` pageData + JSON-LD details.
   - adp: `$top` max 20, `$skip` 1-based, no server-side search (search_terms filtered client-side on titles).
   - oracle: keyword must be quoted inside the finder; detail via `recruitingCEJobRequisitionDetails`.
   - ultipro: verified LoadSearchResults body; board may need a host prefix for recruiting2 / .ca tenants.
   - breezy: `/json?verbose=true` for descriptions. teamtailor: `jobs.rss` with `jobs.json` fallback.
   - usajobs: Pipeworx ignores series/hiring_path/date filters (post-filtered locally); grades must be `05` style;
     `data.usajobs.gov` 403s non-browser UAs without key headers.
   - nycjobs: one uid per job_id (Internal/External rows merged; tag `internal` only when no External row).
10. **Personio demo boards**: unclaimed slugs (dgap, ecfr, article19) serve lorem-ipsum demo jobs — never register them.
11. **Pipeworx quota** is ~50 calls/day shared per egress IP; keep live tests to one call.
12. **Classifier**: use tags `internal` (federal/NYC internal-only postings) to rank down jobs the candidate cannot apply to.

After the remaining workflows land: run the whole suite, ruff, `scripts/validate_catalog.py`, a dry-run sweep, a
real sweep (non-metered), export the static board, review UI screenshots, then commit/push.
