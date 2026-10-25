"""UKG Pro (UltiPro) Recruiting job boards.

Endpoints (verified live 2026-10-25):

* search: ``POST https://recruiting.ultipro.com/{tenant}/JobBoard/{boardGuid}/JobBoardView/LoadSearchResults``
  with body ``{"opportunitySearch": {"Top": 50, "Skip": N, "QueryString": "<keywords>", "OrderBy":
  [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": false}], "Filters": []}}`` ->
  ``{"opportunities": [{Id, Title, RequisitionNumber, FullTime, JobCategoryName, Locations[{Address{City,
  State, Country{Code,Name}}, LocalizedDescription}], PostedDate, BriefDescription, JobLocationType}],
  "totalCount": n}``. ``QueryString`` is a server-side full-text search (any-word match) and is used for
  ``employer.search_terms``. ``JobLocationType``: ``0`` hybrid, ``1`` on-site, ``2`` remote.
* detail: ``GET .../OpportunityDetail?opportunityId={Id}`` -> HTML embedding
  ``new US.Opportunity.CandidateOpportunityDetail({...})`` with ``Description`` (HTML) and compensation
  fields; used by ``enrich``. The same URL is the public posting page.

Board = ``tenantAlias/boardGuid`` (``recruiting.ultipro.com``) or ``host/tenantAlias/boardGuid`` for other
UKG hosts (``recruiting2.ultipro.com``, ``recruiting.ultipro.ca``); when a bare board fails on
``recruiting.ultipro.com`` the adapter retries ``recruiting2.ultipro.com``. ``source_id`` = opportunity GUID.
Verified employer: IREX (``INT1065INRE/86f30819-0126-446a-84bb-cf37a5e878c1``); NDI
(``NAT1051NDEM/79dbb2b6-6a4f-4628-a401-e37889b022fc``) is a valid board that was empty at verification time.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..http import HTTPError
from ..models import Employer, Job
from ..text import html_to_text, iso_date
from .base import Source, SourceError
from .detect import join_locations, safe_parse, structured_tags, to_float

DEFAULT_HOST = "recruiting.ultipro.com"
ALT_HOST = "recruiting2.ultipro.com"
PAGE = 50
DEFAULT_MAX_JOBS = 1000
_REMOTE = {0: False, 1: False, 2: True}
_DETAIL_RE = re.compile(r"CandidateOpportunityDetail\((\{.*?\})\);\s*\n", re.S)


def parse_board(board: str) -> tuple[str | None, str, str]:
    """Board -> (explicit host or None, tenant alias, board GUID)."""
    raw = re.sub(r"^https?://", "", (board or "").strip()).strip("/")
    m = re.search(r"(?:^|/)([A-Za-z0-9]+)/(?:JobBoard/)?([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})", raw)
    if not m:
        raise SourceError(f"ultipro: board {board!r} must look like 'TENANT/board-guid'")
    host = raw[: m.start()].split("/")[0] if m.start() > 0 else None
    return (host or None), m.group(1), m.group(2).lower()


def search_body(query: str, skip: int, top: int = PAGE) -> dict[str, Any]:
    return {
        "opportunitySearch": {
            "Top": top,
            "Skip": skip,
            "QueryString": query,
            "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
            "Filters": [],
        }
    }


def _location(loc: Any) -> str:
    if not isinstance(loc, dict):
        return ""
    addr = loc.get("Address") if isinstance(loc.get("Address"), dict) else {}
    state = addr.get("State")
    state = (state.get("Code") or state.get("Name")) if isinstance(state, dict) else state
    country = addr.get("Country")
    country = (country.get("Name") or country.get("Code")) if isinstance(country, dict) else country
    parts = [p for p in (addr.get("City"), state, country) if isinstance(p, str) and p.strip()]
    if parts:
        return ", ".join(parts)
    return str(loc.get("LocalizedName") or loc.get("LocalizedDescription") or "")


def detail_json(html: str) -> dict[str, Any] | None:
    m = _DETAIL_RE.search(html or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class UltiProSource(Source):
    name = "ultipro"
    kind = "board"
    label = "UKG Pro (UltiPro) job boards"
    docs_url = "https://www.ukg.com/"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        host, tenant, guid = parse_board(employer.board or "")
        cap = employer.max_jobs or DEFAULT_MAX_JOBS
        queries = [t.strip() for t in employer.search_terms if t and t.strip()] or [""]
        seen: dict[str, dict] = {}
        for query in queries:
            host = await self._collect(host, tenant, guid, query, seen, cap)
            if len(seen) >= cap:
                break
        base = f"https://{host}/{tenant}/JobBoard/{guid}"
        jobs = safe_parse(
            list(seen.values()),
            lambda o: self._parse(o, employer, base),
            source=self.name,
            board=f"{tenant}/{guid}",
            logger=self.ctx.log,
        )
        return jobs[:cap]

    async def _collect(
        self, host: str | None, tenant: str, guid: str, query: str, seen: dict[str, dict], cap: int
    ) -> str:
        """Page through one search; returns the host that answered."""
        hosts = [host] if host else [DEFAULT_HOST, ALT_HOST]
        skip = 0
        while True:
            data, used = None, hosts[0]
            for i, h in enumerate(hosts):
                try:
                    data = await self.post_json(
                        f"https://{h}/{tenant}/JobBoard/{guid}/JobBoardView/LoadSearchResults",
                        search_body(query, skip),
                    )
                    used = h
                    break
                except HTTPError:
                    if i == len(hosts) - 1 or skip > 0:
                        raise
            hosts = [used]
            if not isinstance(data, dict) or not isinstance(data.get("opportunities"), list):
                raise SourceError(f"ultipro {tenant}/{guid}: unexpected payload")
            opps = [o for o in data["opportunities"] if isinstance(o, dict) and o.get("Id")]
            for o in opps:
                seen.setdefault(str(o["Id"]), o)
            skip += len(opps)
            total = data.get("totalCount")
            if not opps or len(seen) >= cap or (isinstance(total, int) and skip >= total):
                return used

    def _parse(self, o: dict, employer: Employer, base: str) -> Job | None:
        oid, title = o.get("Id"), (o.get("Title") or "").strip()
        if not oid or not title:
            return None
        full_time = o.get("FullTime")
        emp_type = "full-time" if full_time is True else ("part-time" if full_time is False else None)
        department = o.get("JobCategoryName") or None
        extra: dict[str, Any] = {"board": base}
        if o.get("RequisitionNumber"):
            extra["requisition_id"] = o["RequisitionNumber"]
        if o.get("Featured"):
            extra["featured"] = True
        return Job(
            source=self.name,
            source_id=str(oid),
            title=title,
            company=employer.name,
            url=f"{base}/OpportunityDetail?opportunityId={oid}",
            location=join_locations(_location(loc) for loc in o.get("Locations") or []),
            remote=_REMOTE.get(o.get("JobLocationType")),
            # BriefDescription is a summary; enrich() swaps in the full Description
            description=html_to_text(o.get("BriefDescription")),
            posted_at=iso_date(o.get("PostedDate")),
            employment_type=emp_type,
            department=department,
            employer_key=employer.key,
            tags=structured_tags(department),
            extra=extra,
        )

    async def enrich(self, job: Job) -> Job:
        """Read the full description and pay range from the opportunity detail page."""
        base = (job.extra or {}).get("board")
        if not base or job.extra.get("enriched"):
            return job
        try:
            html = await self.get_text(
                f"{base}/OpportunityDetail", params={"opportunityId": job.source_id}, headers={"Accept": "text/html"}
            )
        except Exception as exc:  # noqa: BLE001 - enrichment is best effort
            self.ctx.log.warning("ultipro enrich %s failed: %s", job.uid, exc)
            return job
        d = detail_json(html)
        if not d:
            return job
        job.extra["enriched"] = True
        job.description = html_to_text(d.get("Description")) or job.description
        if d.get("OpportunityIsClosed"):
            job.extra["closed"] = True
        pay = d.get("PayRange") if isinstance(d.get("PayRange"), dict) else {}
        lo = to_float(d.get("CompensationAnnualMinimum")) or to_float(pay.get("PayRangeMinimum"))
        hi = to_float(d.get("CompensationAnnualMaximum")) or to_float(pay.get("PayRangeMaximum"))
        period = "year"
        if lo is None and hi is None:
            lo, hi = to_float(d.get("CompensationHourlyMinimum")), to_float(d.get("CompensationHourlyMaximum"))
            period = "hour"
        if (lo is not None or hi is not None) and job.salary_min is None:
            job.salary_min, job.salary_max = lo, hi if hi is not None else lo
            job.salary_currency = d.get("CompensationCurrencyCode") or d.get("PayRangeCurrencyCode")
            job.salary_period = period
        if d.get("HoursPerWeek") and not job.extra.get("hours_per_week"):
            job.extra["hours_per_week"] = d["HoursPerWeek"]
        return job
