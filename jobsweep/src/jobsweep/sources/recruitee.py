"""Recruitee career sites.

API (verified live 2026-10-25): ``GET https://{board}.recruitee.com/api/offers/`` -> ``{"offers": [...]}``
with full ``description`` + ``requirements`` HTML, ``location``/``city``/``country``/``locations[]``,
``remote``/``hybrid``/``on_site``, ``careers_url`` (often a custom domain such as
jobs.crisisgroup.org), ``published_at``/``close_at`` (``"YYYY-MM-DD HH:MM:SS UTC"``),
``employment_type_code``, ``experience_code``, ``department`` and ``salary`` (string amounts).
Unknown subdomains answer 404.
"""

from __future__ import annotations

from typing import Any

from ..models import Employer, Job
from ..text import html_to_text, iso_date
from .base import Source, SourceError
from .detect import (
    join_locations,
    normalize_employment_type,
    normalize_period,
    safe_parse,
    structured_tags,
    to_float,
)

_EMPLOYMENT = {
    "fulltime": "full-time",
    "fulltime_permanent": "full-time",
    "fulltime_fixed_term": "full-time",
    "parttime": "part-time",
    "parttime_permanent": "part-time",
    "parttime_fixed_term": "part-time",
    "freelance": "contract",
    "contract": "contract",
    "internship": "internship",
    "traineeship": "internship",
    "apprenticeship": "internship",
    "temporary": "temporary",
    "volunteer": "volunteer",
    "per_diem": "per-diem",
}


def _utc(value: Any) -> str | None:
    if not value:
        return None
    s = str(value).strip()
    if s.endswith(" UTC"):
        s = s[:-4] + "+00:00"
    return iso_date(s)


def _location(o: dict) -> str:
    locs = [loc for loc in o.get("locations") or [] if isinstance(loc, dict)]
    if len(locs) > 1:
        parts = []
        for loc in locs:
            city, country = loc.get("city"), loc.get("country")
            parts.append(", ".join(str(p) for p in (city, country) if p) or loc.get("name"))
        return join_locations(parts)
    if o.get("location"):
        return join_locations([o["location"]])
    if locs:
        return join_locations([locs[0].get("name")])
    return ", ".join(str(p) for p in (o.get("city"), o.get("state_name"), o.get("country")) if p)


def _remote(o: dict) -> bool | None:
    if o.get("remote") is True:
        return True
    if o.get("hybrid") is True or o.get("on_site") is True:
        return False
    return None


class RecruiteeSource(Source):
    name = "recruitee"
    kind = "board"
    label = "Recruitee career sites"
    docs_url = "https://docs.recruitee.com/"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        board = (employer.board or "").strip().lower()
        if not board:
            raise SourceError(f"recruitee: employer {employer.key} has no subdomain")
        board = board.removesuffix(".recruitee.com")
        data = await self.get_json(f"https://{board}.recruitee.com/api/offers/")
        if not isinstance(data, dict):
            raise SourceError(f"recruitee {board}: unexpected payload {type(data).__name__}")
        jobs = safe_parse(
            data.get("offers"),
            lambda o: self._parse(o, employer, board),
            source=self.name,
            board=board,
            logger=self.ctx.log,
        )
        return jobs[: employer.max_jobs] if employer.max_jobs else jobs

    def _parse(self, o: dict, employer: Employer, board: str) -> Job | None:
        oid, title = o.get("id"), (o.get("title") or "").strip()
        if oid is None or not title:
            return None
        if o.get("status") not in (None, "published"):
            return None
        code = str(o.get("employment_type_code") or "").lower()
        employment_type = _EMPLOYMENT.get(code) or normalize_employment_type(code)
        exp_code = o.get("experience_code")
        salary = o.get("salary") or {}
        lo, hi = to_float(salary.get("min")), to_float(salary.get("max"))
        has_salary = lo is not None or hi is not None
        html = (o.get("description") or "") + (
            f"<h3>Requirements</h3>{o['requirements']}" if o.get("requirements") else ""
        )
        extra: dict[str, Any] = {"board": board}
        for key, value in (
            ("apply_url", o.get("careers_apply_url")),
            ("country_code", o.get("country_code")),
            ("employment_type_code", code or None),
            ("experience_code", exp_code),
            ("education_code", o.get("education_code")),
        ):
            if value:
                extra[key] = value
        tags = structured_tags(code, exp_code, o.get("department"), o.get("tags") or [])
        return Job(
            source=self.name,
            source_id=str(oid),
            title=title,
            company=employer.name,
            url=o.get("careers_url") or f"https://{board}.recruitee.com/o/{o.get('slug') or oid}",
            location=_location(o),
            remote=_remote(o),
            description=html_to_text(html),
            posted_at=_utc(o.get("published_at")) or _utc(o.get("created_at")),
            closes_at=_utc(o.get("close_at")),
            salary_min=lo,
            salary_max=hi if hi is not None else lo,
            salary_currency=(salary.get("currency") or None) if has_salary else None,
            salary_period=normalize_period(salary.get("period")) if has_salary else None,
            employment_type=employment_type,
            department=o.get("department") or None,
            seniority=str(exp_code).replace("_", " ") if exp_code else None,
            employer_key=employer.key,
            tags=tags,
            extra=extra,
        )
