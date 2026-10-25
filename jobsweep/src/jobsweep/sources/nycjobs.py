"""NYC Jobs (City of New York) via the NYC Open Data Socrata dataset ``kpav-sd4t`` ("Jobs NYC Postings").

Endpoint: ``GET https://data.cityofnewyork.us/resource/kpav-sd4t.json`` with SoQL ``$q`` (full text),
``$where``, ``$order``, ``$limit``, ``$offset``; optional ``X-App-Token`` header (``SOCRATA_APP_TOKEN``).
Verified 2026-10-25: ~2,600 rows for ~1,340 postings. A posting usually appears twice (``posting_type``
``Internal`` and ``External``, otherwise identical rows; a few appear 3-6 times), so rows are merged per
``job_id`` and ``source_id = job_id`` stays unique without a suffix. The feed also keeps postings whose
``post_until`` (text, ``19-DEC-2026``) has passed: those are dropped. ``posting_date`` is a real date column.

Public posting URL: NYC moved from a127-jobs.nyc.gov (PeopleSoft) to **cityjobs.nyc.gov** (Attrax).
``https://cityjobs.nyc.gov/job/{job_id}`` 302-redirects to the canonical slug page
(``/job/background-investigator-in-manhattan-jid-48471`` for job 806787; the ``jid`` is an internal id)
and works for Internal-only postings too; unknown ids go to /404. That short form is used as ``url``.

Feed mode: ``keywords == "*"`` or ``extra["feed"]`` returns every current posting (``search_plan`` sends
``SearchQuery("*", extra={"feed": True}, limit=5000)``). ``extra["agencies"]`` matches agency names
loosely (``"DEPT OF INVESTIGATION"`` finds ``DEPARTMENT OF INVESTIGATION``; use ``agencies()`` for the
exact list) and ``extra["career_level"]`` filters on ``Entry-Level`` / ``Experienced (non-manager)`` /
``Manager`` / ``Executive`` / ``Student``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from ..config import Settings
from ..models import Job, SearchQuery
from ..text import MAX_DESCRIPTION_CHARS, html_to_text, iso_date, truncate
from .base import Source, SourceError

API_URL = "https://data.cityofnewyork.us/resource/kpav-sd4t.json"
JOB_URL = "https://cityjobs.nyc.gov/job/{job_id}"
SEARCH_URL = "https://cityjobs.nyc.gov/jobs?keywords={job_id}"

PAGE_SIZE = 1000
MAX_LIMIT = 10000
MAX_ROWS = 25000  # hard stop for paging (rows, not postings)

_PERIOD = {"annual": "year", "hourly": "hour", "daily": "day", "weekly": "week", "monthly": "month"}
_SMALL_WORDS = {"of", "and", "for", "the", "on", "in", "to", "at"}
_ACRONYMS = {"NYC", "HRA", "CUNY", "NYPD", "FDNY", "DOI", "DOE", "DCAS", "OATH", "II", "III", "BD"}
_AGENCY_STOP = {"DEPT", "DEPARTMENT", "OF", "THE", "NYC", "AND", "FOR", "CITY", "NEW", "YORK"}
# Some DEP / watershed postings sit outside the five boroughs.
_UPSTATE = re.compile(
    r"\b(kingston|valhalla|shokan|grahamsville|downsville|walton|yorktown|carmel|mahopac|brewster|"
    r"katonah|ashokan|neversink|gilboa|delhi|margaretville|tarrytown|white plains)\b",
    re.I,
)


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value).strip())


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _today() -> date:
    """Indirection so tests can pin 'today' (the post_until filter)."""
    return date.today()


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def title_case(agency: str) -> str:
    """'DEPT OF HEALTH/MENTAL HYGIENE' -> 'Dept of Health/Mental Hygiene' (keeps NYC/HRA/... acronyms)."""

    def word(m: re.Match[str]) -> str:
        w = m.group(0)
        if w.upper() in _ACRONYMS:
            return w.upper()
        if w.lower() in _SMALL_WORDS and m.start() > 0:
            return w.lower()
        return w[:1].upper() + w[1:].lower()

    return re.sub(r"[A-Za-z][A-Za-z']*", word, agency.strip())


def company_name(agency: str) -> str:
    name = title_case(agency)
    name = re.sub(r"^NYC\s+", "", name)
    return f"NYC {name}" if name else "City of New York"


def agency_clause(agency: str) -> str:
    """Loose SoQL match for one agency: exact name OR its significant words in order."""
    upper = agency.strip().upper()
    words = [w for w in re.findall(r"[A-Z0-9]+", upper.replace("'", "")) if w not in _AGENCY_STOP]
    exact = f"upper(agency) = {_quote(upper)}"
    if not words:
        return exact
    pattern = "%" + "%".join(words) + "%"
    return f"({exact} OR upper(agency) like {_quote(pattern)})"


def parse_post_until(value: Any) -> str | None:
    """'19-DEC-2026' -> '2026-12-19' (also accepts ISO strings)."""
    s = _s(value)
    if not s:
        return None
    try:
        return datetime.strptime(s.title(), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return iso_date(s[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", s) else s)


def _location(work_location: str) -> str:
    wl = re.sub(r"\s+", " ", work_location).strip().rstrip(",")
    if not wl:
        return "New York, NY"
    if _UPSTATE.search(wl):
        return wl if re.search(r"\bN\.?Y\.?\b", wl, re.I) else f"{wl}, NY"
    return f"New York, NY ({wl})"


def _description(row: dict[str, Any]) -> str:
    sections = [
        ("", row.get("job_description")),
        ("Minimum qualifications", row.get("minimum_qual_requirements")),
        ("Preferred skills", row.get("preferred_skills")),
        ("Additional information", row.get("additional_information")),
        ("To apply", row.get("to_apply")),
        ("Hours/shift", row.get("hours_shift")),
        ("Residency", row.get("residency_requirement")),
    ]
    parts = []
    for name, raw in sections:
        txt = html_to_text(_s(raw), limit=0)
        if txt:
            parts.append(f"{name}:\n{txt}" if name else txt)
    return truncate("\n\n".join(parts), MAX_DESCRIPTION_CHARS)


def map_rows(rows: list[dict[str, Any]]) -> Job | None:
    """All rows of one ``job_id`` (Internal/External copies) -> one ``Job``."""
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return None
    row = rows[0]
    job_id = _s(row.get("job_id"))
    title = _s(row.get("business_title")) or _s(row.get("civil_service_title"))
    if not job_id or not title:
        return None
    types = sorted({_s(r.get("posting_type")) for r in rows if _s(r.get("posting_type"))})
    posted = max((d for d in (iso_date(_s(r.get("posting_date"))[:10]) for r in rows) if d), default=None)
    closes = max((d for d in (parse_post_until(r.get("post_until")) for r in rows) if d), default=None)
    updated = max((_s(r.get("posting_updated"))[:10] for r in rows if r.get("posting_updated")), default="")

    agency = _s(row.get("agency"))
    career = _s(row.get("career_level"))
    cs_title = _s(row.get("civil_service_title"))
    level = _s(row.get("level"))
    code = _s(row.get("title_code_no"))
    titles = f"{title} {cs_title}"

    lo, hi = _num(row.get("salary_range_from")), _num(row.get("salary_range_to"))
    if not lo and not hi:
        lo = hi = None
    period = _PERIOD.get(_s(row.get("salary_frequency")).lower())

    ft = _s(row.get("full_time_part_time_indicator")).upper()
    if re.search(r"\bintern(ship)?s?\b", titles, re.I):
        employment = "internship"
    elif re.search(r"\bfellow(ship)?s?\b", title, re.I):
        employment = "fellowship"
    else:
        employment = {"F": "full-time", "P": "part-time"}.get(ft)

    tags: list[str] = []
    if types == ["Internal"]:
        tags.append("internal")
    if career.lower() == "entry-level":
        tags.append("entry-level")
    if career.lower() == "student" or re.search(r"\bcollege aide\b", titles, re.I):
        tags.append("student")
    if employment == "internship":
        tags.append("internship")
    if re.search(r"\bfellow(ship)?s?\b", titles, re.I):
        tags.append("fellowship")

    extra: dict[str, Any] = {
        "agency": agency,
        "posting_types": types,
        "job_category": _s(row.get("job_category")) or None,
        "civil_service_title": cs_title or None,
        "level": level or None,
        "title_classification": _s(row.get("title_classification")) or None,
        "number_of_positions": _s(row.get("number_of_positions")) or None,
        "posting_updated": updated or None,
        "search_url": SEARCH_URL.format(job_id=job_id),
    }
    return Job(
        source="nycjobs",
        source_id=job_id,
        title=title,
        company=company_name(agency) if agency else "City of New York",
        url=JOB_URL.format(job_id=job_id),
        location=_location(_s(row.get("work_location")) or _s(row.get("work_location_1"))),
        remote=False,  # city jobs are on-site or hybrid (remote-work pilot is at most 2 days/week)
        description=_description(row),
        posted_at=posted,
        closes_at=closes,
        salary_min=lo,
        salary_max=hi,
        salary_currency="USD" if (lo or hi) else None,
        salary_period=period if (lo or hi) else None,
        employment_type=employment,
        department=_s(row.get("division_work_unit")) or None,
        seniority=career or None,
        grade=f"{cs_title} L{level}" if cs_title and level else (cs_title or None),
        series=[code] if code else [],
        tags=list(dict.fromkeys(tags)),
        extra={k: v for k, v in extra.items() if v not in (None, "", [])},
    )


def merge_rows(rows: list[dict[str, Any]], log: Any = None) -> list[Job]:
    """Group rows by job_id (first-seen order) and map each group; malformed groups are skipped."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if isinstance(r, dict) and _s(r.get("job_id")):
            groups.setdefault(_s(r.get("job_id")), []).append(r)
    jobs: list[Job] = []
    for job_id, grp in groups.items():
        try:
            job = map_rows(grp)
        except Exception as exc:  # noqa: BLE001 - one bad posting never sinks the feed
            if log is not None:
                log.debug("nycjobs: skipping malformed job %s: %s", job_id, exc)
            continue
        if job is not None:
            jobs.append(job)
    return jobs


class NYCJobsSource(Source):
    name = "nycjobs"
    kind = "search"
    label = "NYC Jobs (City of New York, Open Data)"
    requires_env = ()
    metered = False
    docs_url = "https://data.cityofnewyork.us/City-Government/Jobs-NYC-Postings/kpav-sd4t"
    max_queries = 40

    @classmethod
    def availability(cls, settings: Settings) -> tuple[bool, str]:
        if settings.get("SOCRATA_APP_TOKEN"):
            return True, "ok (Socrata app token)"
        return True, "ok (anonymous Socrata, shared rate limit)"

    def _headers(self) -> dict[str, str] | None:
        token = self.ctx.settings.get("SOCRATA_APP_TOKEN")
        return {"X-App-Token": token} if token else None

    async def _rows(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        data = await self.get_json(API_URL, params=params, headers=self._headers())
        if isinstance(data, dict) and data.get("error"):
            raise SourceError(f"nycjobs: {data.get('message') or data.get('error')}")
        if not isinstance(data, list):
            raise SourceError("nycjobs: unexpected response")
        return data

    async def search(self, query: SearchQuery) -> list[Job]:
        extra = query.extra or {}
        kw = (query.keywords or "").strip()
        feed = bool(extra.get("feed")) or kw in ("", "*")
        limit = max(1, min(int(query.limit or (5000 if feed else 50)), MAX_LIMIT))

        where: list[str] = []
        agencies = extra.get("agencies") or extra.get("agency")
        if isinstance(agencies, str):
            agencies = [agencies]
        if agencies:
            where.append("(" + " OR ".join(agency_clause(a) for a in agencies if _s(a)) + ")")
        levels = extra.get("career_level")
        if isinstance(levels, str):
            levels = [levels]
        if levels:
            where.append("career_level in (" + ", ".join(_quote(_s(lv)) for lv in levels) + ")")
        if query.posted_within_days:
            cutoff = _today() - timedelta(days=int(query.posted_within_days))
            where.append(f"posting_date >= '{cutoff.isoformat()}T00:00:00'")

        base: dict[str, Any] = {"$order": "posting_date DESC, job_id, :id"}
        if not feed:
            base["$q"] = kw
        if where:
            base["$where"] = " AND ".join(where)

        rows: list[dict[str, Any]] = []
        ids: set[str] = set()
        offset = 0
        while len(rows) < MAX_ROWS:
            page = await self._rows({**base, "$limit": PAGE_SIZE, "$offset": offset})
            rows.extend(page)
            ids.update(_s(r.get("job_id")) for r in page if isinstance(r, dict))
            # job_ids repeat across Internal/External rows: page on until enough *postings*
            if len(page) < PAGE_SIZE or len(ids) > limit * 1.3 + 10:
                break
            offset += PAGE_SIZE

        today = _today().isoformat()
        jobs = [j for j in merge_rows(rows, self.ctx.log) if not j.closes_at or j.closes_at >= today]
        return jobs[:limit]

    async def agency_counts(self) -> dict[str, int]:
        """{agency name: row count} for every agency with postings (exact names for lanes.yaml)."""
        rows = await self._rows(
            {"$select": "agency, count(*) AS n", "$group": "agency", "$order": "agency", "$limit": 5000}
        )
        out: dict[str, int] = {}
        for r in rows:
            name = _s(r.get("agency")) if isinstance(r, dict) else ""
            if name:
                out[name] = int(_num(r.get("n")) or 0)
        return out

    async def agencies(self) -> list[str]:
        """Distinct agency names currently posting (exact strings to use in ``extra.agencies``)."""
        return sorted(await self.agency_counts())
