"""Personio career pages (public XML feed).

API (verified live 2026-10-25): ``GET https://{board}.jobs.personio.de/xml`` (the ``.jobs.personio.com``
host serves the same feed). The root element is ``<workzag-jobs>`` with one ``<position>`` per open job:
``id, subcompany, office, additionalOffices/office, department, recruitingCategory, name,
jobDescriptions/jobDescription(name, value=CDATA HTML), employmentType (permanent|intern|trainee|
freelance|temporary|working_student), seniority (entry-level|experienced|executive|student), schedule
(full-time|part-time|flexible), yearsOfExperience, keywords, occupation, occupationCategory, createdAt``.
Every posting carries its full description, so no ``enrich`` call is needed. Unknown subdomains answer
404; Personio also serves placeholder "demo" boards (lorem-ipsum postings) for some unclaimed slugs.
Do not add ``?language=en``: postings without an English translation then lose their descriptions.

Board formats: ``prewave`` (= prewave.jobs.personio.de), ``acme.jobs.personio.com`` or a full careers URL.
Public posting URL: ``https://{host}/job/{id}``. ``source_id`` = ``"{board}:{id}"``.
Verified employers: Prewave (``prewave``, supply-chain risk), Democracy Reporting International
(``democracy-reporting``), HateAid (``hateaid``).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from ..models import Employer, Job
from ..text import html_to_text, iso_date, truncate
from .base import Source, SourceError
from .detect import join_locations, normalize_employment_type, remote_from_text, safe_parse, structured_tags

_HOST_RE = re.compile(r"(?:https?://)?([a-z0-9-]+)\.jobs\.personio\.(de|com)", re.I)


def personio_host(board: str) -> tuple[str, str]:
    """Board string -> (host, short board slug)."""
    board = board.strip().strip("/")
    m = _HOST_RE.match(board)
    if m:
        return f"{m.group(1).lower()}.jobs.personio.{m.group(2).lower()}", m.group(1).lower()
    slug = board.lower()
    return f"{slug}.jobs.personio.de", slug


def _text(el: ET.Element | None, tag: str) -> str:
    if el is None:
        return ""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _employment(emp_type: str, schedule: str) -> tuple[str | None, list[str]]:
    """Personio employmentType + schedule -> (normalised type, extra tags)."""
    et, sched = emp_type.lower().replace("-", "_"), schedule.lower()
    by_schedule = "part-time" if sched == "part-time" else ("full-time" if sched == "full-time" else None)
    if et == "intern":
        return "internship", ["internship"]
    if et == "trainee":  # German "Trainee" = graduate programme
        return by_schedule or "full-time", ["graduate-program"]
    if et == "working_student":
        return "part-time", ["student"]
    if et == "freelance":
        return "contract", []
    if et == "temporary":
        return "temporary", []
    if et == "permanent":
        return by_schedule or "full-time", []
    return (normalize_employment_type(emp_type) if emp_type else by_schedule), []


def _description(pos: ET.Element) -> str:
    parts = []
    for jd in pos.findall("jobDescriptions/jobDescription"):
        name, body = _text(jd, "name"), html_to_text(_text(jd, "value"), limit=0)
        if body:
            parts.append(f"{name}\n{body}" if name else body)
    return truncate("\n\n".join(parts))


class PersonioSource(Source):
    name = "personio"
    kind = "board"
    label = "Personio career pages (XML feed)"
    docs_url = "https://developer.personio.de/docs/retrieving-open-job-positions"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        if not (employer.board or "").strip():
            raise SourceError(f"personio: employer {employer.key} has no board")
        host, slug = personio_host(employer.board or "")
        raw = await self.get_text(f"https://{host}/xml", headers={"Accept": "application/xml, text/xml"})
        try:
            root = ET.fromstring(raw.encode("utf-8"))
        except ET.ParseError as exc:
            raise SourceError(f"personio {slug}: invalid XML ({exc})") from exc
        if root.tag != "workzag-jobs":
            raise SourceError(f"personio {slug}: unexpected XML root <{root.tag}>")
        items = [{"el": pos} for pos in root.findall("position")]
        jobs = safe_parse(
            items,
            lambda it: self._parse(it["el"], employer, host, slug),
            source=self.name,
            board=slug,
            logger=self.ctx.log,
        )
        return jobs[: employer.max_jobs] if employer.max_jobs else jobs

    def _parse(self, pos: ET.Element, employer: Employer, host: str, slug: str) -> Job | None:
        pid, title = _text(pos, "id"), _text(pos, "name")
        if not pid or not title:
            return None
        office = _text(pos, "office")
        offices = [office] + [(o.text or "").strip() for o in pos.findall("additionalOffices/office")]
        seniority = _text(pos, "seniority") or None
        emp_type, tags = _employment(_text(pos, "employmentType"), _text(pos, "schedule"))
        if seniority == "student" and "student" not in tags:
            tags.append("student")
        recruiting_category = _text(pos, "recruitingCategory")
        for t in structured_tags(recruiting_category):
            if t not in tags:
                tags.append(t)
        extra: dict[str, Any] = {"board": slug, "host": host, "id": pid}
        for key, tag in (
            ("subcompany", "subcompany"),
            ("recruiting_category", "recruitingCategory"),
            ("years_of_experience", "yearsOfExperience"),
            ("occupation_category", "occupationCategory"),
            ("keywords", "keywords"),
        ):
            value = _text(pos, tag)
            if value:
                extra[key] = value
        return Job(
            source=self.name,
            source_id=f"{slug}:{pid}",
            title=title,
            company=employer.name,
            url=f"https://{host}/job/{pid}",
            location=join_locations(offices),
            remote=True if remote_from_text(office) is True else None,
            description=_description(pos),
            posted_at=iso_date(_text(pos, "createdAt")),
            employment_type=emp_type,
            department=_text(pos, "department") or None,
            seniority=seniority,
            employer_key=employer.key,
            tags=tags,
            extra=extra,
        )
