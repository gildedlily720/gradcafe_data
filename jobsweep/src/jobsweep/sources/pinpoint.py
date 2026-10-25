"""Pinpoint career sites.

API (verified live 2026-10-25): ``GET https://{board}.pinpointhq.com/postings.json`` -> ``{"data": [{id, title,
url, path, description, key_responsibilities(+_header), skills_knowledge_expertise(+_header),
benefits(+_header) (all HTML), compensation (text), compensation_minimum/maximum/currency/frequency,
compensation_visible, deadline_at, employment_type (full_time|part_time|contract|temporary|internship|…),
employment_type_text, workplace_type (onsite|hybrid|remote), location{name, city, province, postal_code},
job{id, requisition_id, department{name}, division{name}}}]}``. Full descriptions are included; there
is no posting date. Unknown subdomains answer 404.

Board = career-site subdomain (``kharon``). ``source_id`` = ``"{board}:{id}"``.
Verified employer: Kharon (``kharon``, sanctions/illicit-finance intelligence).
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Employer, Job
from ..text import html_to_text, iso_date, truncate
from .base import Source, SourceError
from .detect import (
    join_locations,
    normalize_employment_type,
    normalize_period,
    remote_from_text,
    safe_parse,
    structured_tags,
    to_float,
)

_SECTIONS = (
    ("description", None),
    ("key_responsibilities", "key_responsibilities_header"),
    ("skills_knowledge_expertise", "skills_knowledge_expertise_header"),
    ("benefits", "benefits_header"),
)


def pinpoint_board(board: str) -> str:
    raw = re.sub(r"^https?://", "", (board or "").strip()).split("/")[0].lower()
    return raw.removesuffix(".pinpointhq.com")


def _location(loc: Any) -> str:
    if not isinstance(loc, dict):
        return ""
    city, name, province = loc.get("city"), loc.get("name"), loc.get("province")
    first = name if name and (not city or str(city).lower() not in str(name).lower()) else city
    parts = [first, province] if not (first and province and str(province) in str(first)) else [first]
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


def _description(p: dict) -> str:
    out = []
    for body_key, head_key in _SECTIONS:
        body = html_to_text(p.get(body_key), limit=0)
        if not body:
            continue
        head = (p.get(head_key) or "").strip() if head_key else ""
        out.append(f"{head}\n{body}" if head else body)
    return truncate("\n\n".join(out))


class PinpointSource(Source):
    name = "pinpoint"
    kind = "board"
    label = "Pinpoint career sites"
    docs_url = "https://developers.pinpointhq.com/"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        board = pinpoint_board(employer.board or "")
        if not board:
            raise SourceError(f"pinpoint: employer {employer.key} has no board")
        data = await self.get_json(f"https://{board}.pinpointhq.com/postings.json")
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise SourceError(f"pinpoint {board}: unexpected payload")
        jobs = safe_parse(
            data["data"], lambda p: self._parse(p, employer, board), source=self.name, board=board, logger=self.ctx.log
        )
        return jobs[: employer.max_jobs] if employer.max_jobs else jobs

    def _parse(self, p: dict, employer: Employer, board: str) -> Job | None:
        pid, title = p.get("id"), (p.get("title") or "").strip()
        if pid in (None, "") or not title:
            return None
        job_info = p.get("job") if isinstance(p.get("job"), dict) else {}
        dept = job_info.get("department")
        department = dept.get("name") if isinstance(dept, dict) else None
        division = job_info.get("division")
        emp_raw = p.get("employment_type") or p.get("employment_type_text")
        lo = hi = cur = period = None
        if p.get("compensation_visible") is not False:
            lo, hi = to_float(p.get("compensation_minimum")), to_float(p.get("compensation_maximum"))
            if lo is not None or hi is not None:
                cur = p.get("compensation_currency") or None
                period = normalize_period(p.get("compensation_frequency"))
                hi = hi if hi is not None else lo
        extra: dict[str, Any] = {"board": board}
        if job_info.get("requisition_id"):
            extra["requisition_id"] = job_info["requisition_id"]
        if isinstance(division, dict) and division.get("name"):
            extra["division"] = division["name"]
        if p.get("compensation") and p.get("compensation_visible") is not False:
            extra["compensation"] = p["compensation"]
        return Job(
            source=self.name,
            source_id=f"{board}:{pid}",
            title=title,
            company=employer.name,
            url=p.get("url") or f"https://{board}.pinpointhq.com{p.get('path') or ''}",
            location=join_locations([_location(p.get("location"))]),
            remote=remote_from_text(p.get("workplace_type")),
            description=_description(p),
            closes_at=iso_date(p.get("deadline_at")),
            salary_min=lo,
            salary_max=hi,
            salary_currency=cur,
            salary_period=period,
            employment_type=normalize_employment_type(emp_raw) if emp_raw else None,
            department=department,
            employer_key=employer.key,
            tags=structured_tags(emp_raw, department),
            extra=extra,
        )
