"""Apify actors run synchronously (metered: pay per run/result).

API: ``POST https://api.apify.com/v2/acts/{actorId}/run-sync-get-dataset-items`` (actor id ``user~name``),
JSON body = actor input, header ``Authorization: Bearer <APIFY_TOKEN>`` (equivalent to ``?token=``; the
header keeps the token out of URLs and error messages). Query params used: ``timeout`` (run timeout, s),
``maxItems`` (caps paid results), ``maxTotalChargeUsd`` (optional cost cap), ``format=json``, ``clean=1``,
``limit``. The endpoint waits at most 300 s and answers 408 when the run takes longer.
Docs: https://docs.apify.com/api/v2/act-run-sync-get-dataset-items-post (verified 2026-10-25: 401
``user-or-token-not-found`` without a valid token).

Default actors (input schemas read from the public build API ``/v2/acts/{id}`` + ``/v2/actor-builds/{id}``):

* ``blackfalcondata~hiringcafe-scraper`` (default, hiring.cafe): input ``query, country (US, GB, ...),
  location, workplaceTypes [Remote|Hybrid|Onsite], seniorityLevels, commitmentTypes, postedWithinDays,
  sortBy (date), maxResults, includeDetails, descriptionFormat (text|html|markdown|all), outputMode``;
  items ``jobId, title, company, companyName, location, workplaceType, commitment, seniorityLevel,
  jobCategory, description, salaryMin, salaryMax, salaryCurrency, salaryFrequency, applyUrl, portalUrl,
  postedDate, sourceAts, minYearsExperience``.
* ``aakash19~jobspy`` (alias ``jobspy``): input ``search_terms[], locations[], job_boards[] (indeed,
  linkedin, glassdoor, ziprecruiter, google), results_per_search, job_type, remote_only, days_posted (1-60),
  country_code, include_description``; items follow the JobSpy columns (``id, site, job_url, title,
  company, location, date_posted, job_type, interval, min_amount, max_amount, currency, is_remote,
  description``) — mapped defensively since the actor publishes no dataset schema.

``query.extra``: ``actor`` (id, ``user/name`` or alias ``hiringcafe``/``jobspy``), ``actor_input`` (merged
over the generated input), ``timeout_secs`` (default 240), ``max_charge_usd``, ``sites`` (JobSpy actor).
Unknown actors get ``{"query", "location", "maxResults"}`` unless ``actor_input`` says otherwise.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from ..http import HTTPError
from ..models import Job, SearchQuery
from .base import Source, SourceError, SourceUnavailable
from .manual_import import job_from_record, normalize_record, pick, slug, text_value

API = "https://api.apify.com/v2"
HIRINGCAFE = "blackfalcondata~hiringcafe-scraper"
JOBSPY = "aakash19~jobspy"
DEFAULT_ACTOR = HIRINGCAFE
ALIASES = {
    "hiringcafe": HIRINGCAFE,
    "hiring.cafe": HIRINGCAFE,
    "hiring-cafe": HIRINGCAFE,
    "jobspy": JOBSPY,
}
ACTOR_TAGS = {HIRINGCAFE: "hiring-cafe", JOBSPY: "jobspy"}
DEFAULT_TIMEOUT_S = 240
MAX_ITEMS = 100
_UK = re.compile(r"\b(london|united kingdom|uk|england|scotland|wales)\b", re.I)
_REMOTE_LOC = re.compile(r"^\s*(remote|anywhere)\s*$", re.I)


def actor_id(value: str | None) -> str:
    """``hiringcafe`` / ``user/name`` / ``user~name`` -> ``user~name``."""
    raw = (value or "").strip()
    if not raw:
        return DEFAULT_ACTOR
    if raw.lower() in ALIASES:
        return ALIASES[raw.lower()]
    return raw.replace("/", "~")


def actor_tag(actor: str) -> str:
    return ACTOR_TAGS.get(actor) or slug(actor.split("~")[-1]) or "apify"


def _place(location: str | None) -> tuple[str | None, str]:
    """(city-ish filter text, ISO2 country) from a free-text location."""
    loc = (location or "").strip()
    country = "GB" if _UK.search(loc) else "US"
    if not loc or _REMOTE_LOC.match(loc) or re.fullmatch(r"(?i)united states|usa|us|uk|united kingdom", loc):
        return None, country
    return loc.split(",")[0].strip() or None, country


def build_input(actor: str, query: SearchQuery, limit: int) -> dict[str, Any]:
    keywords = (query.keywords or "").strip()
    keywords = "" if keywords == "*" else keywords
    city, country = _place(query.location)
    remote = bool(query.remote) or bool(query.location and _REMOTE_LOC.match(query.location))
    if actor == HIRINGCAFE:
        payload: dict[str, Any] = {
            "query": keywords,
            "country": country,
            "maxResults": limit,
            "sortBy": "date",
            "includeDetails": True,
            "descriptionFormat": "text",
            "descriptionMaxLength": 12000,
            "outputMode": "full",
            "postedWithinDays": int(query.posted_within_days or 0),
        }
        if city:
            payload["location"] = city
        if remote:
            payload["workplaceTypes"] = ["Remote"]
    elif actor == JOBSPY:
        sites = query.extra.get("sites") or ["indeed"]
        payload = {
            "search_terms": [keywords] if keywords else [],
            "locations": [query.location.strip() if query.location and not remote else "United States"],
            "job_boards": [sites] if isinstance(sites, str) else list(sites),
            "results_per_search": limit,
            "days_posted": max(1, min(int(query.posted_within_days or 7), 60)),
            "country_code": country,
            "remote_only": remote,
            "include_description": True,
            "job_type": "any",
        }
    else:
        payload = {"query": keywords, "maxResults": limit}
        if query.location:
            payload["location"] = query.location
    custom = query.extra.get("actor_input")
    if isinstance(custom, dict):
        payload.update(custom)
    return payload


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "data", "results"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
    return []


class ApifySource(Source):
    name = "apify"
    kind = "search"
    label = "Apify actors (hiring.cafe scraper, JobSpy actor)"
    requires_env = ("APIFY_TOKEN",)
    metered = True
    docs_url = "https://docs.apify.com/api/v2/act-run-sync-get-dataset-items-post"
    max_queries = 5

    async def search(self, query: SearchQuery) -> list[Job]:
        token = self.ctx.settings.get("APIFY_TOKEN")
        if not token:
            raise SourceUnavailable("apify needs APIFY_TOKEN")
        actor = actor_id(query.extra.get("actor"))
        limit = max(1, min(int(query.limit or 50), MAX_ITEMS))
        payload = build_input(actor, query, limit)
        timeout_s = int(query.extra.get("timeout_secs") or DEFAULT_TIMEOUT_S)
        params: dict[str, Any] = {
            "timeout": timeout_s,
            "maxItems": limit,
            "limit": limit,
            "format": "json",
            "clean": 1,
        }
        if query.extra.get("max_charge_usd"):
            params["maxTotalChargeUsd"] = query.extra["max_charge_usd"]
        items = await self._run_sync(actor, payload, params, token, timeout_s)
        tag = actor_tag(actor)
        jobs: list[Job] = []
        seen: set[str] = set()
        for item in items:
            try:
                job = self._job(item, tag, actor)
            except Exception as exc:  # noqa: BLE001
                self.ctx.log.debug("apify: skipped malformed item: %s", exc)
                continue
            if job and job.source_id not in seen:
                seen.add(job.source_id)
                jobs.append(job)
        return jobs[:limit]

    async def _run_sync(
        self, actor: str, payload: dict[str, Any], params: dict[str, Any], token: str, timeout_s: int
    ) -> list[dict[str, Any]]:
        """One paid actor run. Sent directly on the shared client: the run needs a per-request timeout
        longer than the client's default (``self.post_json`` cannot pass one) and must never be retried."""
        url = f"{API}/acts/{quote(actor, safe='~')}/run-sync-get-dataset-items"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(min(timeout_s, 300) + 45.0, connect=15.0)
        try:
            async with self.ctx.limiter.for_url(url):
                resp = await self.ctx.client.post(url, params=params, json=payload, headers=headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise SourceError(f"apify: actor {actor} did not answer within {timeout.read:.0f}s") from exc
        except httpx.TransportError as exc:
            raise SourceError(f"apify: {type(exc).__name__}: {exc}") from exc
        if resp.status_code == 408:
            raise SourceError(f"apify: actor {actor} ran longer than 300 s (sync limit); lower maxResults")
        if resp.status_code >= 400:
            raise HTTPError(resp.status_code, url, resp.text)
        try:
            return _items(resp.json())
        except ValueError as exc:
            raise HTTPError(resp.status_code, url, "invalid JSON: " + resp.text[:200]) from exc

    def _job(self, item: dict[str, Any], tag: str, actor: str) -> Job | None:
        job = job_from_record(item, source=self.name, id_prefix=tag, tags=[tag])
        if job is None:
            return None
        n = normalize_record(item)
        site = text_value(n.get("site"))
        if site and site.lower() not in job.tags:
            job.tags.append(site.lower())
        if not job.company:
            job.company = text_value(pick(n, ("companyname", "company"))) or ""
        job.extra["actor"] = actor
        ats = text_value(pick(n, ("sourceats", "atssource")))
        if ats:
            job.extra["ats"] = ats
        portal = text_value(pick(n, ("portalurl",)))
        if portal and portal != job.url:
            job.extra["portal_url"] = portal
        years = pick(n, ("minyearsexperience",))
        if isinstance(years, (int, float)):
            job.extra["min_years_experience"] = years
        return job
