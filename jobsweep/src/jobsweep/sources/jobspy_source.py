"""JobSpy scraper (https://github.com/speedyapply/JobSpy, ``pip install python-jobspy``; optional dependency).

``jobspy.scrape_jobs(site_name, search_term, location, results_wanted, hours_old, country_indeed,
description_format="markdown", verbose=0, ...)`` runs in a worker thread and returns a pandas DataFrame
with columns ``id, site, job_url, job_url_direct, title, company, location, date_posted (date), job_type
("fulltime", "parttime", "internship", "contract" — comma separated when several), salary_source, interval
(yearly/monthly/weekly/daily/hourly), min_amount, max_amount, currency, is_remote, job_level (LinkedIn),
job_function, description (markdown), company_industry, company_url, ...`` (verified live 2026-10-25 with
python-jobspy against Indeed).

``query.extra``: ``sites`` (default ``["indeed", "linkedin"]``; also ``google``, ``zip_recruiter``,
``glassdoor``), ``country_indeed`` (default ``"USA"``; ``"UK"`` for London), ``distance``, ``job_type``,
``linkedin_fetch_description``, ``google_search_term``, ``proxies``, ``offset``.
Indeed only honours one of ``hours_old`` / ``is_remote`` / ``job_type`` per search, so ``hours_old`` wins.
"""

from __future__ import annotations

import asyncio
import importlib
import re
from datetime import date, datetime
from typing import Any

from ..models import Job, SearchQuery
from .base import Source, SourceError, SourceUnavailable
from .manual_import import (
    clean,
    clean_description,
    currency_code,
    location_remote,
    normalize_employment_type,
    normalize_period,
    parse_date,
    short_hash,
    text_value,
    to_float,
)

DEFAULT_SITES = ["indeed", "linkedin"]
MAX_RESULTS = 30
_UK = re.compile(r"\b(london|united kingdom|uk|england|scotland|wales)\b", re.I)
_REMOTE_LOC = re.compile(r"^\s*(remote|anywhere)\s*$", re.I)
_PASSTHROUGH = (
    "distance",
    "job_type",
    "linkedin_fetch_description",
    "linkedin_company_ids",
    "google_search_term",
    "proxies",
    "offset",
    "easy_apply",
    "enforce_annual_salary",
)


def _rows(frame: Any) -> list[dict[str, Any]]:
    """DataFrame (or list of dicts) -> list of row dicts."""
    if frame is None:
        return []
    if isinstance(frame, list):
        return [r for r in frame if isinstance(r, dict)]
    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):
        try:
            rows = to_dict(orient="records")
        except TypeError:
            rows = to_dict()
        return [r for r in rows if isinstance(r, dict)]
    return []


def scrape_kwargs(query: SearchQuery) -> dict[str, Any]:
    extra = query.extra or {}
    sites = extra.get("sites") or DEFAULT_SITES
    sites = [sites] if isinstance(sites, str) else list(sites)
    location = (query.location or "").strip()
    remote = bool(query.remote) or bool(_REMOTE_LOC.match(location))
    if not location or _REMOTE_LOC.match(location):
        location = "United States"
    country = extra.get("country_indeed") or ("UK" if _UK.search(location) else "USA")
    kw: dict[str, Any] = {
        "site_name": sites,
        "search_term": (query.keywords or "").strip(),
        "location": location,
        "results_wanted": max(1, min(int(query.limit or MAX_RESULTS), MAX_RESULTS)),
        "country_indeed": country,
        "description_format": "markdown",
        "verbose": 0,
    }
    if query.posted_within_days:
        kw["hours_old"] = int(query.posted_within_days) * 24
    elif remote:
        kw["is_remote"] = True
    for key in _PASSTHROUGH:
        if key in extra and extra[key] is not None:
            kw[key] = extra[key]
    return kw


def _date(value: Any) -> str | None:
    value = clean(value)
    if isinstance(value, datetime):
        return (
            value.date().isoformat()
            if not (value.hour or value.minute or value.second)
            else value.isoformat()
        )
    if isinstance(value, date):
        return value.isoformat()
    return parse_date(value)


class JobSpySource(Source):
    name = "jobspy"
    kind = "search"
    label = "JobSpy scraper (Indeed, LinkedIn, Google, ZipRecruiter, Glassdoor)"
    requires_module = "jobspy"
    docs_url = "https://github.com/speedyapply/JobSpy"
    max_queries = 15

    async def search(self, query: SearchQuery) -> list[Job]:
        try:
            jobspy = importlib.import_module("jobspy")
        except ImportError as exc:
            raise SourceUnavailable("jobspy needs the optional dependency python-jobspy") from exc
        kwargs = scrape_kwargs(query)
        try:
            frame = await asyncio.to_thread(jobspy.scrape_jobs, **kwargs)
        except Exception as exc:  # noqa: BLE001 - scraper errors become per-target sweep errors
            raise SourceError(f"jobspy: {type(exc).__name__}: {exc}") from exc
        jobs: list[Job] = []
        seen: set[str] = set()
        for row in _rows(frame):
            try:
                job = self._job(row)
            except Exception as exc:  # noqa: BLE001
                self.ctx.log.debug("jobspy: skipped malformed row: %s", exc)
                continue
            if job and job.uid not in seen:
                seen.add(job.uid)
                jobs.append(job)
        return jobs

    def _job(self, r: dict[str, Any]) -> Job | None:
        title = text_value(r.get("title"))
        if not title:
            return None
        site = (text_value(r.get("site")) or "jobspy").lower()
        url = clean(r.get("job_url")) or clean(r.get("job_url_direct")) or ""
        company = text_value(r.get("company")) or ""
        rid = text_value(r.get("id")) or short_hash(url, title, company)
        location = text_value(r.get("location")) or ""

        lo, hi = to_float(r.get("min_amount")), to_float(r.get("max_amount"))
        if lo is not None and hi is not None and lo > hi:
            lo, hi = hi, lo
        has_pay = lo is not None or hi is not None
        employment = normalize_employment_type(text_value(r.get("job_type")))
        level = text_value(r.get("job_level"))
        flag = clean(r.get("is_remote"))  # may be numpy.bool_
        remote: bool | None = True if flag is not None and str(flag).lower() in ("true", "1") else None
        if remote is None:
            remote = location_remote(location)

        tags = [site]
        if employment in ("internship", "fellowship"):
            tags.append(employment)
        if level and re.search(r"\bintern", level, re.I) and "internship" not in tags:
            tags.append("internship")

        extra: dict[str, Any] = {"site": site}
        for key in ("job_url_direct", "company_industry", "salary_source"):
            val = clean(r.get(key))
            if isinstance(val, str) and val:
                extra[key] = val
        return Job(
            source=self.name,
            source_id=f"{site}-{rid}",
            title=title,
            company=company,
            url=str(url),
            location=location,
            remote=remote,
            description=clean_description(r.get("description")),
            posted_at=_date(r.get("date_posted")),
            salary_min=lo,
            salary_max=hi,
            salary_currency=currency_code(r.get("currency")) if has_pay else None,
            salary_period=normalize_period(r.get("interval")) if has_pay else None,
            employment_type=employment,
            department=text_value(r.get("job_function")),
            seniority=level,
            tags=tags,
            extra=extra,
        )
