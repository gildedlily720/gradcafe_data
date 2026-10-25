"""Oracle Recruiting Cloud (Fusion HCM "Candidate Experience") job sites.

API (verified live 2026-10-25 against ``jpmc.fa.oraclecloud.com`` / ``CX_1001``):

* search: ``GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true
  &expand=requisitionList.secondaryLocations,flexFieldsFacet.values&finder=findReqs;siteNumber={site},
  facetsList=LOCATIONS,limit=50,offset=N,sortBy=POSTING_DATES_DESC[,keyword="…"]`` ->
  ``{"items": [{"TotalJobsCount": n, "requisitionList": [{Id, Title, PostedDate, PostingEndDate,
  PrimaryLocation, PrimaryLocationCountry, secondaryLocations[{Name}], WorkplaceTypeCode, WorkplaceType,
  JobFamily, JobFunction, JobSchedule, JobType, ContractType, ShortDescriptionStr, ...}]}]}``.
  Pagination is via ``offset`` inside the finder; ``limit`` up to 200 is accepted (JPMC: 7,242 jobs).
* detail: ``GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all
  &onlyData=true&finder=ById;Id="{id}",siteNumber={site}`` -> ``items[0]`` with ``ExternalDescriptionStr``,
  ``ExternalResponsibilitiesStr``, ``ExternalQualificationsStr``, ``JobSchedule``, ``workLocation`` ...
* public URL: ``https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{id}``.

Board = ``host/siteNumber`` (e.g. ``jpmc.fa.oraclecloud.com/CX_1001``) or a Candidate Experience URL.
Tenants can be huge: ``employer.search_terms`` run one keyword search each (results de-duplicated, newest
first) and ``employer.max_jobs`` defaults to 400 (applied per search and overall). ``source_id`` =
``"{tenant}:{Id}"`` where tenant is the first host label (requisition ids are per tenant).
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Employer, Job
from ..text import html_to_text, iso_date, truncate
from .base import Source, SourceError
from .detect import join_locations, normalize_employment_type, remote_from_text, safe_parse, structured_tags

PAGE = 50
DEFAULT_MAX_JOBS = 400
_WORKPLACE = {"ORA_REMOTE": True, "ORA_HYBRID": False, "ORA_ON_SITE": False, "ORA_ONSITE": False}
_BOARD_RE = re.compile(
    r"(?:https?://)?([a-z0-9-]+(?:\.[a-z0-9-]+)*\.oraclecloud\.com)/(?:hcmUI/CandidateExperience/[A-Za-z-]+/sites/)?([A-Za-z0-9_]+)",
    re.I,
)


def parse_board(board: str) -> tuple[str, str]:
    m = _BOARD_RE.search((board or "").strip())
    if not m:
        raise SourceError(f"oracle: board {board!r} must look like 'host.fa.oraclecloud.com/CX_1'")
    return m.group(1).lower(), m.group(2)


def finder(site: str, offset: int, limit: int = PAGE, keyword: str | None = None) -> str:
    parts = [
        f"findReqs;siteNumber={site}",
        "facetsList=LOCATIONS",
        f"limit={limit}",
        f"offset={offset}",
        "sortBy=POSTING_DATES_DESC",
    ]
    if keyword:
        kw = re.sub(r'["\\]', " ", keyword).strip()
        if kw:
            parts.append(f'keyword="{kw}"')
    return ",".join(parts)


def _employment(*values: Any) -> str | None:
    for v in values:
        if isinstance(v, str) and v.strip():
            return normalize_employment_type(v)
    return None


class OracleHCMSource(Source):
    name = "oracle"
    kind = "board"
    label = "Oracle Recruiting Cloud job sites"
    docs_url = "https://docs.oracle.com/en/cloud/saas/human-resources/farws/api-recruiting-candidate-experience-job-requisitions.html"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        host, site = parse_board(employer.board or "")
        cap = employer.max_jobs or DEFAULT_MAX_JOBS
        terms = [t.strip() for t in employer.search_terms if t and t.strip()] or [None]
        seen: dict[str, dict] = {}
        for term in terms:
            await self._search(host, site, term, seen, cap)
            if len(seen) >= cap:
                break
        tenant = host.split(".")[0]
        jobs = safe_parse(
            list(seen.values()),
            lambda r: self._parse(r, employer, host, site, tenant),
            source=self.name,
            board=f"{host}/{site}",
            logger=self.ctx.log,
        )
        return jobs[:cap]

    async def _search(self, host: str, site: str, term: str | None, seen: dict[str, dict], cap: int) -> None:
        url = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        offset, got = 0, 0
        while True:
            data = await self.get_json(
                url,
                params={
                    "onlyData": "true",
                    "expand": "requisitionList.secondaryLocations,flexFieldsFacet.values",
                    "finder": finder(site, offset, keyword=term),
                },
            )
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise SourceError(f"oracle {host}/{site}: unexpected payload")
            if not items:
                return
            head = items[0] if isinstance(items[0], dict) else {}
            reqs = [r for r in head.get("requisitionList") or [] if isinstance(r, dict) and r.get("Id")]
            for r in reqs:
                seen.setdefault(str(r["Id"]), r)
            got += len(reqs)
            offset += len(reqs)
            total = head.get("TotalJobsCount")
            if not reqs or got >= cap or len(seen) >= cap or (isinstance(total, int) and offset >= total):
                return

    def _parse(self, r: dict, employer: Employer, host: str, site: str, tenant: str) -> Job | None:
        rid, title = str(r.get("Id") or ""), (r.get("Title") or "").strip()
        if not rid or not title:
            return None
        secondary = [s.get("Name") for s in r.get("secondaryLocations") or [] if isinstance(s, dict)]
        remote = _WORKPLACE.get(str(r.get("WorkplaceTypeCode") or "").upper())
        if remote is None:
            remote = remote_from_text(r.get("WorkplaceType"))
        emp_type = _employment(r.get("JobSchedule"), r.get("JobType"), r.get("ContractType"), r.get("WorkerType"))
        family, function = r.get("JobFamily") or None, r.get("JobFunction") or None
        extra: dict[str, Any] = {"host": host, "site": site, "id": rid}
        for key, field in (("job_family", "JobFamily"), ("job_function", "JobFunction"), ("study_level", "StudyLevel")):
            if r.get(field):
                extra[key] = r[field]
        return Job(
            source=self.name,
            source_id=f"{tenant}:{rid}",
            title=title,
            company=employer.name,
            url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
            location=join_locations([r.get("PrimaryLocation"), *secondary]),
            remote=remote,
            # ShortDescriptionStr is a one-line summary; enrich() fetches the full text
            description=html_to_text(r.get("ShortDescriptionStr")),
            posted_at=iso_date(r.get("PostedDate")),
            closes_at=iso_date(r.get("PostingEndDate")),
            employment_type=emp_type,
            department=family or function,
            employer_key=employer.key,
            tags=structured_tags(r.get("JobType"), r.get("ContractType"), r.get("StudyLevel"), family),
            extra=extra,
        )

    async def enrich(self, job: Job) -> Job:
        """Fetch the requisition detail (description, responsibilities, qualifications)."""
        extra = job.extra or {}
        host, site, rid = extra.get("host"), extra.get("site"), extra.get("id")
        if not host or not site or not rid or extra.get("enriched"):
            return job
        try:
            data = await self.get_json(
                f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
                params={"expand": "all", "onlyData": "true", "finder": f'ById;Id="{rid}",siteNumber={site}'},
            )
        except Exception as exc:  # noqa: BLE001 - enrichment is best effort
            self.ctx.log.warning("oracle enrich %s failed: %s", job.uid, exc)
            return job
        items = data.get("items") if isinstance(data, dict) else None
        d = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
        if not d:
            return job
        job.extra["enriched"] = True
        sections = [
            html_to_text(d.get("ExternalDescriptionStr"), limit=0),
            html_to_text(d.get("ExternalResponsibilitiesStr"), limit=0),
            html_to_text(d.get("ExternalQualificationsStr"), limit=0),
        ]
        text = truncate("\n\n".join(s for s in sections if s))
        if text:
            job.description = text
        job.employment_type = job.employment_type or _employment(d.get("JobSchedule"), d.get("JobType"), d.get("ContractType"))
        job.closes_at = job.closes_at or iso_date(d.get("ExternalPostedEndDate"))
        if job.remote is None:
            job.remote = _WORKPLACE.get(str(d.get("WorkplaceTypeCode") or "").upper())
        if d.get("BusinessUnit"):
            job.extra["business_unit"] = d["BusinessUnit"]
        return job
