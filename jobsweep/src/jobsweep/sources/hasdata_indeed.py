"""Indeed search results via the HasData scraper API (metered: 10 credits per call, free tier = 100 calls/mo).

Docs (verified 2026-10-25; both endpoints answer 401 without a key):
* listing: https://docs.hasdata.com/apis/indeed/listing — ``GET https://api.hasdata.com/scrape/indeed/listing``
  with header ``x-api-key``; params ``keyword`` + ``location`` (required), ``sort`` (``date``|``relevance``),
  ``domain`` (``www.indeed.com``, ``uk.indeed.com``...), ``datePosted`` (1, 3, 7 or 14 days),
  ``remoteType`` (``remote``|``hybrid``), ``start`` (offset, steps of 10), ``nextPage``.
  Response ``{"requestMetadata": {...}, "searchInformation": {"totalResults": "3273"}, "jobs": [...],
  "pagination": {"nextPage": "<indeed url>"}}``; a job is ``title, company, location, description (snippet),
  salary {min, max, type: YEARLY|HOURLY|..., currency}, details ["Full-time", "Hybrid work"], benefits[],
  sponsored, date ("15 days ago"), isoDate, url (…/viewjob?jk=…)``. Field names from the HasData Indeed API
  page (https://hasdata.com/apis/indeed-api, "jobs.json" / "job.json" excerpts).
* job: https://docs.hasdata.com/apis/indeed/job — ``GET https://api.hasdata.com/scrape/indeed/job?url=<job url>``
  -> ``{"requestMetadata": {...}, "job": {title, company, location, salary, details: {jobType: [...]},
  description, descriptionHtml}}``. Used by ``enrich`` (one call per promising job).

``query.extra``: ``domain``, ``sort``, ``pages`` (default 1 — every page is a paid call), ``remote_type``.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..models import Job, SearchQuery
from ..text import html_to_text, iso_date, relative_to_iso
from .base import Source, SourceUnavailable
from .manual_import import (
    clean_description,
    currency_code,
    location_remote,
    normalize_employment_type,
    normalize_period,
    remote_from_value,
    salary_from_text,
    short_hash,
    to_float,
)

API = "https://api.hasdata.com/scrape/indeed"
LISTING = f"{API}/listing"
DETAIL = f"{API}/job"
DATE_POSTED_STEPS = (1, 3, 7, 14)
_UK = re.compile(r"\b(london|united kingdom|uk|england|scotland|wales)\b", re.I)
_REMOTE_LOC = re.compile(r"^\s*(remote|anywhere)\s*$", re.I)


def indeed_key(url: str | None) -> str | None:
    """The ``jk`` job key of an Indeed URL (viewjob?jk=..., /rc/clk?jk=..., /m/viewjob?jk=...)."""
    if not url:
        return None
    try:
        qs = parse_qs(urlsplit(url).query)
    except ValueError:
        return None
    for k in ("jk", "vjk"):
        if qs.get(k):
            return qs[k][0]
    return None


def canonical_url(url: str | None) -> str:
    """Strip Indeed tracking params: ``https://<host>/viewjob?jk=<key>``."""
    if not url:
        return ""
    jk = indeed_key(url)
    if not jk:
        return url
    host = urlsplit(url).netloc or "www.indeed.com"
    return f"https://{host}/viewjob?jk={jk}"


def _date_posted(days: int | None) -> int | None:
    if not days:
        return None
    for step in DATE_POSTED_STEPS:
        if days <= step:
            return step
    return None  # older than 14 days: no filter


def _salary(raw: Any) -> tuple[float | None, float | None, str | None, str | None]:
    if isinstance(raw, dict):
        lo, hi = to_float(raw.get("min")), to_float(raw.get("max"))
        if lo is None and hi is None:
            return None, None, None, None
        return (
            lo,
            hi if hi is not None else lo,
            currency_code(raw.get("currency")),
            normalize_period(raw.get("type")),
        )
    if raw:
        return salary_from_text(raw)
    return None, None, None, None


def _labels(raw: Any) -> list[str]:
    if isinstance(raw, dict):
        out: list[str] = []
        for v in raw.values():
            out.extend(_labels(v))
        return out
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if isinstance(x, (str, int, float)) and str(x).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


class HasDataIndeedSource(Source):
    name = "hasdata_indeed"
    kind = "search"
    label = "Indeed via HasData scraper API"
    requires_env = ("HASDATA_API_KEY",)
    metered = True
    docs_url = "https://docs.hasdata.com/apis/indeed/listing"
    max_queries = 10

    def _headers(self) -> dict[str, str]:
        key = self.ctx.settings.get("HASDATA_API_KEY")
        if not key:
            raise SourceUnavailable("hasdata_indeed needs HASDATA_API_KEY")
        return {"x-api-key": key, "Content-Type": "application/json"}

    def params_for(self, query: SearchQuery) -> dict[str, Any]:
        location = (query.location or "").strip()
        remote = bool(query.remote) or bool(_REMOTE_LOC.match(location))
        if not location or _REMOTE_LOC.match(location):
            location = "United States"  # Indeed ties fully remote US jobs to the country
        params: dict[str, Any] = {
            "keyword": (query.keywords or "").strip(),
            "location": location,
            "sort": query.extra.get("sort") or "date",
        }
        domain = query.extra.get("domain")
        if not domain and _UK.search(location):
            domain = "uk.indeed.com"
        if domain:
            params["domain"] = domain
        dp = _date_posted(query.posted_within_days)
        if dp:
            params["datePosted"] = dp
        remote_type = query.extra.get("remote_type") or ("remote" if remote else None)
        if remote_type:
            params["remoteType"] = remote_type
        return params

    async def search(self, query: SearchQuery) -> list[Job]:
        headers = self._headers()
        params = self.params_for(query)
        limit = max(1, int(query.limit or 50))
        pages = max(1, min(int(query.extra.get("pages") or 1), 5))
        jobs: list[Job] = []
        seen: set[str] = set()
        for page in range(pages):
            if page:
                params["start"] = page * 10
            data = await self.get_json(LISTING, params=params, headers=headers)
            rows = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(rows, list) or not rows:
                break
            for raw in rows:
                try:
                    job = self._job(raw)
                except Exception as exc:  # noqa: BLE001
                    self.ctx.log.debug("hasdata_indeed: skipped malformed job: %s", exc)
                    continue
                if job and job.source_id not in seen:
                    seen.add(job.source_id)
                    jobs.append(job)
            if len(jobs) >= limit or not (data.get("pagination") or {}).get("nextPage"):
                break
        return jobs[:limit]

    def _job(self, r: Any) -> Job | None:
        if not isinstance(r, dict):
            return None
        title = (r.get("title") or "").strip()
        raw_url = r.get("url") or r.get("link") or ""
        if not title:
            return None
        jk = r.get("jobKey") or r.get("jk") or indeed_key(raw_url)
        url = canonical_url(raw_url) if raw_url else (f"https://www.indeed.com/viewjob?jk={jk}" if jk else "")
        company = (r.get("company") or "").strip() if isinstance(r.get("company"), str) else ""
        location = (r.get("location") or "").strip() if isinstance(r.get("location"), str) else ""
        lo, hi, cur, period = _salary(r.get("salary"))
        labels = _labels(r.get("details"))
        employment = normalize_employment_type(labels)
        remote = remote_from_value(labels)
        if remote is None:
            remote = location_remote(location)
        posted = iso_date(r.get("isoDate") or r.get("postedAt")) or relative_to_iso(r.get("date"))
        tags = [t for t in ("internship", "fellowship") if employment == t]
        extra: dict[str, Any] = {"snippet": True}
        if r.get("sponsored"):
            extra["sponsored"] = True
        if labels:
            extra["labels"] = labels[:8]
        return Job(
            source=self.name,
            source_id=str(jk) if jk else short_hash(raw_url, title, company),
            title=title,
            company=company,
            url=url,
            location=location,
            remote=remote,
            description=clean_description(r.get("description")),
            posted_at=posted,
            salary_min=lo,
            salary_max=hi,
            salary_currency=cur,
            salary_period=period,
            employment_type=employment,
            tags=tags,
            extra=extra,
        )

    async def enrich(self, job: Job) -> Job:
        """Full description (+ job type / salary when missing) from the HasData job endpoint."""
        if job.extra.get("enriched") or not job.url:
            return job
        data = await self.get_json(DETAIL, params={"url": job.url}, headers=self._headers())
        if not isinstance(data, dict):
            return job
        detail = data.get("job") if isinstance(data.get("job"), dict) else data
        if not isinstance(detail, dict) or not (detail.get("description") or detail.get("descriptionHtml")):
            job.extra["enriched"] = False
            return job
        text = (
            html_to_text(detail["descriptionHtml"])
            if detail.get("descriptionHtml")
            else clean_description(detail.get("description"))
        )
        if text:
            job.description = text
        if not job.employment_type:
            job.employment_type = normalize_employment_type(_labels(detail.get("details")))
            if job.employment_type in ("internship", "fellowship") and job.employment_type not in job.tags:
                job.tags.append(job.employment_type)
        if job.salary_min is None and job.salary_max is None:
            lo, hi, cur, period = _salary(detail.get("salary"))
            if lo is not None or hi is not None:
                job.salary_min, job.salary_max, job.salary_currency, job.salary_period = lo, hi, cur, period
        if job.remote is None:
            job.remote = remote_from_value(_labels(detail.get("details")))
            if job.remote is None and isinstance(detail.get("location"), str):
                job.remote = location_remote(detail["location"])
        job.extra.pop("snippet", None)
        job.extra["enriched"] = True
        return job
