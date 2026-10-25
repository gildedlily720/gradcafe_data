"""Paylocity Recruiting career pages.

Endpoints (verified live 2026-10-25):

* list: ``GET https://recruiting.paylocity.com/recruiting/jobs/All/{guid}`` -> HTML whose inline script
  ``window.pageData = {...}`` holds the whole board (no pagination): ``ModuleTitle`` plus ``Jobs[]`` with
  ``JobId, JobTitle, LocationName, PublishedDate, Description`` (a ~110-character teaser),
  ``IsInternal, HiringDepartment, JobLocation{City, State, Country, ...}, IsRemote, IndeedRemoteType``
  (``0`` temporarily WFH, ``1`` hybrid, ``2`` fully remote; only meaningful when ``IsRemote``).
* detail: ``GET https://recruiting.paylocity.com/Recruiting/Jobs/Details/{JobId}`` -> HTML with a
  schema.org ``JobPosting`` JSON-LD block (full description, datePosted, jobLocation, baseSalary).

The JSON feed named in ARCHITECTURE §3.1 (``/recruiting/v2/api/feed/jobs/{guid}``) is a per-tenant
syndication opt-in: it answered ``{"jobs": []}`` for a board whose page lists 11 postings, so it is only
used as a fallback when the page carries no ``pageData``.

Board = the company GUID from the careers URL. ``source_id`` = ``JobId`` (global). Verified employer:
German Marshall Fund of the US (``43d988e3-6446-4a25-8e28-28602ea60858``).
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..models import Employer, Job
from ..text import html_to_text, iso_date
from .base import Source, SourceError
from .detect import join_locations, normalize_employment_type, safe_parse, structured_tags
from .jobvite import jsonld_job_posting, jsonld_locations, jsonld_salary

BASE = "https://recruiting.paylocity.com"
_PAGE_DATA = re.compile(r"window\.pageData\s*=\s*(\{.*?\})\s*;\s*(?:\n|</script>)", re.S)
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_US = {"USA", "US", "UNITED STATES", ""}


def page_data(html: str) -> dict[str, Any] | None:
    m = _PAGE_DATA.search(html or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _location(j: dict) -> str:
    loc = j.get("JobLocation") if isinstance(j.get("JobLocation"), dict) else None
    if loc and loc.get("City"):
        country = str(loc.get("Country") or "").strip()
        # Paylocity fills State with the company's US state even for foreign offices: keep it for US only.
        state = loc.get("State") if country.upper() in _US else None
        return ", ".join(str(p).strip() for p in (loc.get("City"), state, country) if p and str(p).strip())
    return str(j.get("LocationName") or "").strip()


def _remote(j: dict) -> bool | None:
    if j.get("IsRemote") is True:
        return {1: False, 2: True}.get(j.get("IndeedRemoteType"), True)
    if j.get("IsRemote") is False:
        return False
    return None


class PaylocitySource(Source):
    name = "paylocity"
    kind = "board"
    label = "Paylocity Recruiting career pages"
    docs_url = "https://recruiting.paylocity.com/"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        m = _GUID.search(employer.board or "")
        if not m:
            raise SourceError(f"paylocity: employer {employer.key} board must contain the company GUID")
        guid = m.group(0).lower()
        html = await self.get_text(f"{BASE}/recruiting/jobs/All/{guid}", headers={"Accept": "text/html"})
        data = page_data(html)
        if data is None:
            return await self._from_feed(employer, guid)
        items = [j for j in data.get("Jobs") or [] if isinstance(j, dict) and not j.get("IsInternal")]
        jobs = safe_parse(
            items, lambda j: self._parse(j, employer, guid), source=self.name, board=guid, logger=self.ctx.log
        )
        return jobs[: employer.max_jobs] if employer.max_jobs else jobs

    async def _from_feed(self, employer: Employer, guid: str) -> list[Job]:
        """Fallback: the opt-in syndication feed (field names follow the page model where they overlap)."""
        data = await self.get_json(f"{BASE}/recruiting/v2/api/feed/jobs/{guid}")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceError(f"paylocity {guid}: no pageData on the board page and no usable feed")
        items = []
        for j in data["jobs"]:
            if not isinstance(j, dict):
                continue
            items.append(
                {
                    "JobId": j.get("jobId") or j.get("JobId") or j.get("id"),
                    "JobTitle": j.get("title") or j.get("JobTitle"),
                    "LocationName": j.get("locationName") or j.get("location") or "",
                    "PublishedDate": j.get("postedDate") or j.get("publishedDate"),
                    "Description": j.get("description") or "",
                    "HiringDepartment": j.get("department") or j.get("hiringDepartment"),
                }
            )
        jobs = safe_parse(
            items, lambda j: self._parse(j, employer, guid), source=self.name, board=guid, logger=self.ctx.log
        )
        return jobs[: employer.max_jobs] if employer.max_jobs else jobs

    def _parse(self, j: dict, employer: Employer, guid: str) -> Job | None:
        jid, title = j.get("JobId"), (j.get("JobTitle") or "").strip()
        if jid in (None, "") or not title:
            return None
        department = j.get("HiringDepartment") or None
        extra: dict[str, Any] = {"board": guid}
        if j.get("LocationName"):
            extra["location_name"] = j["LocationName"]
        return Job(
            source=self.name,
            source_id=str(jid),
            title=title,
            company=employer.name,
            url=f"{BASE}/Recruiting/Jobs/Details/{jid}",
            location=join_locations([_location(j)]),
            remote=_remote(j),
            # pageData only carries a teaser; enrich() replaces it with the full JSON-LD description
            description=html_to_text(j.get("Description")),
            posted_at=iso_date(j.get("PublishedDate")),
            department=department,
            employer_key=employer.key,
            tags=structured_tags(department),
            extra=extra,
        )

    async def enrich(self, job: Job) -> Job:
        """Replace the teaser with the detail page's JSON-LD description; add pay and type."""
        if (job.extra or {}).get("enriched"):
            return job
        try:
            html = await self.get_text(f"{BASE}/Recruiting/Jobs/Details/{job.source_id}", headers={"Accept": "text/html"})
        except Exception as exc:  # noqa: BLE001 - enrichment is best effort
            self.ctx.log.warning("paylocity enrich %s failed: %s", job.uid, exc)
            return job
        posting = jsonld_job_posting(html)
        if not posting:
            return job
        job.extra["enriched"] = True
        job.description = html_to_text(posting.get("description")) or job.description
        job.closes_at = job.closes_at or iso_date(posting.get("validThrough"))
        if posting.get("employmentType"):
            job.employment_type = normalize_employment_type(posting["employmentType"])
            for tag in structured_tags(posting["employmentType"]):
                if tag not in job.tags:
                    job.tags.append(tag)
        if not job.location:
            job.location = jsonld_locations(posting)
        if job.salary_min is None:
            lo, hi, cur, period = jsonld_salary(posting)
            if lo is not None:
                job.salary_min, job.salary_max, job.salary_currency, job.salary_period = lo, hi, cur, period
        return job
