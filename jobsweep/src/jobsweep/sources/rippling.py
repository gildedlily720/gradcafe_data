"""Rippling ATS job boards.

API (verified live 2026-10-25):

* list: ``GET https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs`` -> JSON list of
  ``{uuid, name, department{id,label}, url, workLocation{id,label}}``. A job open in several places appears
  once per location with the same ``uuid`` (merged here, locations joined). The whole board comes back in
  one response (Rippling's own board: 649 rows). No descriptions or dates.
* detail: ``GET .../board/{board}/jobs/{uuid}`` -> ``{name, description{company, role} (HTML), workLocations[],
  department{name}, employmentType{label,id}, createdOn, payRangeDetails[{currency, frequency, rangeStart,
  rangeEnd, isRemote, location}], companyName, ...}`` (used by ``enrich``).

Board = board slug from ``https://ats.rippling.com/{board}/jobs``. ``source_id`` = job ``uuid``.
Verified employers: Eurasia Group (``eurasia-group``), NDI (``ndi_careers``).
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Employer, Job
from ..text import html_to_text, iso_date
from .base import Source, SourceError
from .detect import join_locations, normalize_employment_type, remote_from_text, structured_tags, to_float

API = "https://api.rippling.com/platform/api/ats/v1/board"
_TYPES = {
    "SALARIED_FT": "full-time",
    "HOURLY_FT": "full-time",
    "SALARIED_PT": "part-time",
    "HOURLY_PT": "part-time",
    "CONTRACTOR": "contract",
    "INTERN": "internship",
    "TEMP": "temporary",
}
_PERIOD = {"YEAR": "year", "MONTH": "month", "WEEK": "week", "DAY": "day", "HOUR": "hour"}


def rippling_board(board: str) -> str:
    raw = (board or "").strip().strip("/")
    m = re.search(r"(?:ats\.rippling\.com/(?:[a-z]{2}(?:-[A-Z]{2})?/)?|/board/)([A-Za-z0-9_-]+)", raw)
    return m.group(1) if m else raw


def _remote(locations: list[str]) -> bool | None:
    flags = [remote_from_text(loc) for loc in locations if loc]
    return True if flags and all(f is True for f in flags) else None


def _employment(et: Any) -> str | None:
    if not isinstance(et, dict):
        return normalize_employment_type(et) if isinstance(et, str) and et else None
    label = str(et.get("label") or "").upper()
    return _TYPES.get(label) or normalize_employment_type(et.get("id") or et.get("label"))


class RipplingSource(Source):
    name = "rippling"
    kind = "board"
    label = "Rippling ATS job boards"
    docs_url = "https://ats.rippling.com/"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        board = rippling_board(employer.board or "")
        if not board:
            raise SourceError(f"rippling: employer {employer.key} has no board")
        data = await self.get_json(f"{API}/{board}/jobs")
        if isinstance(data, dict):  # tolerate a paginated envelope
            data = data.get("results") or data.get("items") or data.get("jobs")
        if not isinstance(data, list):
            raise SourceError(f"rippling {board}: unexpected payload")
        merged: dict[str, dict[str, Any]] = {}
        for row in data:
            try:
                if not isinstance(row, dict) or not row.get("uuid") or not (row.get("name") or "").strip():
                    continue
                cur = merged.setdefault(str(row["uuid"]), {"row": row, "locations": []})
                loc = row.get("workLocation")
                label = loc.get("label") if isinstance(loc, dict) else loc
                if isinstance(label, str) and label.strip():
                    cur["locations"].append(label.strip())
            except Exception as exc:  # noqa: BLE001 - one bad row must not sink the board
                self.ctx.log.warning("rippling %s: skipping malformed row (%s)", board, exc)
        jobs = []
        for uuid, item in merged.items():
            row, locations = item["row"], item["locations"]
            dept = row.get("department")
            department = (dept.get("label") or dept.get("id")) if isinstance(dept, dict) else dept
            jobs.append(
                Job(
                    source=self.name,
                    source_id=uuid,
                    title=row["name"].strip(),
                    company=employer.name,
                    url=row.get("url") or f"https://ats.rippling.com/{board}/jobs/{uuid}",
                    location=join_locations(locations),
                    remote=_remote(locations),
                    department=department or None,
                    employer_key=employer.key,
                    tags=structured_tags(department),
                    extra={"board": board},
                )
            )
        return jobs[: employer.max_jobs] if employer.max_jobs else jobs

    async def enrich(self, job: Job) -> Job:
        """Fetch the job detail: description, employment type, created date and pay ranges."""
        board = (job.extra or {}).get("board")
        if not board or job.description:
            return job
        try:
            d = await self.get_json(f"{API}/{board}/jobs/{job.source_id}")
        except Exception as exc:  # noqa: BLE001 - enrichment is best effort
            self.ctx.log.warning("rippling enrich %s failed: %s", job.uid, exc)
            return job
        if not isinstance(d, dict):
            return job
        desc = d.get("description")
        if isinstance(desc, dict):
            parts = [html_to_text(desc.get("role"), limit=0), html_to_text(desc.get("company"), limit=0)]
            job.description = html_to_text("\n\n".join(p for p in parts if p).replace("\n", "<br>")) or job.description
        elif isinstance(desc, str):
            job.description = html_to_text(desc)
        locations = [str(x) for x in d.get("workLocations") or [] if x]
        if locations:
            job.location = join_locations(locations)
            job.remote = _remote(locations)
        job.employment_type = _employment(d.get("employmentType")) or job.employment_type
        for tag in structured_tags(job.employment_type):
            if tag not in job.tags:
                job.tags.append(tag)
        job.posted_at = job.posted_at or iso_date(d.get("createdOn"))
        ranges = [p for p in d.get("payRangeDetails") or [] if isinstance(p, dict)]
        if ranges and job.salary_min is None:
            cur = ranges[0].get("currency")
            same = [p for p in ranges if p.get("currency") == cur]
            lows = [x for x in (to_float(p.get("rangeStart")) for p in same) if x is not None]
            highs = [x for x in (to_float(p.get("rangeEnd")) for p in same) if x is not None]
            if lows or highs:
                job.salary_min = min(lows) if lows else None
                job.salary_max = max(highs) if highs else job.salary_min
                job.salary_currency = cur
                job.salary_period = _PERIOD.get(str(ranges[0].get("frequency") or "").upper())
            if all(p.get("isRemote") is True for p in ranges):
                job.remote = True
        return job
