"""BambooHR career sites.

API (verified live 2026-10-25):

* list: ``GET https://{board}.bamboohr.com/careers/list`` -> ``{"meta": {"totalCount": n}, "result": [{id,
  jobOpeningName, departmentId, departmentLabel, employmentStatusLabel, location{city,state},
  atsLocation{country,state,province,city}, isRemote, locationType}]}``. No descriptions or dates.
  ``locationType``: ``"0"`` on-site, ``"1"`` remote, ``"2"`` hybrid. Unknown subdomains answer ``result: []``.
* detail: ``GET https://{board}.bamboohr.com/careers/{id}/detail`` -> ``{"result": {"jobOpening": {...,
  description (HTML), compensation (free text), datePosted, location{..., addressCountry},
  minimumExperience, jobOpeningShareUrl}}}`` (used by ``enrich``).

Board = careers subdomain (``cnas``). Public URL ``https://{board}.bamboohr.com/careers/{id}``.
``source_id`` = ``"{board}:{id}"`` (ids are per-tenant counters). Verified employers: Center for a New
American Security (``cnas``), OCCRP (``occrp``), Access Now (``accessnow``), Meedan (``meedan``).
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Employer, Job
from ..text import html_to_text, iso_date, parse_salary
from .base import Source, SourceError
from .detect import join_locations, normalize_employment_type, safe_parse, structured_tags

_REMOTE_BY_TYPE = {"0": False, "1": True, "2": False}


def bamboo_board(board: str) -> str:
    board = re.sub(r"^https?://", "", board.strip()).split("/")[0].lower()
    return board.removesuffix(".bamboohr.com")


def _location(j: dict) -> str:
    loc = j.get("location") if isinstance(j.get("location"), dict) else {}
    ats = j.get("atsLocation") if isinstance(j.get("atsLocation"), dict) else {}
    city = loc.get("city") or ats.get("city")
    region = loc.get("state") or ats.get("state") or ats.get("province")
    country = loc.get("addressCountry") or ats.get("country")
    parts = [p for p in (city, region, country) if isinstance(p, str) and p.strip()]
    # "Philippines" province + "Philippines" country -> keep one
    return ", ".join(dict.fromkeys(p.strip() for p in parts))


def _employment(label: Any) -> str | None:
    if not isinstance(label, str) or not label.strip() or label.strip().lower() in ("tbd", "n/a", "other"):
        return None
    return normalize_employment_type(label)


class BambooHRSource(Source):
    name = "bamboohr"
    kind = "board"
    label = "BambooHR career sites"
    docs_url = "https://documentation.bamboohr.com/docs/getting-started"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        if not (employer.board or "").strip():
            raise SourceError(f"bamboohr: employer {employer.key} has no board")
        board = bamboo_board(employer.board or "")
        data = await self.get_json(f"https://{board}.bamboohr.com/careers/list")
        if not isinstance(data, dict) or not isinstance(data.get("result"), list):
            raise SourceError(f"bamboohr {board}: unexpected payload")
        jobs = safe_parse(
            data["result"],
            lambda j: self._parse(j, employer, board),
            source=self.name,
            board=board,
            logger=self.ctx.log,
        )
        return jobs[: employer.max_jobs] if employer.max_jobs else jobs

    def _parse(self, j: dict, employer: Employer, board: str) -> Job | None:
        jid, title = j.get("id"), (j.get("jobOpeningName") or "").strip()
        if jid in (None, "") or not title:
            return None
        remote: bool | None = True if j.get("isRemote") is True else None
        if remote is None:
            remote = _REMOTE_BY_TYPE.get(str(j.get("locationType")))
        status = j.get("employmentStatusLabel")
        department = j.get("departmentLabel") or None
        extra: dict[str, Any] = {"board": board, "id": str(jid)}
        if isinstance(status, str) and status:
            extra["employment_status"] = status
        return Job(
            source=self.name,
            source_id=f"{board}:{jid}",
            title=title,
            company=employer.name,
            url=f"https://{board}.bamboohr.com/careers/{jid}",
            location=join_locations([_location(j)]),
            remote=remote,
            employment_type=_employment(status),
            department=department,
            employer_key=employer.key,
            tags=structured_tags(status, department),
            extra=extra,
        )

    async def enrich(self, job: Job) -> Job:
        """Fetch ``/careers/{id}/detail`` for description, posting date and compensation."""
        extra = job.extra or {}
        board, jid = extra.get("board"), extra.get("id")
        if not board or not jid or job.description:
            return job
        try:
            data = await self.get_json(f"https://{board}.bamboohr.com/careers/{jid}/detail")
        except Exception as exc:  # noqa: BLE001 - enrichment is best effort
            self.ctx.log.warning("bamboohr enrich %s failed: %s", job.uid, exc)
            return job
        opening = ((data or {}).get("result") or {}).get("jobOpening") if isinstance(data, dict) else None
        if not isinstance(opening, dict):
            return job
        job.description = html_to_text(opening.get("description")) or job.description
        job.posted_at = job.posted_at or iso_date(opening.get("datePosted"))
        loc = _location(opening)
        if loc:
            job.location = loc
        comp = opening.get("compensation")
        if isinstance(comp, str) and comp.strip():
            job.extra["compensation"] = comp.strip()
            if job.salary_min is None:
                job.salary_min, job.salary_max, job.salary_currency, job.salary_period = parse_salary(comp)
        if opening.get("minimumExperience"):
            job.extra["minimum_experience"] = opening["minimumExperience"]
        if job.employment_type is None:
            job.employment_type = _employment(opening.get("employmentStatusLabel"))
        return job
