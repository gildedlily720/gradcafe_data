"""Jobvite career sites (HTML; Jobvite has no public JSON list API).

Endpoints (verified live 2026-10-25):

* list: ``GET https://jobs.jobvite.com/{board}/jobs`` -> server-rendered HTML with every open job on one
  page (``?p=N`` does not paginate). Two templates exist: a table (``<td class="jv-job-list-name"><a
  href="/{board}/job/{id}">Title</a></td><td class="jv-job-list-location">City, Region</td>``) and a list
  (``<a href="/{board}/job/{id}"><div class="jv-job-list-name">…</div><div class="jv-job-list-location">``).
  Jobs are grouped under ``<h3>`` category headings. Multi-location jobs show "N Locations".
  ``https://jobs.jobvite.com/careers/{board}/jobs`` serves the same page.
* detail: ``GET https://jobs.jobvite.com/{board}/job/{id}`` -> HTML with a schema.org ``JobPosting``
  JSON-LD block (description HTML, datePosted, employmentType, jobLocation[], baseSalary).
* The legacy XML feed ``https://app.jobvite.com/CompanyJobs/Xml.aspx?c={companyId}`` is opt-in per
  company and returned an empty body for the boards tested; it is only tried, best effort, when the
  board looks like a company id and the HTML page lists nothing.

Board = career-site slug (``mercycorps``, ``brg``). ``source_id`` = Jobvite job id (global, e.g.
``oQnAvfw2``). The list has no descriptions or dates: ``enrich`` fills them from the detail JSON-LD.
Verified employers: Mercy Corps (``mercycorps``, 29 jobs), Berkeley Research Group (``brg``, EMEA board).
"""

from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

from ..models import Employer, Job
from ..text import html_to_text, iso_date
from .base import Source, SourceError
from .detect import (
    join_locations,
    normalize_employment_type,
    normalize_period,
    remote_from_text,
    structured_tags,
    to_float,
)

BASE = "https://jobs.jobvite.com"
_JSONLD_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
_VOID = {"br", "img", "input", "meta", "link", "hr", "source", "wbr", "col", "area", "base", "embed"}
_MULTI_LOC = re.compile(r"^\d+\s+locations?$", re.I)
_COMPANY_ID = re.compile(r"^q[A-Za-z0-9]{7}$")


# ---- schema.org JobPosting helpers (shared with breezy.py / paylocity.py) -----------------------------


def jsonld_job_posting(html: str | None) -> dict[str, Any] | None:
    """Return the first schema.org ``JobPosting`` object embedded as JSON-LD in ``html``."""
    for m in _JSONLD_RE.finditer(html or ""):
        try:
            data = json.loads(m.group(1).strip(), strict=False)
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop(0)
            if isinstance(item, dict):
                if str(item.get("@type", "")).strip("'\"") == "JobPosting":
                    return item
                stack.extend(item.get("@graph") or [])
    return None


def jsonld_locations(posting: dict[str, Any]) -> str:
    places = posting.get("jobLocation")
    places = places if isinstance(places, list) else [places]
    out = []
    for place in places:
        addr = place.get("address") if isinstance(place, dict) else None
        if isinstance(addr, dict):
            out.append(
                ", ".join(
                    str(addr[k]).strip()
                    for k in ("addressLocality", "addressRegion", "addressCountry")
                    if isinstance(addr.get(k), (str, int)) and str(addr[k]).strip()
                )
            )
    return join_locations(out)


def jsonld_salary(posting: dict[str, Any]) -> tuple[float | None, float | None, str | None, str | None]:
    sal = posting.get("baseSalary")
    if not isinstance(sal, dict):
        return None, None, None, None
    value = sal.get("value") if isinstance(sal.get("value"), dict) else {}
    lo = to_float(value.get("minValue")) if value else None
    hi = to_float(value.get("maxValue")) if value else None
    if lo is None and hi is None:
        lo = hi = to_float(sal.get("value")) if not isinstance(sal.get("value"), dict) else None
    if lo is None and hi is None:
        return None, None, None, None
    return lo, hi if hi is not None else lo, sal.get("currency") or None, normalize_period(value.get("unitText"))


# ---- list page parser ----------------------------------------------------------------------------


class _ListParser(HTMLParser):
    """Collect ``{id, title, location, category}`` rows from both Jobvite list templates."""

    def __init__(self, board: str) -> None:
        super().__init__(convert_charrefs=True)
        self.href_re = re.compile(r"/job/([A-Za-z0-9]+)/?(?:\?.*)?$")
        self.rows: list[dict[str, str]] = []
        self.category = ""
        self._cur: dict[str, str] | None = None
        self._field: str | None = None  # "title" | "location" | "category"
        self._depth = 0
        self._buf: list[str] = []

    def _begin(self, field: str) -> None:
        self._field, self._depth, self._buf = field, 1, []

    def _end(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if self._field == "category":
            self.category = text
        elif self._cur is not None and self._field:
            self._cur[self._field] = text
        self._field = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if self._field:
            if tag not in _VOID:
                self._depth += 1
        classes = a.get("class", "").split()
        if tag == "a":
            m = self.href_re.search(a.get("href", ""))
            if m:
                if self._cur is None or self._cur.get("id"):
                    self._new()
                assert self._cur is not None
                self._cur["id"] = m.group(1)
        if self._field:
            return
        if tag == "h3" or (tag == "h2" and "h2" in classes):
            self._begin("category")
        elif "jv-job-list-name" in classes:
            if self._cur is None or self._cur.get("title"):
                self._new()
            self._begin("title")
        elif "jv-job-list-location" in classes and self._cur is not None:
            self._begin("location")

    def _new(self) -> None:
        self._cur = {"category": self.category}
        self.rows.append(self._cur)

    def handle_endtag(self, tag: str) -> None:
        if self._field and tag not in _VOID:
            self._depth -= 1
            if self._depth <= 0:
                self._end()

    def handle_data(self, data: str) -> None:
        if self._field:
            self._buf.append(data)


def parse_job_list(html: str, board: str) -> list[dict[str, str]]:
    parser = _ListParser(board)
    parser.feed(html)
    parser.close()
    seen: set[str] = set()
    rows = []
    for row in parser.rows:
        jid = row.get("id")
        if jid and row.get("title") and jid not in seen:
            seen.add(jid)
            rows.append(row)
    return rows


def _feed_rows(xml_text: str) -> list[dict[str, str]]:
    """Best-effort parse of the legacy ``CompanyJobs/Xml.aspx`` feed (``<job><id>…<title>…``)."""
    rows = []
    for block in re.findall(r"<job>(.*?)</job>", xml_text, re.S):
        def field(name: str, block: str = block) -> str:
            m = re.search(rf"<{name}>(.*?)</{name}>", block, re.S)
            return unescape(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S)).strip() if m else ""

        if field("id") and field("title"):
            rows.append(
                {
                    "id": field("id"),
                    "title": field("title"),
                    "location": field("location"),
                    "category": field("category"),
                    "description": field("description"),
                    "date": field("date"),
                    "jobtype": field("jobtype"),
                    "url": field("detail-url"),
                }
            )
    return rows


class JobviteSource(Source):
    name = "jobvite"
    kind = "board"
    label = "Jobvite career sites"
    docs_url = "https://jobs.jobvite.com/"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        board = (employer.board or "").strip().strip("/")
        if not board:
            raise SourceError(f"jobvite: employer {employer.key} has no board")
        board = re.sub(r"^https?://jobs\.jobvite\.com/(?:careers/)?", "", board).split("/")[0]
        html = await self.get_text(f"{BASE}/{board}/jobs", headers={"Accept": "text/html"})
        rows = parse_job_list(html, board)
        if not rows and _COMPANY_ID.match(board):
            try:
                rows = _feed_rows(await self.get_text(f"https://app.jobvite.com/CompanyJobs/Xml.aspx?c={board}"))
            except Exception as exc:  # noqa: BLE001 - legacy feed is optional
                self.ctx.log.debug("jobvite feed %s failed: %s", board, exc)
        if not rows and "jv-" not in html and "jobvite" not in html.lower():
            raise SourceError(f"jobvite {board}: page is not a Jobvite career site")
        jobs: list[Job] = []
        for row in rows:
            try:
                jobs.append(self._parse(row, employer, board))
            except Exception as exc:  # noqa: BLE001 - one bad row must not sink the board
                self.ctx.log.warning("jobvite %s: skipping row %r (%s)", board, row.get("id"), exc)
        return jobs[: employer.max_jobs] if employer.max_jobs else jobs

    def _parse(self, row: dict[str, str], employer: Employer, board: str) -> Job:
        jid = row["id"]
        location = row.get("location", "")
        extra: dict[str, Any] = {"board": board}
        if _MULTI_LOC.match(location):
            extra["locations_count"] = int(location.split()[0])
            location = ""
        category = row.get("category") or None
        emp_type = normalize_employment_type(row.get("jobtype")) if row.get("jobtype") else None
        return Job(
            source=self.name,
            source_id=jid,
            title=row["title"],
            company=employer.name,
            url=row.get("url") or f"{BASE}/{board}/job/{jid}",
            location=re.sub(r"\s*,\s*", ", ", location),
            remote=True if remote_from_text(location) is True else None,
            description=html_to_text(row.get("description")),
            posted_at=iso_date(row.get("date")),
            employment_type=emp_type,
            department=category,
            employer_key=employer.key,
            tags=structured_tags(category, row.get("jobtype")),
            extra=extra,
        )

    async def enrich(self, job: Job) -> Job:
        """Fill description, date, type, locations and pay from the detail page's JSON-LD."""
        board = (job.extra or {}).get("board")
        if not board or (job.description and job.posted_at):
            return job
        try:
            html = await self.get_text(f"{BASE}/{board}/job/{job.source_id}", headers={"Accept": "text/html"})
        except Exception as exc:  # noqa: BLE001 - enrichment is best effort
            self.ctx.log.warning("jobvite enrich %s failed: %s", job.uid, exc)
            return job
        posting = jsonld_job_posting(html)
        if not posting:
            return job
        job.description = html_to_text(posting.get("description")) or job.description
        job.posted_at = job.posted_at or iso_date(posting.get("datePosted"))
        job.closes_at = job.closes_at or iso_date(posting.get("validThrough"))
        if posting.get("employmentType") and not job.employment_type:
            job.employment_type = normalize_employment_type(posting["employmentType"])
            for tag in structured_tags(posting["employmentType"]):
                if tag not in job.tags:
                    job.tags.append(tag)
        locations = jsonld_locations(posting)
        if locations and not job.location:
            job.location = locations
        if job.salary_min is None:
            lo, hi, cur, period = jsonld_salary(posting)
            if lo is not None:
                job.salary_min, job.salary_max, job.salary_currency, job.salary_period = lo, hi, cur, period
        return job
