"""Ashby job boards.

API (verified live 2026-10-25): ``GET https://api.ashbyhq.com/posting-api/job-board/{board}
?includeCompensation=true`` -> ``{"jobs": [...], "apiVersion": "1"}``; 404 for unknown boards. Postings
with ``isListed: false`` are skipped. Note Ashby sets ``isRemote: true`` on *hybrid* jobs, so
``workplaceType`` wins when present.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..models import Employer, Job
from ..text import html_to_text, iso_date, parse_salary
from .base import Source, SourceError
from .detect import (
    join_locations,
    normalize_employment_type,
    normalize_period,
    plain_text,
    remote_from_text,
    safe_parse,
    structured_tags,
    to_float,
)

API = "https://api.ashbyhq.com/posting-api/job-board"


def _salary_components(comp: dict) -> list[dict]:
    comps = [c for c in comp.get("summaryComponents") or [] if isinstance(c, dict)]
    if not comps:
        for tier in comp.get("compensationTiers") or []:
            if isinstance(tier, dict):
                comps.extend(c for c in tier.get("components") or [] if isinstance(c, dict))
    return [c for c in comps if str(c.get("compensationType") or "").lower() == "salary"]


def compensation(comp: Any) -> tuple[float | None, float | None, str | None, str | None]:
    """Ashby compensation block -> (min, max, currency, period)."""
    if not isinstance(comp, dict):
        return None, None, None, None
    lows, highs, currency, period = [], [], None, None
    for c in _salary_components(comp):
        cur = c.get("currencyCode")
        if currency and cur and cur != currency:
            continue
        lo, hi = to_float(c.get("minValue")), to_float(c.get("maxValue"))
        if lo is None and hi is None:
            continue
        currency = currency or cur
        period = period or normalize_period(c.get("interval"))
        if lo is not None:
            lows.append(lo)
        if hi is not None:
            highs.append(hi)
    if lows or highs:
        lo = min(lows) if lows else None
        return lo, max(highs) if highs else lo, currency, period
    summary = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary")
    lo, hi, cur, per = parse_salary(summary)
    return lo, hi, cur, per


def _remote(job: dict, location: str) -> bool | None:
    wt = str(job.get("workplaceType") or "").lower().replace("-", "").replace("_", "")
    if wt == "remote":
        return True
    if wt in ("hybrid", "onsite"):
        return False
    if job.get("isRemote") is True:
        return True
    if job.get("isRemote") is False:
        return False
    return True if remote_from_text(location) is True else None


class AshbySource(Source):
    name = "ashby"
    kind = "board"
    label = "Ashby job boards"
    docs_url = "https://developers.ashbyhq.com/docs/public-job-posting-api"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        board = (employer.board or "").strip()
        if not board:
            raise SourceError(f"ashby: employer {employer.key} has no job board name")
        data = await self.get_json(f"{API}/{quote(board)}", params={"includeCompensation": "true"})
        if not isinstance(data, dict):
            raise SourceError(f"ashby {board}: unexpected payload {type(data).__name__}")
        jobs = safe_parse(
            data.get("jobs"),
            lambda j: self._parse(j, employer, board),
            source=self.name,
            board=board,
            logger=self.ctx.log,
        )
        return jobs[: employer.max_jobs] if employer.max_jobs else jobs

    def _parse(self, j: dict, employer: Employer, board: str) -> Job | None:
        jid, title = j.get("id"), (j.get("title") or "").strip()
        if not jid or not title or j.get("isListed") is False:
            return None
        secondary = []
        for s in j.get("secondaryLocations") or []:
            secondary.append(s.get("location") if isinstance(s, dict) else s)
        location = join_locations([j.get("location"), *secondary])
        lo, hi, cur, period = compensation(j.get("compensation"))
        raw_type = j.get("employmentType")
        description = plain_text(j.get("descriptionPlain")) or html_to_text(j.get("descriptionHtml"))
        extra: dict[str, Any] = {"board": board}
        comp = j.get("compensation") or {}
        if isinstance(comp, dict) and comp.get("compensationTierSummary"):
            extra["compensation"] = comp["compensationTierSummary"]
        if j.get("team") and j.get("team") != j.get("department"):
            extra["team"] = j["team"]
        if j.get("applyUrl"):
            extra["apply_url"] = j["applyUrl"]
        country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")) or None
        if country:
            extra["country"] = country
        return Job(
            source=self.name,
            source_id=str(jid),
            title=title,
            company=employer.name,
            url=j.get("jobUrl") or f"https://jobs.ashbyhq.com/{quote(board)}/{jid}",
            location=location,
            remote=_remote(j, location),
            description=description,
            posted_at=iso_date(j.get("publishedAt")),
            salary_min=lo,
            salary_max=hi,
            salary_currency=cur,
            salary_period=period,
            employment_type=normalize_employment_type(raw_type),
            department=j.get("department") or j.get("team") or None,
            employer_key=employer.key,
            tags=structured_tags(raw_type, j.get("department"), j.get("team")),
            extra=extra,
        )
