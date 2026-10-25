"""Lever postings.

API (verified live 2026-10-25): ``GET https://api.lever.co/v0/postings/{site}?mode=json`` returns a JSON
list of every published posting (``[]`` for a site with no openings, 404 ``{"ok": false}`` for an unknown
site). Boards written ``eu:{site}`` use ``https://api.eu.lever.co``.
"""

from __future__ import annotations

from typing import Any

from ..models import Employer, Job
from ..text import MAX_DESCRIPTION_CHARS, html_to_text, iso_date
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


def api_url(board: str) -> tuple[str, str]:
    """``board`` -> (postings URL, bare site name)."""
    board = board.strip()
    if board.lower().startswith("eu:"):
        site = board[3:].strip()
        return f"https://api.eu.lever.co/v0/postings/{site}", site
    return f"https://api.lever.co/v0/postings/{board}", board


def _description(p: dict) -> str:
    parts: list[str] = []
    body = p.get("descriptionPlain") or html_to_text(p.get("description"), limit=0)
    if body:
        parts.append(plain_text(body, limit=0))
    for lst in p.get("lists") or []:
        if not isinstance(lst, dict):
            continue
        heading = (lst.get("text") or "").strip()
        content = html_to_text(lst.get("content"), limit=0)
        if heading or content:
            parts.append(f"{heading}\n{content}".strip())
    tail = p.get("additionalPlain") or html_to_text(p.get("additional"), limit=0)
    if tail:
        parts.append(plain_text(tail, limit=0))
    salary_text = p.get("salaryDescriptionPlain")
    if salary_text and all(salary_text.strip()[:60] not in part for part in parts):
        parts.append(plain_text(salary_text, limit=0))
    return plain_text("\n\n".join(parts), limit=MAX_DESCRIPTION_CHARS)


def _remote(workplace: Any, location: str) -> bool | None:
    wt = str(workplace or "").lower()
    if wt == "remote":
        return True
    if wt in ("hybrid", "onsite", "on-site", "on_site"):
        return False
    return True if remote_from_text(location) is True else None


class LeverSource(Source):
    name = "lever"
    kind = "board"
    label = "Lever job postings"
    docs_url = "https://github.com/lever/postings-api"

    async def fetch_board(self, employer: Employer) -> list[Job]:
        board = (employer.board or "").strip()
        if not board:
            raise SourceError(f"lever: employer {employer.key} has no site name")
        url, site = api_url(board)
        data = await self.get_json(url, params={"mode": "json"})
        if not isinstance(data, list):
            raise SourceError(f"lever {board}: unexpected payload {str(data)[:120]}")
        jobs = safe_parse(
            data,
            lambda p: self._parse(p, employer, board, site),
            source=self.name,
            board=board,
            logger=self.ctx.log,
        )
        return jobs[: employer.max_jobs] if employer.max_jobs else jobs

    def _parse(self, p: dict, employer: Employer, board: str, site: str) -> Job | None:
        pid, title = p.get("id"), (p.get("text") or "").strip()
        if not pid or not title:
            return None
        cats = p.get("categories") or {}
        all_locs = cats.get("allLocations") or []
        if not isinstance(all_locs, list):
            all_locs = [all_locs]
        location = join_locations([cats.get("location"), *all_locs])
        commitment = cats.get("commitment")
        employment_type = normalize_employment_type(commitment)
        salary = p.get("salaryRange") or {}
        lo, hi = to_float(salary.get("min")), to_float(salary.get("max"))
        extra: dict[str, Any] = {"board": board}
        for key, value in (
            ("team", cats.get("team")),
            ("country", p.get("country")),
            ("workplace_type", p.get("workplaceType")),
            ("apply_url", p.get("applyUrl")),
            ("commitment", commitment),
        ):
            if value:
                extra[key] = value
        return Job(
            source=self.name,
            source_id=str(pid),
            title=title,
            company=employer.name,
            url=p.get("hostedUrl") or f"https://jobs.lever.co/{site}/{pid}",
            location=location,
            remote=_remote(p.get("workplaceType"), location),
            description=_description(p),
            posted_at=iso_date(p.get("createdAt")),
            salary_min=lo,
            salary_max=hi if hi is not None else lo,
            salary_currency=(salary.get("currency") or None) if (lo is not None or hi is not None) else None,
            salary_period=normalize_period(salary.get("interval"))
            if (lo is not None or hi is not None)
            else None,
            employment_type=employment_type,
            department=cats.get("department") or cats.get("team") or None,
            employer_key=employer.key,
            tags=structured_tags(commitment, cats.get("department"), cats.get("team")),
            extra=extra,
        )
