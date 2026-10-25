"""Breezy HR career portals.

API (verified live 2026-10-25): ``GET https://{board}.breezy.hr/json?verbose=true`` -> JSON list of
positions ``{id, friendly_id, name, url, published_date, type{id,name}, location{country{id,name}, state,
city, is_remote, remote_details{value,label}, name}, locations[...], department, salary (free text),
company{name}, description (HTML, only with verbose=true)}``. Unknown subdomains answer 404; a portal
with no openings answers ``[]``.

Board = portal subdomain (``zantech-it``) or full host/URL. ``source_id`` = Breezy position id (12-hex,
global). Verified employer: Zantech (``zantech-it``, DHS/federal intelligence and IT contractor);
Factal (``factal``) also uses Breezy (no openings at verification time).
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Employer, Job
from ..text import html_to_text, iso_date, parse_salary
from .base import Source, SourceError
from .detect import join_locations, normalize_employment_type, remote_from_text, safe_parse, structured_tags
from .jobvite import jsonld_job_posting


def breezy_host(board: str) -> str:
    board = re.sub(r"^https?://", "", board.strip()).split("/")[0].lower()
    return board if board.endswith(".breezy.hr") else f"{board}.breezy.hr"


def _loc_name(loc: Any) -> str:
    if not isinstance(loc, dict):
        return ""
    if loc.get("name"):
        return str(loc["name"])
    country = (loc.get("country") or {}).get("name") if isinstance(loc.get("country"), dict) else None
    state = loc.get("state")
    state = state.get("name") if isinstance(state, dict) else state
    return ", ".join(str(p) for p in (loc.get("city"), state, country) if p)


def _remote(loc: Any) -> bool | None:
    if not isinstance(loc, dict):
        return None
    details = loc.get("remote_details")
    if isinstance(details, dict) and details.get("value"):
        flag = remote_from_text(details["value"])
        if flag is not None:
            return flag
    if loc.get("is_remote") is True:
        return True
    if loc.get("is_remote") is False:
        return False
    return None


class BreezySource(Source):
    name = "breezy"
    kind = "board"
    label = "Breezy HR career portals"
    docs_url = "https://developer.breezy.hr/"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        if not (employer.board or "").strip():
            raise SourceError(f"breezy: employer {employer.key} has no board")
        host = breezy_host(employer.board or "")
        data = await self.get_json(f"https://{host}/json", params={"verbose": "true"})
        if not isinstance(data, list):
            raise SourceError(f"breezy {host}: unexpected payload {type(data).__name__}")
        board = host.removesuffix(".breezy.hr")
        jobs = safe_parse(
            data,
            lambda p: self._parse(p, employer, board),
            source=self.name,
            board=board,
            logger=self.ctx.log,
        )
        return jobs[: employer.max_jobs] if employer.max_jobs else jobs

    def _parse(self, p: dict, employer: Employer, board: str) -> Job | None:
        pid, title = p.get("id"), (p.get("name") or "").strip()
        if not pid or not title:
            return None
        locs = [loc for loc in p.get("locations") or [] if isinstance(loc, dict)] or [p.get("location")]
        flags = [_remote(loc) for loc in locs if loc]
        remote = True if flags and all(f is True for f in flags) else (False if False in flags else None)
        ptype = p.get("type") if isinstance(p.get("type"), dict) else {}
        emp_type = normalize_employment_type(ptype.get("id") or ptype.get("name"))
        salary_text = p.get("salary") if isinstance(p.get("salary"), str) else None
        lo, hi, cur, period = parse_salary(salary_text)
        department = p.get("department") if isinstance(p.get("department"), str) else None
        extra: dict[str, Any] = {"board": board}
        if salary_text:
            extra["salary_text"] = salary_text
        if p.get("friendly_id"):
            extra["friendly_id"] = p["friendly_id"]
        return Job(
            source=self.name,
            source_id=str(pid),
            title=title,
            company=employer.name,
            url=p.get("url") or f"https://{board}.breezy.hr/p/{p.get('friendly_id') or pid}",
            location=join_locations(_loc_name(loc) for loc in locs),
            remote=remote,
            description=html_to_text(p.get("description")),
            posted_at=iso_date(p.get("published_date")),
            salary_min=lo,
            salary_max=hi,
            salary_currency=cur,
            salary_period=period,
            employment_type=emp_type,
            department=department,
            employer_key=employer.key,
            tags=structured_tags(ptype.get("id"), ptype.get("name"), department),
            extra=extra,
        )

    async def enrich(self, job: Job) -> Job:
        """``verbose=true`` already returns descriptions; otherwise read the posting page's JSON-LD."""
        if job.description or not job.url:
            return job
        try:
            html = await self.get_text(job.url, headers={"Accept": "text/html"})
        except Exception as exc:  # noqa: BLE001 - enrichment is best effort
            self.ctx.log.warning("breezy enrich %s failed: %s", job.uid, exc)
            return job
        posting = jsonld_job_posting(html)
        if posting:
            job.description = html_to_text(posting.get("description")) or job.description
            job.posted_at = job.posted_at or iso_date(posting.get("datePosted"))
        return job
