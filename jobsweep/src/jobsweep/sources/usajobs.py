"""USAJOBS search (federal jobs).

Two transports, same response shape (the USAJOBS ``SearchResult`` JSON):

* **Direct API** when ``USAJOBS_API_KEY`` and ``USAJOBS_EMAIL`` are set:
  ``GET https://data.usajobs.gov/api/search`` with headers ``Host: data.usajobs.gov``,
  ``User-Agent: <email>``, ``Authorization-Key: <key>`` and params ``Keyword, PositionTitle, LocationName,
  JobCategoryCode, HiringPath, PayGradeLow, PayGradeHigh, Organization, RemoteIndicator, DatePosted,
  ResultsPerPage (<=500), Page, Fields=Full, SortField=opendate, SortDirection=Desc``. (Without a key the
  endpoint answers 401, or 403 from Akamai for non-browser user agents.)
* **Pipeworx fallback** without credentials: ``POST https://gateway.pipeworx.io/v1/tools/usajobs_search``
  with ``{keyword, location, position_title, organization, pay_grade_low, pay_grade_high, remote,
  results_per_page, page}`` (optional ``PIPEWORX_API_KEY`` appended as ``?_apiKey=``). Verified 2026-10-25:
  the gateway returns the full USAJOBS document (``UserArea.Details`` included), the anonymous tier is
  50 calls/day (``X-RateLimit-*`` headers), pay grades must be two-digit numbers (``"05"``; ``"GS5"`` is a
  400 upstream), and it silently drops ``job_category_code`` / ``hiring_path`` / date filters — so
  series, hiring path and ``posted_within_days`` are post-filtered here.

``SearchQuery.extra`` keys: ``series`` (list of 4-digit codes), ``hiring_path`` (list, e.g.
``["graduates", "student", "public"]``), ``grade_low`` / ``grade_high`` (``"05"``, ``"GS-7"``, 9 ...),
``organization`` (agency code(s)), ``position_title``.

Code lists (``https://data.usajobs.gov/api/codelist/<name>``, fetched 2026-10-25) used for the mappings:
``positionscheduletypes``, ``positionofferingtypes``, ``hiringpaths``, ``remunerationrateintervalcodes``.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from ..config import Settings
from ..models import Job, SearchQuery
from ..text import MAX_DESCRIPTION_CHARS, html_to_text, iso_date, truncate
from .base import Source, SourceError

DIRECT_URL = "https://data.usajobs.gov/api/search"
PIPEWORX_URL = "https://gateway.pipeworx.io/v1/tools/usajobs_search"

MAX_PER_PAGE = 500
MAX_PAGES_DIRECT = 10
MAX_PAGES_FALLBACK = 2  # free tier is ~50 calls/day: never page deep through the gateway
MAX_LOCATIONS = 25  # some announcements list 400+ duty stations

# positionscheduletypes
_SCHEDULE = {
    "1": "full-time",
    "2": "part-time",
    "3": "full-time",  # shift work
    "4": "per-diem",  # intermittent (no fixed tour, works as needed)
    "5": "part-time",  # job sharing
    "6": "other",  # multiple schedules
}
# positionofferingtypes
_OFFERING = {
    "15317": "Permanent",
    "15318": "Temporary",
    "15319": "Term",
    "15320": "Detail",
    "15321": "Temporary promotion",
    "15322": "Seasonal",
    "15323": "Summer",
    "15324": "Presidential Management Fellows",
    "15326": "Recent graduates",
    "15327": "Multiple",
    "15328": "Internships",
    "15522": "Intermittent",
    "15667": "ICTAP Only",
    "15668": "Agency Employees Only",
    "15669": "Telework",
}
_TEMPORARY_OFFERINGS = {"15318", "15319", "15322", "15323"}
# remunerationrateintervalcodes -> Job.salary_period (others: bi-weekly, fee basis, piece work, ...)
_RATE = {"PA": "year", "PH": "hour", "PD": "day", "PM": "month"}
# hiring paths that are only open to current/former federal employees
_INTERNAL_PATHS = {
    "fed",
    "fed-competitive",
    "fed-excepted",
    "fed-internal-search",
    "fed-internal-nosearch",
    "fed-transition",
    "ses",
    "se-other",
    "land",
    "nopublic",
}
# locations kept first when an announcement lists more than MAX_LOCATIONS duty stations
_PRIORITY_LOC = re.compile(
    r"new york|district of columbia|washington, dc|anywhere|remote|negotiable|london|arlington, virginia",
    re.I,
)


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value).strip())


def _codes(items: Any, key: str = "Code") -> list[str]:
    if not isinstance(items, list):
        return []
    return [_s(i.get(key)) for i in items if isinstance(i, dict) and _s(i.get(key))]


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def normalize_grade(value: Any) -> str | None:
    """'GS-7' / 'gs07' / 7 / '7' -> '07' (the two-digit form USAJOBS and Pipeworx accept)."""
    if value is None or value == "":
        return None
    m = re.search(r"(\d{1,2})", str(value))
    return f"{int(m.group(1)):02d}" if m else None


def format_grade(plans: list[str], low: Any, high: Any) -> str | None:
    """('GS', '7', '9') -> 'GS-07/09'; equal low/high -> 'GS-13'; no grades -> 'GS'."""
    plan = "/".join(dict.fromkeys(p for p in plans if p))
    lo, hi = _s(low), _s(high)
    lo = f"{int(lo):02d}" if lo.isdigit() else lo
    hi = f"{int(hi):02d}" if hi.isdigit() else hi
    if lo and hi and lo != hi:
        grades = f"{lo}/{hi}"
    else:
        grades = lo or hi
    if plan and grades:
        return f"{plan}-{grades}"
    return plan or grades or None


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in re.split(r"[;,]", value) if v.strip()]
    if isinstance(value, (list, tuple, set)):
        return [_s(v) for v in value if _s(v)]
    return [_s(value)]


def _date_part(value: Any) -> str | None:
    s = _s(value)
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    return iso_date(s)


def _locations(desc: dict[str, Any]) -> str:
    names: list[str] = []
    for loc in desc.get("PositionLocation") or []:
        if isinstance(loc, dict):
            name = _s(loc.get("LocationName")) or _s(loc.get("CityName"))
            if name and name not in names:
                names.append(name)
    if not names:
        return _s(desc.get("PositionLocationDisplay"))
    if len(names) > MAX_LOCATIONS:
        first = [n for n in names if _PRIORITY_LOC.search(n)]
        rest = [n for n in names if n not in first]
        kept = (first + rest)[:MAX_LOCATIONS]
        return "; ".join(kept) + f"; … +{len(names) - len(kept)} more locations"
    return "; ".join(names)


def _employment_type(schedule: list[str], offering: list[str], title: str) -> str | None:
    if "15328" in offering or re.search(r"\bintern(ship)?s?\b", title, re.I):
        return "internship"
    if "15324" in offering or re.search(r"\bfellows?(hip)?\b", title, re.I):
        return "fellowship"
    sched = _SCHEDULE.get(schedule[0]) if schedule else None
    if sched == "part-time":
        return sched
    if offering and set(offering) <= _TEMPORARY_OFFERINGS:
        return "temporary"
    return sched


def _tags(
    paths: list[str], offering: list[str], title: str, summary: str, telework: Any, remote: bool | None
) -> list[str]:
    tags: list[str] = []
    text = f"{title}\n{summary}"
    if "graduates" in paths or "15326" in offering:
        tags += ["recent-graduates", "pathways"]
    if "student" in paths:
        tags.append("student")
    if "public" in paths:
        tags.append("public")
    if "15328" in offering or re.search(r"\bintern(ship)?s?\b", title, re.I):
        tags.append("internship")
    if "15324" in offering or re.search(r"presidential management fellow", text, re.I):
        tags += ["fellowship", "pathways"]
    elif re.search(r"\bfellow(ship)?s?\b", title, re.I):
        tags.append("fellowship")
    if re.search(r"\bpathways\b", text, re.I):
        tags.append("pathways")
    if re.search(r"\brecent graduates?\b", text, re.I):
        tags.append("recent-graduates")
    if re.search(r"\breservists?\b", text, re.I):
        tags.append("reservist")
    # FEMA "CORE" (Cadre of On-Call Response/Recovery Employees) is always upper case.
    if re.search(r"\bCORE\b", text) or re.search(r"cadre of on-call", text, re.I):
        tags.append("core")
    if paths and set(paths) <= _INTERNAL_PATHS:
        tags.append("internal")
    if telework is True or _s(telework).lower() == "true":
        tags.append("telework")
    if remote:
        tags.append("remote")
    return list(dict.fromkeys(tags))


def _truthy(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    s = _s(value).lower()
    if s in ("true", "yes", "1"):
        return True
    if s in ("false", "no", "0"):
        return False
    return None


def map_item(item: dict[str, Any], via: str = "direct") -> Job | None:
    """One ``SearchResultItems`` entry -> ``Job`` (None when it has no id/title)."""
    desc = item.get("MatchedObjectDescriptor") or {}
    if not isinstance(desc, dict):
        return None
    source_id = _s(item.get("MatchedObjectId")) or _s(desc.get("PositionID"))
    title = _s(desc.get("PositionTitle"))
    if not source_id or not title:
        return None
    details = (desc.get("UserArea") or {}).get("Details") or {}
    if not isinstance(details, dict):
        details = {}

    url = _s(desc.get("PositionURI")) or f"https://www.usajobs.gov/job/{source_id}"
    url = url.replace("https://www.usajobs.gov:443/", "https://www.usajobs.gov/")

    paths = [p.lower() for p in _as_list(details.get("HiringPath"))]
    offering = _codes(desc.get("PositionOfferingType"))
    schedule = _codes(desc.get("PositionSchedule"))
    summary = _s(details.get("JobSummary"))

    remote = _truthy(details.get("RemoteIndicator"))
    if re.search(r"\(remote job\)|\bremote\b", _s(desc.get("PositionLocationDisplay")), re.I):
        remote = True

    salary_min = salary_max = None
    period = currency = None
    pay = desc.get("PositionRemuneration") or []
    if isinstance(pay, list) and pay and isinstance(pay[0], dict):
        salary_min, salary_max = _num(pay[0].get("MinimumRange")), _num(pay[0].get("MaximumRange"))
        if not salary_min and not salary_max:
            salary_min = salary_max = None
        else:
            period = _RATE.get(_s(pay[0].get("RateIntervalCode")).upper())
            currency = "USD"

    duties = details.get("MajorDuties")
    duties_text = "\n".join(_s(d) for d in duties) if isinstance(duties, list) else _s(duties)
    sections = [
        ("Summary", summary),
        ("Duties", duties_text),
        ("Qualifications", _s(desc.get("QualificationSummary"))),
    ]
    body = "\n\n".join(f"{name}:\n{html_to_text(txt, limit=0)}" for name, txt in sections if txt)
    description = truncate(body, MAX_DESCRIPTION_CHARS)

    dept = _s(desc.get("DepartmentName"))
    sub = _s(desc.get("SubAgency")) or _s(details.get("SubAgencyName"))
    company = _s(desc.get("OrganizationName")) or dept or "U.S. Federal Government"
    department = dept or None
    if sub and sub not in (company, dept):
        department = f"{dept} / {sub}" if dept else sub

    extra: dict[str, Any] = {
        "announcement_number": _s(desc.get("PositionID")) or None,
        "hiring_paths": paths,
        "offering_types": [_OFFERING.get(c, c) for c in offering],
        "promotion_potential": _s(details.get("PromotionPotential")) or None,
        "security_clearance": _s(details.get("SecurityClearance")) or None,
        "telework_eligible": _truthy(details.get("TeleworkEligible")),
        "total_openings": _s(details.get("TotalOpenings")) or None,
        "organization_code": _s(details.get("OrganizationCodes")) or None,
        "via": via,
    }

    return Job(
        source="usajobs",
        source_id=source_id,
        title=title,
        company=company,
        url=url,
        location=_locations(desc),
        remote=remote,
        description=description,
        posted_at=_date_part(desc.get("PublicationStartDate")),
        closes_at=_date_part(desc.get("ApplicationCloseDate")),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=currency,
        salary_period=period,
        employment_type=_employment_type(schedule, offering, title),
        department=department,
        grade=format_grade(_codes(desc.get("JobGrade")), details.get("LowGrade"), details.get("HighGrade")),
        series=_codes(desc.get("JobCategory")),
        tags=_tags(paths, offering, title, summary, details.get("TeleworkEligible"), remote),
        extra={k: v for k, v in extra.items() if v not in (None, [], "")},
    )


def _keep(job: Job, series: set[str], paths: set[str], since: str | None) -> bool:
    if series and not series.intersection(job.series):
        return False
    if paths and not paths.intersection(job.extra.get("hiring_paths") or []):
        return False
    if since and job.posted_at and job.posted_at[:10] < since:
        return False
    return True


class USAJobsSource(Source):
    name = "usajobs"
    kind = "search"
    label = "USAJOBS (federal)"
    requires_env = ()  # works without credentials through the Pipeworx gateway
    metered = False
    docs_url = "https://developer.usajobs.gov/api-reference/get-api-search"

    @classmethod
    def direct(cls, settings: Settings) -> bool:
        return settings.has("USAJOBS_API_KEY", "USAJOBS_EMAIL")

    @classmethod
    def availability(cls, settings: Settings) -> tuple[bool, str]:
        if cls.direct(settings):
            return True, "direct API"
        return True, "Pipeworx fallback (free, ~50 calls/day)"

    @classmethod
    def query_budget(cls, settings: Settings) -> int | None:
        return 200 if cls.direct(settings) else 12

    # ------------------------------------------------------------------------------------------
    async def search(self, query: SearchQuery) -> list[Job]:
        extra = query.extra or {}
        series = {s.zfill(4) for s in _as_list(extra.get("series"))}
        paths = {p.lower() for p in _as_list(extra.get("hiring_path"))}
        days = query.posted_within_days
        since = (date.today() - timedelta(days=days)).isoformat() if days else None
        limit = max(1, int(query.limit or 50))
        direct = self.direct(self.ctx.settings)
        # Post-filters (always applied; server-side too when direct) may discard results: fetch more.
        filtered = bool(series or paths or (since and not direct))
        per_page = min(MAX_PER_PAGE, max(limit * 3, 100) if filtered else max(limit, 25))
        max_pages = MAX_PAGES_DIRECT if direct else MAX_PAGES_FALLBACK

        jobs: list[Job] = []
        seen: set[str] = set()
        page = 1
        while page <= max_pages:
            if direct:
                data = await self._direct(query, series, paths, per_page, page)
            else:
                data = await self._pipeworx(query, per_page, page)
            result = data.get("SearchResult") or {}
            items = result.get("SearchResultItems") or []
            for item in items:
                try:
                    job = map_item(item, via="direct" if direct else "pipeworx")
                except Exception as exc:  # noqa: BLE001 - one bad posting never sinks the query
                    self.ctx.log.debug("usajobs: skipping malformed item: %s", exc)
                    continue
                if job is None or job.source_id in seen or not _keep(job, series, paths, since):
                    continue
                seen.add(job.source_id)
                jobs.append(job)
            pages = _num((result.get("UserArea") or {}).get("NumberOfPages")) or 1
            if len(jobs) >= limit or len(items) < per_page or page >= pages:
                break
            page += 1
        return jobs[:limit]

    async def _direct(
        self, query: SearchQuery, series: set[str], paths: set[str], per_page: int, page: int
    ) -> dict[str, Any]:
        st = self.ctx.settings
        extra = query.extra or {}
        params: dict[str, Any] = {
            "ResultsPerPage": per_page,
            "Page": page,
            "Fields": "Full",
            "SortField": "opendate",
            "SortDirection": "Desc",
        }
        kw = (query.keywords or "").strip()
        if kw and kw != "*":
            params["Keyword"] = kw
        loc = (query.location or "").strip()
        if loc and loc.lower() != "remote":
            params["LocationName"] = loc
        if query.remote or loc.lower() == "remote":
            params["RemoteIndicator"] = "True"
        if extra.get("position_title"):
            params["PositionTitle"] = extra["position_title"]
        if series:
            params["JobCategoryCode"] = ";".join(sorted(series))
        if paths:
            params["HiringPath"] = ";".join(sorted(paths))
        if normalize_grade(extra.get("grade_low")):
            params["PayGradeLow"] = normalize_grade(extra.get("grade_low"))
        if normalize_grade(extra.get("grade_high")):
            params["PayGradeHigh"] = normalize_grade(extra.get("grade_high"))
        if extra.get("organization"):
            params["Organization"] = ";".join(_as_list(extra["organization"]))
        if query.posted_within_days:
            params["DatePosted"] = max(0, min(60, int(query.posted_within_days)))
        headers = {
            "Host": "data.usajobs.gov",
            "User-Agent": st.get("USAJOBS_EMAIL") or "",
            "Authorization-Key": st.get("USAJOBS_API_KEY") or "",
        }
        data = await self.get_json(DIRECT_URL, params=params, headers=headers)
        if not isinstance(data, dict):
            raise SourceError("usajobs: unexpected response")
        return data

    async def _pipeworx(self, query: SearchQuery, per_page: int, page: int) -> dict[str, Any]:
        extra = query.extra or {}
        body: dict[str, Any] = {"results_per_page": per_page, "page": page}
        kw = (query.keywords or "").strip()
        if kw and kw != "*":
            body["keyword"] = kw
        loc = (query.location or "").strip()
        if loc and loc.lower() != "remote":
            body["location"] = loc
        if query.remote or loc.lower() == "remote":
            body["remote"] = True
        if extra.get("position_title"):
            body["position_title"] = str(extra["position_title"])
        if extra.get("organization"):
            body["organization"] = ";".join(_as_list(extra["organization"]))
        if normalize_grade(extra.get("grade_low")):
            body["pay_grade_low"] = normalize_grade(extra.get("grade_low"))
        if normalize_grade(extra.get("grade_high")):
            body["pay_grade_high"] = normalize_grade(extra.get("grade_high"))
        params = {}
        key = self.ctx.settings.get("PIPEWORX_API_KEY")
        if key:
            params["_apiKey"] = key
        # retries=1: a 429 here is a daily quota, waiting and retrying only burns calls
        data = await self.post_json(PIPEWORX_URL, body, params=params or None, retries=1)
        if not isinstance(data, dict):
            raise SourceError("usajobs/pipeworx: unexpected response")
        if "SearchResult" not in data:
            msg = data.get("message") or data.get("error") or str(data)[:200]
            raise SourceError(f"usajobs/pipeworx: {msg}")
        return data
