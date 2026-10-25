"""Adzuna job search API.

Docs: https://developer.adzuna.com/docs/search and the endpoint spec (Swagger 1.2)
https://api.adzuna.com/v1/api-docs/adzuna (verified 2026-10-25; a request without valid keys answers
401 ``{"exception": "AUTH_FAIL"}``).

``GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?app_id&app_key&what&where
&results_per_page(<=50)&max_days_old&sort_by=date&content-type=application/json`` ->
``{"count": n, "mean": x, "results": [Job]}`` where a Job has ``id, title, description (snippet, may hold
<strong> highlights), redirect_url, created, company.display_name, location.display_name/area[],
category.label/tag, salary_min/max, salary_is_predicted ("0"/"1"), contract_time (full_time/part_time),
contract_type (permanent/contract), latitude, longitude, adref``.

Country: ``query.extra["country"]`` (gb, us, at, au, be, br, ca, ch, de, es, fr, in, it, mx, nl, nz, pl, sg,
za); otherwise inferred from the location (London/UK -> gb), else ``us``. Other extras: ``category`` (tag),
``distance`` (km), ``sort_by``, ``what_exclude``, ``title_only``. Predicted (estimated) salaries are kept in
``extra["salary_estimate"]`` instead of the salary fields.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Job, SearchQuery
from ..text import html_to_text, iso_date
from .base import Source, SourceError, SourceUnavailable
from .manual_import import infer_period, location_remote, normalize_employment_type, short_hash, to_float

API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
MAX_PER_PAGE = 50
MAX_PAGES = 4

CURRENCIES = {
    "gb": "GBP",
    "us": "USD",
    "at": "EUR",
    "au": "AUD",
    "be": "EUR",
    "br": "BRL",
    "ca": "CAD",
    "ch": "CHF",
    "de": "EUR",
    "es": "EUR",
    "fr": "EUR",
    "in": "INR",
    "it": "EUR",
    "mx": "MXN",
    "nl": "EUR",
    "nz": "NZD",
    "pl": "PLN",
    "sg": "SGD",
    "za": "ZAR",
}
_UK = re.compile(r"\b(london|united kingdom|uk|england|scotland|wales|great britain|gb)\b", re.I)
_UK_SUFFIX = re.compile(r",?\s*\b(united kingdom|uk|england|gb)\b\.?$", re.I)
_NOWHERE = re.compile(r"^\s*(remote|anywhere|worldwide|global|united states|usa|us)\s*$", re.I)


def country_for(query: SearchQuery) -> str:
    cc = str(query.extra.get("country") or "").strip().lower()
    if cc:
        cc = {"uk": "gb"}.get(cc, cc)
        if cc not in CURRENCIES:
            raise SourceError(
                f"adzuna: unsupported country {cc!r} (use one of {', '.join(sorted(CURRENCIES))})"
            )
        return cc
    if query.location and _UK.search(query.location):
        return "gb"
    return "us"


def where_for(location: str | None, country: str) -> str | None:
    if not location or _NOWHERE.match(location):
        return None
    loc = location.strip()
    if country == "gb":
        loc = _UK_SUFFIX.sub("", loc).strip() or loc
    return loc


class AdzunaSource(Source):
    name = "adzuna"
    kind = "search"
    label = "Adzuna job search API"
    requires_env = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY")
    docs_url = "https://developer.adzuna.com/docs/search"
    max_queries = 40  # free tier is rate limited (~25 calls/min, ~250/day)

    async def search(self, query: SearchQuery) -> list[Job]:
        app_id = self.ctx.settings.get("ADZUNA_APP_ID")
        app_key = self.ctx.settings.get("ADZUNA_APP_KEY")
        if not (app_id and app_key):
            raise SourceUnavailable("adzuna needs ADZUNA_APP_ID and ADZUNA_APP_KEY")
        country = country_for(query)
        limit = max(1, int(query.limit or MAX_PER_PAGE))
        per_page = min(limit, MAX_PER_PAGE)
        params: dict[str, Any] = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": per_page,
            "sort_by": query.extra.get("sort_by") or "date",
            "content-type": "application/json",
        }
        keywords = (query.keywords or "").strip()
        if keywords and keywords != "*":
            params["what"] = keywords
        where = where_for(query.location, country)
        if where:
            params["where"] = where
        if query.posted_within_days:
            params["max_days_old"] = int(query.posted_within_days)
        for key in ("category", "distance", "what_exclude", "title_only", "what_or", "what_phrase"):
            if query.extra.get(key):
                params[key] = query.extra[key]

        jobs: list[Job] = []
        seen: set[str] = set()
        pages = min(MAX_PAGES, -(-limit // per_page))
        for page in range(1, pages + 1):
            data = await self.get_json(API.format(country=country, page=page), params=params)
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                break
            for raw in results:
                try:
                    job = self._job(raw, country, query)
                except Exception as exc:  # noqa: BLE001 - one bad posting never sinks the query
                    self.ctx.log.debug("adzuna: skipped malformed result: %s", exc)
                    continue
                if job and job.source_id not in seen:
                    seen.add(job.source_id)
                    jobs.append(job)
            count = data.get("count") if isinstance(data, dict) else None
            if (
                len(results) < per_page
                or len(jobs) >= limit
                or (isinstance(count, int) and page * per_page >= count)
            ):
                break
        return jobs[:limit]

    def _job(self, r: Any, country: str, query: SearchQuery) -> Job | None:
        if not isinstance(r, dict):
            return None
        title = html_to_text(r.get("title"), limit=300)
        url = r.get("redirect_url") or ""
        if not title:
            return None
        rid = str(r.get("id") or "").strip() or short_hash(url, title)
        company = html_to_text(
            ((r.get("company") or {}) if isinstance(r.get("company"), dict) else {}).get("display_name")
        )
        loc_obj = r.get("location") if isinstance(r.get("location"), dict) else {}
        location = (loc_obj.get("display_name") or "").strip()
        if not location and isinstance(loc_obj.get("area"), list):
            location = ", ".join(str(a) for a in reversed(loc_obj["area"]) if a)
        description = html_to_text(r.get("description"))
        category = r.get("category") if isinstance(r.get("category"), dict) else {}

        lo, hi = to_float(r.get("salary_min")), to_float(r.get("salary_max"))
        predicted = str(r.get("salary_is_predicted") or "0").strip().lower() in ("1", "true")
        extra: dict[str, Any] = {"country": country}
        if category.get("tag"):
            extra["category_tag"] = category["tag"]
        if r.get("adref"):
            extra["adref"] = r["adref"]
        currency = period = None
        if lo is not None or hi is not None:
            if predicted:
                extra["salary_estimate"] = [lo, hi]
                lo = hi = None
            else:
                currency = CURRENCIES.get(country)
                period = infer_period(lo, hi) or "year"

        contract_time = r.get("contract_time")
        contract_type = str(r.get("contract_type") or "").lower()
        employment = normalize_employment_type(contract_time)
        if contract_type == "contract":
            employment = "contract"
        if contract_type:
            extra["contract_type"] = contract_type

        tags = []
        if category.get("tag") == "graduate-jobs":
            tags.append("graduate-program")
        if employment == "internship":
            tags.append("internship")

        remote = location_remote(location)
        if remote is None and re.search(r"\bremote\b", title, re.I):
            remote = True
        return Job(
            source=self.name,
            source_id=rid,
            title=title,
            company=company or "",
            url=url,
            location=location,
            remote=remote,
            description=description,
            posted_at=iso_date(r.get("created")),
            salary_min=lo,
            salary_max=hi if hi is not None else lo,
            salary_currency=currency,
            salary_period=period,
            employment_type=employment,
            department=(category.get("label") or None),
            tags=tags,
            extra=extra,
        )
