"""StateJobsNY (New York State agencies) — public vacancy listing, HTML scrape (best effort).

Verified 2026-10-25 (no robots.txt; plain HTTP clients are served normally):

* ``GET https://statejobs.ny.gov/public/vacancyTable.cfm`` returns **every** vacancy open to the general
  public (~1,600 rows, ~700 KB) in one ``<table id="vacancyTable">`` (DataTables pages client side).
  Columns: Item # (vacancy id), Title (link ``vacancyDetailsView.cfm?id=``), Grade (``18`` / ``NS`` /
  ``Hourly``), Posted (``10/23/26``), Deadline, Agency (``Health, Department of``), County.
* The advanced search form posts to the same page: ``?searchResults=Yes&Keywords=…`` (phrase match over
  the posting text) plus ``isInternship=Yes``, ``isnyhelp=Yes``, ``telecommuting=Yes``, ``region9=9``
  (New York City; regions 0-9, 97 "not provided", 102 "outside NY"), ``minDate`` (``YYYY-MM-DD``).
* ``GET https://statejobs.ny.gov/public/vacancyDetailsView.cfm?id={id}`` has the details as
  ``<span class="leftCol">Label</span><span class="rightCol">Value</span>`` rows inside the sections
  ``information``, ``schedule``, ``location``, ``jobspecifics`` and ``contact`` (salary text like
  ``From $70892 to $88615 Annually`` / ``From $21.63 to $22.63 Hourly``).

The listing has no descriptions, so ``enrich(job)`` fetches the detail page (duties, qualifications,
salary, schedule, city). ``search`` enriches keyword results itself (at most ``ENRICH_CAP``); feed queries
(``keywords == "*"`` / ``extra["feed"]``) only do so when ``extra["enrich"]`` is set (True or a cap).
``extra``: ``regions`` (names such as ``"New York City"`` or codes), ``internship``, ``ny_helps``,
``telecommuting`` (bools), ``agencies`` (substring match on the agency column), ``enrich``.
"""

from __future__ import annotations

import asyncio
import html as htmllib
import re
from datetime import date, timedelta
from typing import Any

from ..config import Settings
from ..models import Job, SearchQuery
from ..text import MAX_DESCRIPTION_CHARS, html_to_text, iso_date, parse_salary, truncate
from .base import Source, SourceError

BASE_URL = "https://statejobs.ny.gov/public/"
LIST_URL = BASE_URL + "vacancyTable.cfm"
DETAIL_URL = BASE_URL + "vacancyDetailsView.cfm?id={id}"

ENRICH_CAP = 25  # detail fetches per keyword search (each is one ~26 KB page)

REGIONS = {
    "capital/saratoga": 0,
    "eastern adirondacks": 1,
    "western adirondacks": 2,
    "central new york": 3,
    "finger lakes": 4,
    "western new york": 5,
    "hudson valley": 6,
    "westchester/rockland": 7,
    "long island": 8,
    "new york city": 9,
    "nyc": 9,
    "region not provided": 97,
    "outside ny": 102,
}
_NYC_COUNTIES = {
    "new york": "Manhattan",
    "kings": "Brooklyn",
    "queens": "Queens",
    "bronx": "Bronx",
    "richmond": "Staten Island",
}
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
_LINK = re.compile(r"<a\b[^>]*href=\"([^\"]*vacancyDetailsView\.cfm\?id=(\d+))\"[^>]*>(.*?)</a>", re.S | re.I)
_PAIR = re.compile(
    r"<span class=\"leftCol\">(.*?)</span>\s*<span class=\"rightCol\">(.*?)</span>", re.S | re.I
)
_SECTION = re.compile(r"<div id=\"(information|schedule|location|jobspecifics|contact)\">", re.I)


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value).strip())


def _cell_text(raw: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


def _today() -> date:
    """Indirection so tests can pin 'today'."""
    return date.today()


def company_name(agency: str) -> tuple[str, str | None]:
    """'Health, Department of - Veterans Home at Oxford' -> ('NYS Department of Health', 'Veterans Home at Oxford')."""
    agency = re.sub(r"\s+", " ", agency).strip()
    if not agency:
        return "New York State", None
    if "new york" in agency.lower():  # e.g. "DASNY - Dormitory Authority of the State of New York"
        return agency, None
    main, _, sub = agency.partition(" - ")
    if "," in main:
        head, tail = main.rsplit(",", 1)
        main = f"{tail.strip()} {head.strip()}".strip()
    main = re.sub(r"^(NYS|New York State|State)\s+", "", main).strip()
    if re.search(r"\b(SUNY|CUNY|University|College)\b", main):
        return main, (sub.strip() or None)
    return f"NYS {main}", (sub.strip() or None)


def _grade(raw: str) -> str | None:
    raw = raw.strip()
    if raw.isdigit():
        return f"SG-{int(raw):02d}"
    return raw if raw and raw.lower() != "hourly" else None


def _location(county: str, city: str = "") -> str:
    county = county.strip()
    borough = _NYC_COUNTIES.get(county.lower())
    if borough:
        return f"New York, NY ({borough})"
    if city:
        return f"{city}, NY"
    if not county or county.lower() in ("to be determined", "tbd"):
        return "New York State (location to be determined)"
    return f"{county} County, NY"


def _title_tags(title: str) -> list[str]:
    tags = []
    if re.search(r"\bintern(ship)?s?\b", title, re.I):
        tags.append("internship")
    if re.search(r"\bfellow(ship)?s?\b", title, re.I):
        tags.append("fellowship")
    if re.search(r"\btrainee\b|\btr\s?\d\b", title, re.I):
        tags.append("trainee")
    if re.search(r"\bny\s?helps\b", title, re.I):
        tags.append("ny-helps")
    return tags


def parse_table(page: str) -> list[Job]:
    """vacancyTable.cfm HTML -> jobs (title, agency, grade, county, dates; no description)."""
    start = page.find('id="vacancyTable"')
    if start < 0:
        raise SourceError("nysjobs: vacancy table not found (site layout changed or access blocked)")
    end = page.find("</table>", start)
    table = page[start : end if end > 0 else None]
    jobs: list[Job] = []
    for row in _ROW.findall(table):
        try:
            cells = _CELL.findall(row)
            if len(cells) < 7:
                continue  # header row
            link = _LINK.search(cells[1])
            vid = _cell_text(cells[0]) or (link.group(2) if link else "")
            title = _cell_text(link.group(3) if link else cells[1])
            if not vid.isdigit() or not title:
                continue
            grade_raw, posted, deadline, agency, county = (_cell_text(c) for c in cells[2:7])
            company, sub = company_name(agency)
            extra: dict[str, Any] = {"agency": agency, "county": county, "grade_raw": grade_raw}
            jobs.append(
                Job(
                    source="nysjobs",
                    source_id=vid,
                    title=title,
                    company=company,
                    url=DETAIL_URL.format(id=vid),
                    location=_location(county),
                    remote=None,
                    posted_at=iso_date(posted),
                    closes_at=iso_date(deadline),
                    department=sub,
                    grade=_grade(grade_raw),
                    tags=_title_tags(title),
                    extra={k: v for k, v in extra.items() if v},
                )
            )
        except Exception:  # noqa: BLE001 - one odd row never sinks the listing
            continue
    return jobs


def parse_detail(page: str) -> dict[str, dict[str, str]]:
    """Detail page -> {section: {label: plain-text value}} (labels repeat across sections)."""
    out: dict[str, dict[str, str]] = {}
    parts = _SECTION.split(page)
    # parts = [prefix, name1, body1, name2, body2, ...]
    for name, body in zip(parts[1::2], parts[2::2]):
        fields: dict[str, str] = {}
        for label, value in _PAIR.findall(body):
            key = _cell_text(label)
            if key and key not in fields:
                fields[key] = html_to_text(value, limit=0)
        out[name.lower()] = fields
    return out


def _yes(value: str | None) -> bool:
    return _s(value).lower() == "yes"


def apply_detail(job: Job, page: str) -> Job:
    """Fill description, salary, schedule, location and tags from a detail page."""
    sec = parse_detail(page)
    if not sec:
        raise SourceError(f"nysjobs: no details on vacancy {job.source_id}")
    info, sched = sec.get("information", {}), sec.get("schedule", {})
    loc, spec, contact = sec.get("location", {}), sec.get("jobspecifics", {}), sec.get("contact", {})

    parts = []
    for label, text in spec.items():
        if text:
            parts.append(text if label == "Duties Description" else f"{label}:\n{text}")
    if contact.get("Notes on Applying"):
        parts.append("How to apply:\n" + contact["Notes on Applying"])
    if parts:
        job.description = truncate("\n\n".join(parts), MAX_DESCRIPTION_CHARS)

    pay = info.get("Salary Range", "")
    lo, hi, cur, period = parse_salary(pay)
    if lo or hi:
        if re.search(r"hourly", pay, re.I):
            period = "hour"
        elif re.search(r"annual", pay, re.I):
            period = "year"
        elif re.search(r"bi-?weekly", pay, re.I):
            period = None
        job.salary_min, job.salary_max, job.salary_currency, job.salary_period = lo, hi, cur or "USD", period

    etype = info.get("Employment Type", "").lower()
    if _yes(info.get("Internship")):
        job.employment_type = "internship"
    elif "part" in etype:
        job.employment_type = "part-time"
    elif "full" in etype:
        appt = info.get("Appointment Type", "").lower()
        job.employment_type = "temporary" if "temporary" in appt else "full-time"

    city, county = loc.get("City", ""), loc.get("County", "") or job.extra.get("county", "")
    if city or county:
        job.location = _location(county, city)
    job.remote = False  # telecommuting, where allowed, is partial (hybrid)

    tags = list(job.tags)
    if _yes(info.get("Internship")):
        tags.append("internship")
    if _yes(info.get("NY HELPS")):
        tags.append("ny-helps")
    if _yes(sched.get("Telecommuting allowed?")):
        tags.append("telework")
    job.tags = list(dict.fromkeys(tags))

    for key, label in (
        ("occupational_category", "Occupational Category"),
        ("appointment_type", "Appointment Type"),
        ("jurisdictional_class", "Jurisdictional Class"),
        ("bargaining_unit", "Bargaining Unit"),
        ("salary_text", "Salary Range"),
    ):
        if info.get(label):
            job.extra[key] = info[label]
    if sched.get("Telecommuting allowed?"):
        job.extra["telecommuting"] = _yes(sched.get("Telecommuting allowed?"))
    if loc.get("Street Address"):
        job.extra["street"] = loc["Street Address"]
    job.extra["enriched"] = True
    return job


class NYSJobsSource(Source):
    name = "nysjobs"
    kind = "search"
    label = "StateJobsNY (New York State)"
    requires_env = ()
    metered = False
    docs_url = "https://statejobs.ny.gov/public/vacancyTable.cfm"
    max_queries = 20

    @classmethod
    def availability(cls, settings: Settings) -> tuple[bool, str]:
        return True, "ok (public HTML listing, best effort; details fetched on enrich)"

    async def search(self, query: SearchQuery) -> list[Job]:
        extra = query.extra or {}
        kw = (query.keywords or "").strip()
        feed = bool(extra.get("feed")) or kw in ("", "*")
        limit = max(1, int(query.limit or (5000 if feed else 50)))

        params: dict[str, Any] = {}
        if not feed:
            params["Keywords"] = kw
        regions = extra.get("regions") or extra.get("region")
        if isinstance(regions, (str, int)):
            regions = [regions]
        for r in regions or []:
            code = r if isinstance(r, int) else REGIONS.get(_s(r).lower(), int(r) if _s(r).isdigit() else None)
            if code is not None:
                params[f"region{code}"] = code
        for key, param in (("internship", "isInternship"), ("ny_helps", "isnyhelp")):
            if extra.get(key):
                params[param] = "Yes"
        if extra.get("telecommuting"):
            params["telecommuting"] = "Yes"
        if params:
            params = {"searchResults": "Yes", **params}

        page = await self.get_text(LIST_URL, params=params or None)
        jobs = parse_table(page)

        agencies = extra.get("agencies")
        if isinstance(agencies, str):
            agencies = [agencies]
        if agencies:
            needles = [a.lower() for a in agencies if _s(a)]
            jobs = [j for j in jobs if any(n in _s(j.extra.get("agency")).lower() for n in needles)]
        if query.posted_within_days:
            since = (_today() - timedelta(days=int(query.posted_within_days))).isoformat()
            jobs = [j for j in jobs if not j.posted_at or j.posted_at >= since]
        jobs.sort(key=lambda j: j.posted_at or "", reverse=True)
        jobs = jobs[:limit]

        want = extra.get("enrich", not feed)
        if want:
            cap = want if isinstance(want, int) and not isinstance(want, bool) else ENRICH_CAP
            await self._enrich_many(jobs[: max(0, cap)])
        return jobs

    async def _enrich_many(self, jobs: list[Job]) -> None:
        async def one(job: Job) -> None:
            try:
                await self.enrich(job)
            except Exception as exc:  # noqa: BLE001 - listing data is still useful
                self.ctx.log.debug("nysjobs: enrich %s failed: %s", job.source_id, exc)

        await asyncio.gather(*(one(j) for j in jobs))

    async def enrich(self, job: Job) -> Job:
        if job.extra.get("enriched"):
            return job
        page = await self.get_text(DETAIL_URL.format(id=job.source_id))
        return apply_detail(job, page)
