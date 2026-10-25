"""TheirStack job search API (metered: 1 API credit per job returned).

Docs: https://theirstack.com/en/docs/api-reference/jobs/search_jobs_v1 (OpenAPI:
https://api.theirstack.com/openapi.json; verified 2026-10-25, unauthenticated calls answer 401).

``POST https://api.theirstack.com/v1/jobs/search`` with ``Authorization: Bearer <THEIRSTACK_API_KEY>`` and a
JSON body. Used filters: ``job_title_or`` (keyword patterns, all words in any order), ``job_country_code_or``
(ISO2), ``posted_at_max_age_days`` (one ``posted_at_*`` or company filter is mandatory),
``workplace_types_or`` (``remote``/``hybrid``/``on_site``), ``job_description_pattern_or`` (regex),
``limit``, ``page`` (0-based). Response ``{"metadata": {...}, "data": [job]}``; a job has ``id, job_title,
url, final_url, source_url, date_posted, discovered_at, company, company_object{name, domain...},
location, short_location, long_location, locations[{display_name...}], country_code, remote, hybrid,
salary_string, min/max_annual_salary, min/max_salary, salary_currency, salary_period,
employment_statuses[], seniority, description (markdown)``.

``query.extra``: ``country_codes`` (default from the location: UK -> GB, else US), ``description_patterns``
(list of regexes), ``title_patterns`` (overrides the keyword title filter), ``limit`` (credits per query,
default min(query.limit, 25)), ``seniority`` (list: junior, mid_level, senior, staff, c_level).
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Job, SearchQuery
from ..text import iso_date
from .base import Source, SourceUnavailable
from .manual_import import (
    clean_description,
    currency_code,
    normalize_employment_type,
    normalize_period,
    salary_from_text,
    short_hash,
    to_float,
)

API = "https://api.theirstack.com/v1/jobs/search"
DEFAULT_LIMIT = 25
MAX_PAGES = 4
_UK = re.compile(r"\b(london|united kingdom|uk|england|scotland|wales|great britain)\b", re.I)
_REMOTE = re.compile(r"^\s*remote\b", re.I)


def build_body(query: SearchQuery, page: int = 0, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    body: dict[str, Any] = {
        "page": page,
        "limit": limit,
        "posted_at_max_age_days": int(query.posted_within_days or query.extra.get("max_age_days") or 30),
        "include_total_results": False,
        "blur_company_data": False,
    }
    keywords = (query.keywords or "").strip()
    titles = query.extra.get("title_patterns")
    if titles:
        body["job_title_or"] = [titles] if isinstance(titles, str) else list(titles)
    elif keywords and keywords != "*":
        body["job_title_or"] = [keywords]
    patterns = query.extra.get("description_patterns")
    if patterns:
        body["job_description_pattern_or"] = [patterns] if isinstance(patterns, str) else list(patterns)
    countries = query.extra.get("country_codes")
    if countries:
        body["job_country_code_or"] = [countries] if isinstance(countries, str) else list(countries)
    elif query.location and _UK.search(query.location):
        body["job_country_code_or"] = ["GB"]
    else:
        body["job_country_code_or"] = ["US"]
    if query.remote or (query.location and _REMOTE.match(query.location)):
        body["workplace_types_or"] = ["remote"]
    if query.extra.get("seniority"):
        sen = query.extra["seniority"]
        body["job_seniority_or"] = [sen] if isinstance(sen, str) else list(sen)
    return body


class TheirStackSource(Source):
    name = "theirstack"
    kind = "search"
    label = "TheirStack job search API"
    requires_env = ("THEIRSTACK_API_KEY",)
    metered = True
    docs_url = "https://theirstack.com/en/docs/api-reference/jobs/search_jobs_v1"
    max_queries = 10

    async def search(self, query: SearchQuery) -> list[Job]:
        key = self.ctx.settings.get("THEIRSTACK_API_KEY")
        if not key:
            raise SourceUnavailable("theirstack needs THEIRSTACK_API_KEY")
        want = int(query.extra.get("limit") or min(int(query.limit or DEFAULT_LIMIT), DEFAULT_LIMIT))
        want = max(1, want)
        per_page = min(want, 100)
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        jobs: list[Job] = []
        seen: set[str] = set()
        returned = 0
        for page in range(MAX_PAGES):
            request_limit = min(per_page, want - returned)
            body = build_body(query, page=page, limit=request_limit)
            # retries=0: every returned job costs a credit, never replay a request that may have succeeded
            data = await self.post_json(API, body, headers=headers, retries=0)
            rows = data.get("data") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                break
            # Credits are charged for raw rows, including duplicates/malformed jobs.
            returned += len(rows)
            for raw in rows:
                try:
                    job = self._job(raw)
                except Exception as exc:  # noqa: BLE001
                    self.ctx.log.debug("theirstack: skipped malformed job: %s", exc)
                    continue
                if job and job.source_id not in seen:
                    seen.add(job.source_id)
                    jobs.append(job)
            if len(rows) < request_limit or returned >= want:
                break
        return jobs[:want]

    def _job(self, r: Any) -> Job | None:
        if not isinstance(r, dict):
            return None
        title = (r.get("job_title") or "").strip()
        if not title:
            return None
        url = r.get("final_url") or r.get("url") or r.get("source_url") or ""
        rid = str(r.get("id") or "").strip() or short_hash(url, title)
        company_obj = r.get("company_object") if isinstance(r.get("company_object"), dict) else {}
        company = (company_obj.get("name") or r.get("company") or "").strip()

        locs = [
            loc.get("display_name")
            for loc in r.get("locations") or []
            if isinstance(loc, dict) and loc.get("display_name")
        ]
        locs = list(dict.fromkeys(locs))
        location = (
            "; ".join(locs)
            if len(locs) > 1
            else (
                r.get("short_location")
                or r.get("long_location")
                or r.get("location")
                or (locs[0] if locs else "")
            )
        )

        remote: bool | None = None
        if r.get("remote") is True:
            remote = True
        elif r.get("hybrid") is True or r.get("remote") is False:
            remote = False

        lo = hi = None
        cur = currency_code(r.get("salary_currency"))
        period = normalize_period(r.get("salary_period"))
        if period and (r.get("min_salary") is not None or r.get("max_salary") is not None):
            lo, hi = to_float(r.get("min_salary")), to_float(r.get("max_salary"))
        if lo is None and hi is None:
            lo, hi = to_float(r.get("min_annual_salary")), to_float(r.get("max_annual_salary"))
            period = "year" if (lo is not None or hi is not None) else None
        if lo is None and hi is None and r.get("salary_string"):
            lo, hi, c, period = salary_from_text(r.get("salary_string"))
            cur = cur or c
        if lo is None and hi is None:
            cur = period = None

        statuses = r.get("employment_statuses") or []
        employment = normalize_employment_type(statuses)
        tags = [t for t in ("internship",) if "internship" in statuses or employment == "internship"]

        extra: dict[str, Any] = {}
        for k in ("salary_string", "country_code", "company_domain", "source_url", "closed_at"):
            if r.get(k):
                extra[k] = r[k]
        return Job(
            source=self.name,
            source_id=rid,
            title=title,
            company=company,
            url=url,
            location=location or "",
            remote=remote,
            description=clean_description(r.get("description")),
            posted_at=iso_date(r.get("date_posted")),
            salary_min=lo,
            salary_max=hi,
            salary_currency=cur,
            salary_period=period,
            employment_type=employment,
            seniority=r.get("seniority") or None,
            tags=tags,
            extra=extra,
        )
