"""Greenhouse job boards.

API (verified live 2026-10-25): ``GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true
&pay_transparency=true`` -> ``{"jobs": [...], "meta": {"total": n}}``. ``content`` is HTML-escaped HTML;
``pay_transparency=true`` adds ``pay_input_ranges`` (cents). EU-hosted boards (job-boards.eu.greenhouse.io)
are served by the same API host. Unknown boards answer 404.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Employer, Job
from ..text import html_to_text, iso_date
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

API = "https://boards-api.greenhouse.io/v1/boards"

_WORKPLACE_KEYS = re.compile(r"location type|workplace|work type|remote|work arrangement|work model", re.I)
_TIME_KEYS = re.compile(r"time type|employment type|job type|employee type|worker type|schedule", re.I)
_EARLY_KEYS = re.compile(r"early career|university|intern|graduate|campus|program", re.I)


def _meta_values(job: dict) -> list[tuple[str, Any]]:
    out = []
    for m in job.get("metadata") or []:
        if isinstance(m, dict) and m.get("name"):
            out.append((str(m["name"]), m.get("value")))
    return out


def _meta_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(v) for v in value if v)
    if isinstance(value, dict):
        return ""
    return str(value)


_BLURB_PERIODS = (
    ("hour", re.compile(r"\b(per hour|hourly|an hour|/ ?h(ou)?r)\b", re.I)),
    ("month", re.compile(r"\b(per month|monthly|a month)\b", re.I)),
    ("year", re.compile(r"\b(per year|per annum|annual|annually|yearly|a year)\b", re.I)),
)


def _blurb_period(blurb: str | None) -> str | None:
    """Explicit pay-period phrases only (blurbs also say things like "years of experience")."""
    text = html_to_text(blurb, limit=0) if blurb else ""
    for name, pat in _BLURB_PERIODS:
        if pat.search(text):
            return name
    return None


def _salary(job: dict) -> tuple[float | None, float | None, str | None, str | None]:
    """Pay ranges (cents) -> (min, max, currency, period); falls back to currency_range metadata."""
    ranges = [r for r in job.get("pay_input_ranges") or [] if isinstance(r, dict)]
    lows, highs, currency, period = [], [], None, None
    for r in ranges:
        cur = r.get("currency_type")
        if currency and cur and cur != currency:
            continue
        currency = currency or cur
        lo, hi = to_float(r.get("min_cents")), to_float(r.get("max_cents"))
        if lo is not None:
            lows.append(lo / 100.0)
        if hi is not None:
            highs.append(hi / 100.0)
        period = period or normalize_period(r.get("title")) or _blurb_period(r.get("blurb"))
    if not lows and not highs:
        for _, value in _meta_values(job):
            if isinstance(value, dict) and ("min_value" in value or "max_value" in value):
                lo, hi = to_float(value.get("min_value")), to_float(value.get("max_value"))
                if lo is None and hi is None:
                    continue
                lows, highs, currency = (
                    [x for x in [lo] if x is not None],
                    [x for x in [hi] if x is not None],
                    value.get("unit"),
                )
                break
    if not lows and not highs:
        return None, None, None, None
    lo = min(lows) if lows else None
    hi = max(highs) if highs else lo
    top = hi or lo or 0
    if top < 300 and period in (None, "month", "year"):  # e.g. Robinhood "Zone 1" ranges of $20.40-$24
        period = "hour"
    return lo, hi, currency, period or "year"


def _location(job: dict) -> str:
    name = ((job.get("location") or {}).get("name") or "").strip()
    if name:
        return join_locations(re.split(r"\s*[|;]\s*", name))
    return join_locations(o.get("name") for o in job.get("offices") or [] if isinstance(o, dict))


class GreenhouseSource(Source):
    name = "greenhouse"
    kind = "board"
    label = "Greenhouse job boards"
    docs_url = "https://developers.greenhouse.io/job-board.html"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        board = (employer.board or "").strip()
        if not board:
            raise SourceError(f"greenhouse: employer {employer.key} has no board token")
        data = await self.get_json(
            f"{API}/{board}/jobs", params={"content": "true", "pay_transparency": "true"}
        )
        if not isinstance(data, dict):
            raise SourceError(f"greenhouse {board}: unexpected payload {type(data).__name__}")
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
        if jid is None or not title:
            return None
        meta = _meta_values(j)
        remote: bool | None = None
        employment_type: str | None = None
        early: list[str] = []
        for key, value in meta:
            text = _meta_text(value)
            if not text:
                continue
            if remote is None and _WORKPLACE_KEYS.search(key):
                remote = remote_from_text(text)
            elif employment_type is None and _TIME_KEYS.search(key):
                employment_type = normalize_employment_type(text)
            elif _EARLY_KEYS.search(key) and text.lower() not in ("none", "no", "false", "n/a"):
                early.append(text)
        location = _location(j)
        if remote is None:
            remote = True if remote_from_text(location) is True else None
        departments = [
            d.get("name") for d in j.get("departments") or [] if isinstance(d, dict) and d.get("name")
        ]
        lo, hi, cur, period = _salary(j)
        offices = [o.get("name") for o in j.get("offices") or [] if isinstance(o, dict) and o.get("name")]
        extra: dict[str, Any] = {"board": board}
        if j.get("requisition_id"):
            extra["requisition_id"] = j["requisition_id"]
        if offices:
            extra["offices"] = offices
        if j.get("updated_at"):
            extra["updated_at"] = j["updated_at"]
        return Job(
            source=self.name,
            source_id=str(jid),
            title=title,
            company=employer.name,
            url=j.get("absolute_url") or f"https://job-boards.greenhouse.io/{board}/jobs/{jid}",
            location=location,
            remote=remote,
            description=html_to_text(j.get("content")),
            posted_at=iso_date(j.get("first_published")) or iso_date(j.get("updated_at")),
            closes_at=iso_date(j.get("application_deadline")),
            salary_min=lo,
            salary_max=hi,
            salary_currency=cur,
            salary_period=period,
            employment_type=employment_type,
            department=departments[0] if departments else None,
            employer_key=employer.key,
            tags=structured_tags(employment_type, early, departments),
            extra=extra,
        )

    async def enrich(self, job: Job) -> Job:
        """List calls already carry ``content``; fetch the single job only when it is missing."""
        board = (job.extra or {}).get("board")
        if job.description or not board:
            return job
        try:
            j = await self.get_json(
                f"{API}/{board}/jobs/{job.source_id}", params={"pay_transparency": "true"}
            )
        except Exception as exc:  # noqa: BLE001 - enrichment is best effort
            self.ctx.log.warning("greenhouse enrich %s failed: %s", job.uid, exc)
            return job
        if isinstance(j, dict):
            try:
                job.description = html_to_text(j.get("content")) or job.description
                if job.salary_min is None:
                    job.salary_min, job.salary_max, job.salary_currency, job.salary_period = _salary(j)
            except Exception as exc:  # noqa: BLE001 - a malformed detail payload must not lose the job
                self.ctx.log.warning("greenhouse enrich %s: malformed detail (%s)", job.uid, exc)
        return job
